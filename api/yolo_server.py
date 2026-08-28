"""7004 YOLO 常驻推理服务。

为什么单独常驻：ultralytics 装在 yolo 环境，流程 API 跑在 fastapi 环境，
本来就没法同进程；模型加载+预热约 2s、常驻内存约 1GB，开机拉起一次，
流程 API / 采集台 / 前端都通过 HTTP 问它。

模型类别（Xuanniu.pt）: 0=就地 1=远方 2=分闸 3=0 4=合闸

对流程的意义：本服务只报告真实印刷状态「就地/远方」，起止状态由任务
language 决定。开始前识别到任务目标状态则无需拨；识别到拨前状态才执行；
拨后识别到目标状态才算成功。

启动（yolo 环境）：
    /home/robot/miniconda3/envs/yolo/bin/python -m api.yolo_server \
        --model models/Xuanniu.pt --conf 0.25

接口：
    GET  /api/yolo/status   模型名/类别表/阈值（也当健康检查用）
    POST /api/yolo/infer    从 reach_server 抓一帧推理，返回全部框
    GET  /api/yolo/scene    抓帧推理后归类：就地 / 远方 / null（没识别到）
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="yolo-server")

# 只连本机 reach_server，绝不走系统代理——终端里设了坏代理也不受影响
_http = requests.Session()
_http.trust_env = False

_reach_base = "http://127.0.0.1:8001"
_model = None
_model_name = ""
_names: dict[int, str] = {}
_conf = 0.25
_lock = threading.Lock()   # ultralytics 推理不保证线程安全，串行化

SCENE_CLASSES = ("就地", "远方")


def _grab_jpeg(timeout_s: float = 5.0) -> bytes:
    """从 reach_server 的 MJPEG 流抓一帧完整 JPEG。"""
    r = _http.get(f"{_reach_base}/api/reach/stream", stream=True,
                  timeout=(3.0, timeout_s))
    try:
        r.raise_for_status()
        buf = b""
        deadline = time.monotonic() + timeout_s
        for chunk in r.iter_content(chunk_size=16384):
            buf += chunk
            start = buf.find(b"\xff\xd8")
            if start >= 0:
                end = buf.find(b"\xff\xd9", start + 2)
                if end >= 0:
                    return buf[start:end + 2]
            if time.monotonic() > deadline:
                break
            if len(buf) > 8 * 1024 * 1024:
                buf = buf[-2 * 1024 * 1024:]
        raise RuntimeError(f"{timeout_s}s 内没抓到完整帧")
    finally:
        r.close()


def _grab_wrist_jpeg(timeout_s: float = 3.0) -> bytes:
    """Fetch the latest wrist JPEG relayed from teleimager ZMQ by reach_server."""
    r = _http.get(
        f"{_reach_base}/api/reach/wrist_snapshot",
        timeout=(2.0, timeout_s),
    )
    try:
        r.raise_for_status()
        if not r.content.startswith(b"\xff\xd8"):
            raise RuntimeError("腕部相机响应不是 JPEG")
        return bytes(r.content)
    finally:
        r.close()


def _infer_jpeg(jpeg: bytes, conf: float | None = None) -> list[dict]:
    import numpy as np

    import cv2
    img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("帧解码失败")
    with _lock:
        results = _model.predict(img, conf=conf or _conf, verbose=False)
    boxes = []
    for r in results:
        for b in r.boxes:
            cls = int(b.cls[0])
            boxes.append({
                "cls": cls,
                "name": str(_names.get(cls, cls)),
                "conf": round(float(b.conf[0]), 3),
                "xyxy": [round(float(v), 1) for v in b.xyxy[0].tolist()],
            })
    return boxes


def _grab_and_infer(conf: float | None = None, keep_jpeg: bool = False) -> dict:
    try:
        jpeg = _grab_jpeg()
    except Exception as exc:
        return {"ok": False, "error": f"抓帧失败（reach_server 在跑吗？）: {exc}"}
    try:
        boxes = _infer_jpeg(jpeg, conf)
    except Exception as exc:
        return {"ok": False, "error": f"推理失败: {exc}"}
    out: dict = {"ok": True, "boxes": boxes}
    if keep_jpeg:
        out["jpeg"] = jpeg
    return out


@app.get("/api/yolo/status")
def status():
    return {"ok": True, "model": _model_name, "names": _names, "conf": _conf,
            "reach_base": _reach_base}


@app.post("/api/yolo/infer")
def infer(body: dict | None = None):
    body = body or {}
    res = _grab_and_infer(float(body["conf"]) if body.get("conf") else None)
    if not res["ok"]:
        return JSONResponse(res, status_code=502)
    return res


@app.get("/api/yolo/scene")
def scene(
    conf: float | None = None,
    include_image: bool = False,
    include_wrist: bool = False,
):
    """就地/远方归类：取两类框里置信度最高的那个定调。

    返回 {"ok": true, "scene": "就地"|"远方"|null, "conf": ..., "boxes": [...]}
    scene=null 表示画面里没识别到这两类（调用方转人工或报错）。
    include_image=true 时返回体多一个 jpeg_b64——就是本次判定用的头部帧。
    include_wrist=true 时再取一帧右腕 JPEG，只用于横移拨动前留档。
    """
    res = _grab_and_infer(conf, keep_jpeg=include_image)
    if not res["ok"]:
        return JSONResponse(res, status_code=502)
    candidates = [b for b in res["boxes"] if b["name"] in SCENE_CLASSES]
    best = max(candidates, key=lambda b: b["conf"], default=None)
    out = {"ok": True,
           "scene": best["name"] if best else None,
           "conf": best["conf"] if best else None,
           "boxes": res["boxes"]}
    if include_image and res.get("jpeg") is not None:
        import base64
        out["jpeg_b64"] = base64.b64encode(res["jpeg"]).decode("ascii")
        if include_wrist:
            try:
                wrist_jpeg = _grab_wrist_jpeg()
                out["wrist_jpeg_b64"] = base64.b64encode(wrist_jpeg).decode("ascii")
            except Exception as exc:
                # 右腕图只做拨动前证据，缺失不能改变头部 YOLO 的控制结论。
                out["wrist_error"] = str(exc)
    return out


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> None:
    global _reach_base, _model, _model_name, _names, _conf
    import uvicorn

    parser = argparse.ArgumentParser(description="YOLO 常驻推理服务（7004）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7004)
    parser.add_argument("--reach-base", default="http://127.0.0.1:8001")
    parser.add_argument("--model", default="models/Xuanniu.pt",
                        help="YOLO .pt 模型路径")
    parser.add_argument("--conf", type=float, default=0.25, help="置信度阈值")
    args = parser.parse_args()
    _reach_base = args.reach_base.rstrip("/")
    _conf = args.conf

    from ultralytics import YOLO
    t0 = time.perf_counter()
    _model = YOLO(args.model)
    _model_name = Path(args.model).name
    _names = {int(k): str(v) for k, v in (_model.names or {}).items()}
    # 预热：首次推理比后续慢约 1s，启动时垫掉，别让流程第一问吃这个延迟
    import numpy as np
    _model.predict(np.zeros((720, 1280, 3), np.uint8), conf=_conf, verbose=False)
    print(f"[yolo] 模型 {_model_name} 加载+预热完成"
          f"（{time.perf_counter() - t0:.1f}s），类别: {_names}")
    missing = [c for c in SCENE_CLASSES if c not in _names.values()]
    if missing:
        print(f"[yolo] ⚠ 模型没有场景类别 {missing}，/scene 会永远返回 null")

    print(f"[yolo] 服务已启动（常驻属正常）: http://{_lan_ip()}:{args.port}/")
    print(f"[yolo] 抓帧来源: {_reach_base}/api/reach/stream")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
