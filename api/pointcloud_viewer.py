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
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .pointcloud_core import (
    build_pointcloud,
    encode_pointcloud,
    fit_surface_plane,
    point_from_pixel,
)


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

app = FastAPI(title="pointcloud-viewer")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="pointcloud-web")

_http = requests.Session()
_http.trust_env = False
_reach_base = "http://127.0.0.1:18001"
_model = None
_model_name = ""
_names: dict[int, str] = {}
_default_conf = 0.25
_box_padding_ratio = 0.1
_model_lock = threading.Lock()
_capture_lock = threading.Lock()


@dataclass
class Capture:
    capture_id: str
    binary: bytes
    jpeg: bytes
    metadata: dict[str, Any]
    depth_mm: np.ndarray
    intrinsics: tuple[float, float, float, float]
    created_monotonic: float


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


@app.get("/api/pointcloud/stream")
def camera_stream():
    """Proxy reach_server's MJPEG stream for the integrated live preview."""
    try:
        upstream = _http.get(
            f"{_reach_base}/api/reach/stream",
            stream=True,
            timeout=(3.0, None),
        )
        upstream.raise_for_status()
    except requests.RequestException as exc:
        return Response(
            f"相机流不可达（{_reach_base}）: {exc}",
            status_code=502,
            media_type="text/plain",
        )

    def generate():
        try:
            yield from upstream.iter_content(chunk_size=65536)
        finally:
            upstream.close()

    return StreamingResponse(
        generate(),
        media_type=upstream.headers.get(
            "Content-Type",
            "multipart/x-mixed-replace; boundary=frame",
        ),
        headers={"Cache-Control": "no-cache"},
    )


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
            dense_box_sampling=True,
            box_padding_ratio=_box_padding_ratio,
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
        "image_url": f"/api/pointcloud/image/{capture_id}",
        "point_count": cloud.count,
        "stride": stride,
        "dense_box_sampling": True,
        "box_padding_ratio": _box_padding_ratio,
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
        _latest = Capture(
            capture_id=capture_id,
            binary=binary,
            jpeg=snapshot["jpeg"],
            metadata=metadata,
            depth_mm=snapshot["depth_mm"],
            intrinsics=snapshot["intrinsics"],
            created_monotonic=time.monotonic(),
        )
    return metadata


@app.get("/api/pointcloud/image/{capture_id}")
def capture_image(capture_id: str):
    with _capture_lock:
        capture_value = _latest
    if capture_value is None or capture_value.capture_id != capture_id:
        return JSONResponse(
            {"ok": False, "error": "快照图像不存在或已被新快照替换"},
            status_code=404,
        )
    return Response(
        capture_value.jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


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


def _capture_by_id(capture_id: str) -> Capture | None:
    with _capture_lock:
        capture_value = _latest
    if capture_value is None or capture_value.capture_id != capture_id:
        return None
    return capture_value


@app.post("/api/pointcloud/pixel/{capture_id}")
def pointcloud_pixel(capture_id: str, body: dict):
    capture_value = _capture_by_id(capture_id)
    if capture_value is None:
        return JSONResponse(
            {"ok": False, "error": "快照不存在或已被新快照替换"},
            status_code=404,
        )
    try:
        result = point_from_pixel(
            capture_value.depth_mm,
            capture_value.intrinsics,
            int(body["u"]),
            int(body["v"]),
            search_radius=int(body.get("search_radius", 6)),
            z_min_m=float(
                body.get("z_min_m", capture_value.metadata["z_min_m"])
            ),
            z_max_m=float(
                body.get("z_max_m", capture_value.metadata["z_max_m"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"RGB 选点失败: {exc}"}, status_code=400
        )
    return {
        "ok": True,
        "capture_id": capture_id,
        **result,
    }


@app.post("/api/pointcloud/confirm/{capture_id}")
def confirm_pointcloud_target(capture_id: str, body: dict):
    capture_value = _capture_by_id(capture_id)
    if capture_value is None:
        return JSONResponse(
            {"ok": False, "error": "快照不存在或已被新快照替换"},
            status_code=404,
        )
    try:
        p_camera = np.asarray(body["p_camera"], dtype=float).reshape(3)
        reference = np.asarray(
            body.get("surface_reference_camera", p_camera),
            dtype=float,
        ).reshape(3)
        adjustment = np.asarray(
            body.get("adjustment_camera_m", [0.0, 0.0, 0.0]),
            dtype=float,
        ).reshape(3)
        if (
            not np.isfinite(p_camera).all()
            or not np.isfinite(reference).all()
            or not np.isfinite(adjustment).all()
        ):
            raise ValueError("三维坐标包含非有限数值")
        plane = fit_surface_plane(
            capture_value.depth_mm,
            capture_value.intrinsics,
            reference,
        )
        request_body = {
            "p_camera_surface": p_camera.tolist(),
            "pixel": body.get("pixel"),
            "adjustment_camera_m": adjustment.tolist(),
            "approach_offset_m": float(body.get("approach_offset_m", 0.0)),
            "plane": plane,
            "source_frame_id": capture_value.metadata.get("source", {}).get(
                "frame_id"
            ),
        }
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"确认目标参数非法: {exc}"},
            status_code=400,
        )
    try:
        upstream = _http.post(
            f"{_reach_base}/api/reach/confirm_pointcloud_pick",
            json=request_body,
            timeout=(3.0, 15.0),
        )
        result = upstream.json()
        if not upstream.ok or not result.get("ok"):
            raise RuntimeError(
                result.get("error") or f"reach HTTP {upstream.status_code}"
            )
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"18001 拒绝目标: {exc}"},
            status_code=502,
        )
    result["capture_id"] = capture_id
    result["capture_age_s"] = round(
        time.monotonic() - capture_value.created_monotonic, 3
    )
    return result


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
    parser.add_argument("--reach-base", default="http://127.0.0.1:18001")
    parser.add_argument("--model", default="models/Xuanniu.pt")
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
