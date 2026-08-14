"""7005 single-frame RGB / YOLO-box semantic point-cloud viewer."""

from __future__ import annotations

import argparse
import io
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .pointcloud_core import build_pointcloud, encode_pointcloud


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

app = FastAPI(title="pointcloud-viewer")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="pointcloud-web")

_http = requests.Session()
_http.trust_env = False
_reach_base = "http://127.0.0.1:8001"
_model = None
_model_name = ""
_names: dict[int, str] = {}
_default_conf = 0.25
_model_lock = threading.Lock()
_capture_lock = threading.Lock()


@dataclass
class Capture:
    capture_id: str
    binary: bytes
    metadata: dict[str, Any]


_latest: Capture | None = None


def _fetch_rgbd_snapshot(timeout_s: float = 15.0) -> dict[str, Any]:
    response = _http.get(
        f"{_reach_base}/api/reach/rgbd_snapshot",
        timeout=(3.0, timeout_s),
    )
    try:
        response.raise_for_status()
        with np.load(io.BytesIO(response.content), allow_pickle=False) as archive:
            jpeg = archive["jpeg"].astype(np.uint8, copy=True).tobytes()
            depth_mm = archive["depth_mm"].astype(np.float32, copy=True)
            intrinsics = archive["intrinsics"].astype(np.float64, copy=True)
            metadata = json.loads(
                archive["metadata_json"].astype(np.uint8, copy=False).tobytes().decode("utf-8")
            )
            transform = archive["T_cam2root"].astype(np.float64, copy=True)
    except Exception as exc:
        raise RuntimeError(f"RGB-D 快照解析失败: {exc}") from exc
    if intrinsics.shape != (4,):
        raise RuntimeError(f"RGB-D 内参 shape 异常: {intrinsics.shape}")
    if transform.size == 0:
        transform_value = None
    elif transform.shape == (4, 4):
        transform_value = transform.tolist()
    else:
        raise RuntimeError(f"T_cam2root shape 异常: {transform.shape}")
    return {
        "jpeg": jpeg,
        "depth_mm": depth_mm,
        "intrinsics": tuple(float(value) for value in intrinsics),
        "metadata": metadata,
        "T_cam2root": transform_value,
    }


def _infer(bgr: np.ndarray, conf: float) -> list[dict[str, Any]]:
    if _model is None:
        raise RuntimeError("YOLO 模型尚未加载")
    with _model_lock:
        results = _model.predict(bgr, conf=conf, verbose=False)
    boxes: list[dict[str, Any]] = []
    for result in results:
        if result.boxes is None:
            continue
        for box in result.boxes:
            cls = int(box.cls[0])
            boxes.append({
                "cls": cls,
                "name": str(_names.get(cls, cls)),
                "conf": round(float(box.conf[0]), 4),
                "xyxy": [round(float(value), 2) for value in box.xyxy[0].tolist()],
            })
    return boxes


@app.get("/")
def page():
    return FileResponse(WEB_DIR / "pointcloud.html")


@app.get("/api/pointcloud/status")
def status():
    with _capture_lock:
        latest_id = None if _latest is None else _latest.capture_id
    return {
        "ok": True,
        "model": _model_name,
        "names": _names,
        "conf": _default_conf,
        "reach_base": _reach_base,
        "latest_capture_id": latest_id,
        "semantic_mode": "yolo_detection_boxes",
    }


@app.post("/api/pointcloud/capture")
def capture(body: dict | None = None):
    global _latest
    body = body or {}
    try:
        stride = int(body.get("stride", 4))
        z_min_m = float(body.get("z_min_m", 0.15))
        z_max_m = float(body.get("z_max_m", 3.0))
        conf = float(body.get("conf", _default_conf))
        if not 0.01 <= conf <= 1.0:
            raise ValueError("conf 必须在 0.01~1.0")
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"参数非法: {exc}"}, status_code=400)

    started = time.perf_counter()
    try:
        snapshot = _fetch_rgbd_snapshot()
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"无法从 reach_server 获取同帧 RGB-D: {exc}"},
            status_code=502,
        )
    bgr = cv2.imdecode(
        np.frombuffer(snapshot["jpeg"], dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if bgr is None:
        return JSONResponse({"ok": False, "error": "JPEG 解码失败"}, status_code=502)
    try:
        boxes = _infer(bgr, conf)
        cloud = build_pointcloud(
            snapshot["depth_mm"],
            bgr,
            snapshot["intrinsics"],
            boxes,
            stride=stride,
            z_min_m=z_min_m,
            z_max_m=z_max_m,
        )
        binary = encode_pointcloud(cloud)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"点云生成失败: {exc}"},
            status_code=500,
        )

    capture_id = uuid.uuid4().hex
    class_counts = {
        str(int(cls)): int(np.count_nonzero(cloud.class_ids == cls))
        for cls in np.unique(cloud.class_ids)
        if int(cls) >= 0
    }
    metadata = {
        "ok": True,
        "capture_id": capture_id,
        "data_url": f"/api/pointcloud/data/{capture_id}",
        "point_count": cloud.count,
        "stride": stride,
        "z_min_m": z_min_m,
        "z_max_m": z_max_m,
        "conf": conf,
        "model": _model_name,
        "names": _names,
        "boxes": boxes,
        "class_point_counts": class_counts,
        "intrinsics": list(snapshot["intrinsics"]),
        "source": snapshot["metadata"],
        "T_cam2root": snapshot["T_cam2root"],
        "capture_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "binary_protocol": {
            "magic": "PCV1",
            "version": 1,
            "bytes_per_point": 24,
            "arrays": ["positions_f32x3", "rgb_u8x3", "semantic_u8x3",
                       "pixels_u16x2", "class_ids_i16"],
        },
    }
    with _capture_lock:
        _latest = Capture(capture_id=capture_id, binary=binary, metadata=metadata)
    return metadata


@app.get("/api/pointcloud/data/{capture_id}")
def pointcloud_data(capture_id: str):
    with _capture_lock:
        capture_value = _latest
    if capture_value is None or capture_value.capture_id != capture_id:
        return JSONResponse(
            {"ok": False, "error": "点云快照不存在或已被新快照替换"},
            status_code=404,
        )
    return Response(
        capture_value.binary,
        media_type="application/vnd.ik-replay.pointcloud",
        headers={"Cache-Control": "no-store"},
    )


def _lan_ip() -> str:
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    global _reach_base, _model, _model_name, _names, _default_conf
    import uvicorn

    parser = argparse.ArgumentParser(description="RGB/YOLO语义点云查看器（7005）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7005)
    parser.add_argument("--reach-base", default="http://127.0.0.1:8001")
    parser.add_argument("--model", default="models/Xuanniu-NJ.pt")
    parser.add_argument("--conf", type=float, default=0.25)
    args = parser.parse_args()
    _reach_base = args.reach_base.rstrip("/")
    _default_conf = args.conf

    from ultralytics import YOLO

    started = time.perf_counter()
    _model = YOLO(args.model)
    _model_name = Path(args.model).name
    _names = {int(key): str(value) for key, value in (_model.names or {}).items()}
    _model.predict(np.zeros((720, 1280, 3), dtype=np.uint8),
                   conf=_default_conf, verbose=False)
    print(
        f"[pointcloud] 模型 {_model_name} 加载+预热完成"
        f"（{time.perf_counter() - started:.1f}s），类别: {_names}"
    )
    print(f"[pointcloud] RGB-D 来源: {_reach_base}/api/reach/rgbd_snapshot")
    print(f"[pointcloud] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
