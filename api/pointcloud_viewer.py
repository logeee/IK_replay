"""7005 single-frame RGB / YOLO-box semantic point-cloud viewer."""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
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

from core.capability_client import (
    DEFAULT_CAPABILITY_URL,
    CapabilityUnavailable,
    describe_active,
    fetch_snapshot,
)

from .pointcloud_core import (
    PointCloud,
    build_pointcloud,
    detection_pixel_mask,
    encode_pointcloud,
    fit_surface_plane,
    point_from_pixel,
)


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
SCENE_MISMATCH_TRAINING_DIR = (
    ROOT / "data" / "training_samples" / "scene_mismatch"
)

app = FastAPI(title="pointcloud-viewer")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="pointcloud-web")

_http = requests.Session()
_http.trust_env = False
_reach_base = "http://127.0.0.1:18001"
_capability_snapshot: dict[str, Any] | None = None   # 启动拜访 18000 的注册表快照
_model = None
_model_name = ""
_model_error = ""
_names: dict[int, str] = {}
_default_conf = 0.25
_box_padding_ratio = 0.1
_model_lock = threading.Lock()
_capture_lock = threading.Lock()
_auto_target_lock = threading.Lock()
_capture_progress_lock = threading.Lock()
_capture_progress: dict[str, dict[str, Any]] = {}


@dataclass
class Capture:
    capture_id: str
    binary: bytes
    jpeg: bytes
    metadata: dict[str, Any]
    depth_mm: np.ndarray
    intrinsics: tuple[float, float, float, float]
    distortion: np.ndarray
    created_monotonic: float
    bgr: np.ndarray
    cloud: PointCloud
    boxes: list[dict[str, Any]]
    wall_plane: dict[str, Any] | None = None
    panel_fit: dict[str, Any] | None = None
    auto_target: dict[str, Any] | None = None


_latest: Capture | None = None


def _set_capture_progress(
    operation_id: str | None,
    step: int,
    message: str,
    *,
    done: bool = False,
    error: bool = False,
) -> None:
    if operation_id is None:
        return
    with _capture_progress_lock:
        _capture_progress[operation_id] = {
            "ok": not error,
            "available": True,
            "operation_id": operation_id,
            "step": step,
            "total_steps": 7,
            "message": message,
            "done": done,
            "error": error,
            "updated_monotonic": time.monotonic(),
        }
        if len(_capture_progress) > 32:
            oldest = min(
                _capture_progress,
                key=lambda key: _capture_progress[key]["updated_monotonic"],
            )
            if oldest != operation_id:
                _capture_progress.pop(oldest, None)


@app.get("/api/pointcloud/capture-progress/{operation_id}")
def capture_progress(operation_id: str):
    with _capture_progress_lock:
        progress = _capture_progress.get(operation_id)
        return (
            {"ok": True, "available": False, "operation_id": operation_id}
            if progress is None
            else dict(progress)
        )


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
            distortion = (
                archive["distortion"].astype(np.float64, copy=True)
                if "distortion" in archive.files
                else np.empty(0, dtype=np.float64)
            )
            metadata = json.loads(
                archive["metadata_json"].astype(np.uint8, copy=False).tobytes().decode("utf-8")
            )
            transform = archive["T_cam2root"].astype(np.float64, copy=True)
    except Exception as exc:
        raise RuntimeError(f"RGB-D 快照解析失败: {exc}") from exc
    if intrinsics.shape != (4,):
        raise RuntimeError(f"RGB-D 内参 shape 异常: {intrinsics.shape}")
    if distortion.size not in {0, 4, 5, 8, 12, 14}:
        raise RuntimeError(f"RGB-D 畸变参数 shape 异常: {distortion.shape}")
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
        "distortion": distortion.reshape(-1),
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
        masks = getattr(getattr(result, "masks", None), "xy", None) or []
        for index, box in enumerate(result.boxes):
            cls = int(box.cls[0])
            detection = {
                "cls": cls,
                "name": str(_names.get(cls, cls)),
                "conf": round(float(box.conf[0]), 4),
                "xyxy": [round(float(value), 2) for value in box.xyxy[0].tolist()],
            }
            if index < len(masks):
                polygon = np.asarray(masks[index], dtype=np.float32)
                if (
                    polygon.ndim == 2
                    and polygon.shape[0] >= 3
                    and polygon.shape[1] == 2
                    and np.isfinite(polygon).all()
                ):
                    detection["polygon"] = [
                        [round(float(x), 2), round(float(y), 2)]
                        for x, y in polygon
                    ]
            boxes.append(detection)
    return boxes


def _semantic_clusters(
    cloud,
    boxes: list[dict[str, Any]],
    image_shape: tuple[int, int],
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    u = cloud.pixels[:, 0]
    v = cloud.pixels[:, 1]
    for index, box in enumerate(boxes):
        inside = detection_pixel_mask(u, v, box, image_shape=image_shape)
        points = cloud.positions[inside]
        if not len(points):
            continue
        center = np.median(points, axis=0)
        clusters.append({
            "index": index,
            "cls": int(box["cls"]),
            "name": str(box.get("name") or box["cls"]),
            "conf": float(box.get("conf") or 0.0),
            "point_count": int(len(points)),
            "centroid_camera_m": [float(value) for value in center],
        })
    return clusters


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
        "model_available": _model is not None,
        "model_error": _model_error or None,
        "names": _names,
        "conf": _default_conf,
        "reach_base": _reach_base,
        "latest_capture_id": latest_id,
        "semantic_mode": (
            "yolo_instance_mask_fallback_box"
            if _model is not None
            else "manual_pointcloud_no_yolo"
        ),
    }


@app.post("/api/pointcloud/capture")
def capture(body: dict | None = None):
    global _latest
    body = body or {}
    operation_id = body.get("operation_id")
    try:
        if operation_id is not None:
            operation_id = str(operation_id)
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", operation_id):
                raise ValueError("operation_id 格式非法")
        stride = int(body.get("stride", 4))
        z_min_m = float(body.get("z_min_m", 0.15))
        z_max_m = float(body.get("z_max_m", 3.0))
        conf = float(body.get("conf", _default_conf))
        if not 0.01 <= conf <= 1.0:
            raise ValueError("conf 必须在 0.01~1.0")
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"参数非法: {exc}"}, status_code=400)

    started = time.perf_counter()
    timings: dict[str, float] = {}
    _set_capture_progress(operation_id, 1, "1/7 获取并对齐同帧 RGB-D…")
    stage_started = time.perf_counter()
    try:
        snapshot = _fetch_rgbd_snapshot()
    except Exception as exc:
        _set_capture_progress(
            operation_id, 1, f"1/7 RGB-D 获取失败：{exc}", done=True, error=True
        )
        return JSONResponse(
            {"ok": False, "error": f"无法从 reach_server 获取同帧 RGB-D: {exc}"},
            status_code=502,
        )
    timings["rgbd"] = round((time.perf_counter() - stage_started) * 1000.0, 1)
    _set_capture_progress(
        operation_id,
        2,
        f"2/7 RGB-D 已就绪（{timings['rgbd']:.1f} ms），解码彩色图像…",
    )
    stage_started = time.perf_counter()
    bgr = cv2.imdecode(
        np.frombuffer(snapshot["jpeg"], dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if bgr is None:
        _set_capture_progress(
            operation_id, 2, "2/7 JPEG 解码失败", done=True, error=True
        )
        return JSONResponse({"ok": False, "error": "JPEG 解码失败"}, status_code=502)
    timings["jpeg_decode"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 1
    )
    distortion = np.asarray(
        snapshot.get("distortion", []),
        dtype=np.float64,
    ).reshape(-1)
    inference_enabled = _model is not None
    _set_capture_progress(
        operation_id,
        3,
        (
            f"3/7 彩色图像已解码（{timings['jpeg_decode']:.1f} ms），运行 YOLO…"
            if inference_enabled
            else "3/7 未配置 YOLO 模型，跳过语义识别…"
        ),
    )
    stage_started = time.perf_counter()
    if inference_enabled:
        try:
            boxes = _infer(bgr, conf)
        except Exception as exc:
            _set_capture_progress(
                operation_id, 3, f"3/7 YOLO 失败：{exc}", done=True, error=True
            )
            return JSONResponse(
                {"ok": False, "error": f"YOLO 推理失败: {exc}"},
                status_code=500,
            )
    else:
        boxes = []
    timings["yolo"] = round((time.perf_counter() - stage_started) * 1000.0, 1)
    _set_capture_progress(
        operation_id,
        4,
        (
            f"4/7 YOLO 完成（{timings['yolo']:.1f} ms，{len(boxes)} 个目标），"
            "生成三维点云…"
            if inference_enabled
            else "4/7 无 YOLO 语义，生成可手动选点的三维点云…"
        ),
    )
    stage_started = time.perf_counter()
    try:
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
            distortion=distortion,
        )
    except Exception as exc:
        _set_capture_progress(
            operation_id, 4, f"4/7 点云生成失败：{exc}", done=True, error=True
        )
        return JSONResponse(
            {"ok": False, "error": f"点云生成失败: {exc}"},
            status_code=500,
        )
    timings["pointcloud"] = round(
        (time.perf_counter() - stage_started) * 1000.0, 1
    )
    _set_capture_progress(
        operation_id,
        5,
        f"5/7 已生成 {cloud.count:,} 点（{timings['pointcloud']:.1f} ms），编码点云数据…",
    )
    stage_started = time.perf_counter()
    try:
        binary = encode_pointcloud(cloud)
    except Exception as exc:
        _set_capture_progress(
            operation_id, 5, f"5/7 点云编码失败：{exc}", done=True, error=True
        )
        return JSONResponse(
            {"ok": False, "error": f"点云编码失败: {exc}"},
            status_code=500,
        )
    timings["encode"] = round((time.perf_counter() - stage_started) * 1000.0, 1)

    capture_id = uuid.uuid4().hex
    class_counts = {
        str(int(cls)): int(np.count_nonzero(cloud.class_ids == cls))
        for cls in np.unique(cloud.class_ids)
        if int(cls) >= 0
    }
    clusters = _semantic_clusters(cloud, boxes, bgr.shape[:2])
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
        "model_available": inference_enabled,
        "model_error": _model_error or None,
        "names": _names,
        "boxes": boxes,
        "semantic_clusters": clusters,
        "class_point_counts": class_counts,
        "intrinsics": list(snapshot["intrinsics"]),
        "distortion": distortion.tolist(),
        "distortion_compensated": bool(
            distortion.size in {4, 5, 8, 12, 14}
            and np.any(np.abs(distortion) > 1e-12)
        ),
        "mask_instance_count": sum(
            1 for box in boxes if len(box.get("polygon") or []) >= 3
        ),
        "source": snapshot["metadata"],
        "T_cam2root": snapshot["T_cam2root"],
        "capture_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "timings_ms": timings,
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
            distortion=distortion,
            created_monotonic=time.monotonic(),
            bgr=bgr,
            cloud=cloud,
            boxes=boxes,
        )
    _set_capture_progress(
        operation_id,
        5,
        f"5/7 后端完成（总计 {metadata['capture_ms']:.1f} ms），等待浏览器下载…",
        done=True,
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


@app.get("/api/pointcloud/capture/{capture_id}")
def capture_metadata(capture_id: str):
    capture_value = _capture_by_id(capture_id)
    if capture_value is None:
        return JSONResponse(
            {"ok": False, "error": "快照不存在或已被新快照替换"},
            status_code=404,
        )
    return capture_value.metadata


@app.post("/api/pointcloud/training-sample/scene-mismatch/{capture_id}")
def save_scene_mismatch_training_sample(capture_id: str, body: dict | None = None):
    """保存类别与任务预期不一致的原始图及标签，供后续补充训练。"""
    capture_value = _capture_by_id(capture_id)
    if capture_value is None:
        return JSONResponse(
            {"ok": False, "error": "快照不存在或已被新快照替换"},
            status_code=404,
        )
    body = body or {}
    observed = str(body.get("observed_scene") or "").strip()
    expected = str(body.get("expected_scene") or "").strip()
    if not observed or not expected:
        return JSONResponse(
            {"ok": False, "error": "缺少 observed_scene 或 expected_scene"},
            status_code=422,
        )

    try:
        SCENE_MISMATCH_TRAINING_DIR.mkdir(parents=True, exist_ok=True)
        stem = f"{time.strftime('%Y%m%d_%H%M%S')}_{capture_id[:8]}"
        image_path = SCENE_MISMATCH_TRAINING_DIR / f"{stem}.jpg"
        label_path = SCENE_MISMATCH_TRAINING_DIR / f"{stem}.json"
        image_path.write_bytes(capture_value.jpeg)
        label = {
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": "scene_mismatch",
            "capture_id": capture_id,
            "observed_scene": observed,
            "expected_scene": expected,
            "site": body.get("site"),
            "flip_kind": body.get("flip_kind"),
            "direction": body.get("direction"),
            "attempt": body.get("attempt"),
            "image_file": image_path.name,
            "model": capture_value.metadata.get("model"),
            "boxes": capture_value.boxes,
            "auto_target": capture_value.auto_target,
            "capture": capture_value.metadata,
        }
        label_path.write_text(
            json.dumps(label, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"训练样本保存失败: {exc}"},
            status_code=500,
        )
    return {
        "ok": True,
        "image": str(image_path),
        "label": str(label_path),
    }


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


# --------------- 选点记录：每次 confirm 存档，便于事后复查 ---------------
# 每条记录一个目录：snapshot.jpg（标注截图）+ cloud.ply（旋钮附近彩色点云，
# 内嵌粉点/算法目标/最终目的点三种颜色标记）+ meta.json（全部数值）。
# 网页画廊: GET /picks
PICK_HISTORY_DIR = ROOT / "data" / "pick_history"
PICK_HISTORY_KEEP = 500          # 最多保留条数，超出删最旧
PICK_CROP_RADIUS_M = 0.20        # 以算法目标为中心裁剪这么大一球的点云
# flip_* 文件由流程（api/flow.py）在拨动前后追加：头部判定帧、
# 腕部核验帧与 YOLO 复核结论
PICK_RECORD_FILES = ("snapshot.jpg", "cloud.ply", "meta.json",
                     "flip_before.jpg", "flip_after.jpg",
                     "flip_before_wrist.jpg", "flip_result.json")
_RECORD_NAME_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")


def _project_pixel(
    intrinsics: tuple[float, float, float, float],
    p_camera: np.ndarray,
) -> tuple[int, int] | None:
    fx, fy, cx, cy = intrinsics
    if p_camera[2] <= 1e-6:
        return None
    return (int(round(fx * p_camera[0] / p_camera[2] + cx)),
            int(round(fy * p_camera[1] / p_camera[2] + cy)))


def _marker_points(
    center: np.ndarray,
    rgb: tuple[int, int, int],
    count: int = 300,
    radius_m: float = 0.004,
) -> tuple[np.ndarray, np.ndarray]:
    """以 center 为球心撒一小团纯色点，在点云查看器里当立体标记。"""
    rng = np.random.default_rng(0)
    directions = rng.normal(size=(count, 3))
    directions /= np.linalg.norm(directions, axis=1)[:, None]
    offsets = directions * radius_m * rng.random((count, 1)) ** (1.0 / 3.0)
    positions = (np.asarray(center, dtype=np.float64)[None, :] + offsets)
    colors = np.tile(np.asarray(rgb, dtype=np.uint8), (count, 1))
    return positions.astype(np.float32), colors


def _write_ply(path: Path, positions: np.ndarray, rgb: np.ndarray) -> None:
    data = np.empty(
        positions.shape[0],
        dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
               ("red", "u1"), ("green", "u1"), ("blue", "u1")],
    )
    data["x"], data["y"], data["z"] = positions.T.astype(np.float32)
    data["red"], data["green"], data["blue"] = rgb.T.astype(np.uint8)
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {positions.shape[0]}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    )
    with open(path, "wb") as fh:
        fh.write(header.encode("ascii"))
        data.tofile(fh)


def _save_pick_record(
    capture_value: Capture,
    reference: np.ndarray,
    p_camera: np.ndarray,
    adjustment: np.ndarray,
    request_body: dict[str, Any],
    result: dict[str, Any],
) -> str | None:
    """存一条选点记录，返回记录名（失败返回 None，不影响主流程）。"""
    try:
        name = (f"{time.strftime('%Y%m%d_%H%M%S')}_"
                f"{capture_value.capture_id[:8]}")
        record_dir = PICK_HISTORY_DIR / name
        record_dir.mkdir(parents=True, exist_ok=True)

        auto = capture_value.auto_target or {}
        panel_center = (auto.get("panel_center_camera_m")
                        if auto.get("ok") else None)

        # ---- 旋钮附近彩色点云 + 三色立体标记 ----
        positions = np.asarray(capture_value.cloud.positions,
                               dtype=np.float32)
        colors = np.asarray(capture_value.cloud.rgb, dtype=np.uint8)
        keep = (np.linalg.norm(
            positions - np.asarray(reference, dtype=np.float32)[None, :],
            axis=1) <= PICK_CROP_RADIUS_M)
        parts_p = [positions[keep]]
        parts_c = [colors[keep]]
        markers = (
            (panel_center, (255, 0, 255)),   # 粉点（面板中心）：品红
            (reference, (0, 255, 0)),        # 算法/基准目标点：绿
            (p_camera, (255, 0, 0)),         # 微调后的最终目的点：红
        )
        for center, rgb in markers:
            if center is None:
                continue
            mp, mc = _marker_points(np.asarray(center, dtype=np.float64), rgb)
            parts_p.append(mp)
            parts_c.append(mc)
        _write_ply(record_dir / "cloud.ply",
                   np.vstack(parts_p), np.vstack(parts_c))

        # ---- 标注截图（BGR 画图）----
        image = capture_value.bgr.copy()
        adj_mm = [float(v) * 1000.0 for v in adjustment]
        # 流程带来的墙面系微调（右/入墙/上，mm）——比相机系分量直观
        wall_mm = request_body.get("adjustment_wall_mm")
        if not (isinstance(wall_mm, dict)
                and all(k in wall_mm for k in ("x", "y", "z"))):
            wall_mm = None
        for center, bgr, label in (
            (panel_center, (255, 0, 255), "panel"),
            (reference, (0, 255, 0), "target"),
            (p_camera, (0, 0, 255), "final"),
        ):
            if center is None:
                continue
            px = _project_pixel(capture_value.intrinsics,
                                np.asarray(center, dtype=np.float64))
            if px is None:
                continue
            cv2.drawMarker(image, px, bgr, cv2.MARKER_CROSS, 26, 2)
            cv2.circle(image, px, 10, bgr, 2)
            cv2.putText(image, label, (px[0] + 14, px[1] - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, bgr, 2)
        p_root = result.get("p_root") or []
        lines = [
            # cv2 写不了中文：R=右 U=上 I=入墙（墙面系）
            (f"adj wall(mm) R{wall_mm['x']:+g} U{wall_mm['z']:+g} "
             f"I{wall_mm['y']:+g}" if wall_mm else
             f"adj cam(mm) x{adj_mm[0]:+.1f} y{adj_mm[1]:+.1f} "
             f"z{adj_mm[2]:+.1f}"),
            (f"p_root [{p_root[0]:+.3f} {p_root[1]:+.3f} {p_root[2]:+.3f}]m"
             if len(p_root) == 3 else "p_root -"),
            f"{result.get('selection_source') or 'manual'}"
            + (f" {auto.get('matched_detection_name')}"
               f"-slot{auto.get('target_point_slot')}" if auto.get("ok") else ""),
        ]
        for i, text in enumerate(lines):
            cv2.putText(image, text, (12, 28 + 26 * i),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 0, 0), 4)
            cv2.putText(image, text, (12, 28 + 26 * i),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.62, (80, 255, 255), 2)
        cv2.imwrite(str(record_dir / "snapshot.jpg"), image,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 88])

        # ---- 全量数值 ----
        meta = {
            "saved_at": datetime_now_iso(),
            "capture_id": capture_value.capture_id,
            "selection_source": request_body.get("selection_source"),
            "model_version": request_body.get("model_version"),
            "target_point_slot": request_body.get("target_point_slot"),
            "matched_detection_name":
                request_body.get("matched_detection_name"),
            "panel_center_camera_m": panel_center,
            "reference_camera_m": [float(v) for v in reference],
            "adjustment_camera_m": [float(v) for v in adjustment],
            "adjustment_mm": adj_mm,
            "adjustment_wall_mm": ({k: float(wall_mm[k])
                                    for k in ("x", "y", "z")}
                                   if wall_mm else None),
            "base_adjustment_wall_mm":
                request_body.get("base_adjustment_wall_mm"),
            "first_round_adjustment_wall_mm":
                request_body.get("first_round_adjustment_wall_mm"),
            "flow_round": request_body.get("flow_round"),
            "final_p_camera_m": [float(v) for v in p_camera],
            "approach_offset_m": request_body.get("approach_offset_m"),
            "confirm_result": {k: result.get(k) for k in
                               ("p_root", "p_root_surface", "p_torso",
                                "offset_mode", "depth_mm")},
            "auto_target": {k: auto.get(k) for k in
                            ("target_wall_m", "panel_center_wall_m",
                             "offset_wall_m", "wall_axes_camera",
                             "panel_fit_quality")} if auto.get("ok") else None,
            "yolo_boxes": capture_value.boxes,
            "crop_radius_m": PICK_CROP_RADIUS_M,
        }
        (record_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        # ---- 只留最近 PICK_HISTORY_KEEP 条 ----
        records = sorted(
            p for p in PICK_HISTORY_DIR.iterdir()
            if p.is_dir() and _RECORD_NAME_RE.match(p.name)
        )
        for old in records[:-PICK_HISTORY_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
        return name
    except Exception as exc:
        print(f"[pointcloud] 选点记录保存失败（不影响下发）: {exc}")
        return None


def datetime_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@app.post("/api/pointcloud/auto-target/{capture_id}")
def auto_target(capture_id: str):
    """Use the frozen RGB-D frame to predict point 1 or point 3 in memory."""
    capture_value = _capture_by_id(capture_id)
    if capture_value is None:
        return JSONResponse(
            {"ok": False, "error": "快照不存在或已被新快照替换"},
            status_code=404,
        )
    with _auto_target_lock:
        if capture_value.auto_target is not None:
            return capture_value.auto_target
        started = time.perf_counter()
        timings: dict[str, float] = {}
        try:
            from .cabinet_panel_fit import analyze_yolo_mask_panel
            from .cabinet_target_finder import predict_target
            from .cabinet_wall_frame import build_wall_coordinate_frame

            wall_started = time.perf_counter()
            wall_cloud = build_pointcloud(
                capture_value.depth_mm,
                capture_value.bgr,
                capture_value.intrinsics,
                [],
                stride=3,
                z_min_m=float(capture_value.metadata["z_min_m"]),
                z_max_m=float(capture_value.metadata["z_max_m"]),
                max_points=350_000,
                dense_box_sampling=False,
                distortion=capture_value.distortion,
            )
            wall_plane = build_wall_coordinate_frame(
                wall_cloud.positions,
                wall_cloud.pixels,
                capture_value.depth_mm.shape,
                plane_threshold_m=0.008,
                stride=3,
                min_plane_points=300,
                plane_analysis_max_points=200_000,
            )
            timings["wall"] = round(
                (time.perf_counter() - wall_started) * 1000.0, 1
            )

            panel_started = time.perf_counter()
            panel_fit = analyze_yolo_mask_panel(
                capture_value.cloud,
                capture_value.boxes,
                image_shape=capture_value.depth_mm.shape,
                wall_plane=wall_plane,
            )
            timings["panel"] = round(
                (time.perf_counter() - panel_started) * 1000.0, 1
            )
            if not panel_fit.get("available"):
                raise ValueError(
                    "YOLO Mask 面板拟合失败"
                    + (
                        f"：{panel_fit.get('reason')}"
                        if panel_fit.get("reason")
                        else ""
                    )
                )

            predict_started = time.perf_counter()
            prediction = predict_target(panel_fit, wall_plane)
            timings["predict"] = round(
                (time.perf_counter() - predict_started) * 1000.0, 1
            )
            timings["total"] = round(
                (time.perf_counter() - started) * 1000.0, 1
            )
            result = {
                "ok": True,
                **prediction,
                "panel_center_camera_m": panel_fit[
                    "rectangle_center_camera_m"
                ],
                "wall_axes_camera": [
                    wall_plane["x_axis_camera"],
                    wall_plane["y_axis_camera"],
                    wall_plane["z_axis_camera"],
                ],
                "wall_coordinate": wall_plane,
                "panel_fit": panel_fit,
                "timings_ms": timings,
            }
        except (TypeError, ValueError, np.linalg.LinAlgError) as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": str(exc),
                    "model_version": "0.2.0-s",
                    "timings_ms": {
                        **timings,
                        "total": round(
                            (time.perf_counter() - started) * 1000.0, 1
                        ),
                    },
                },
                status_code=422,
            )
        except Exception as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"算法找点失败: {exc}",
                    "model_version": "0.2.0-s",
                    "timings_ms": {
                        **timings,
                        "total": round(
                            (time.perf_counter() - started) * 1000.0, 1
                        ),
                    },
                },
                status_code=500,
            )
        with _capture_lock:
            if _latest is capture_value:
                capture_value.wall_plane = wall_plane
                capture_value.panel_fit = panel_fit
                capture_value.auto_target = result
        return result


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
            distortion=capture_value.distortion,
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
            distortion=capture_value.distortion,
        )
        # 语义点云已经建立了稳定的柜面坐标系（X 正方向=右）。局部平面仍
        # 负责接近法向，但横移轴直接沿用柜面 X，避免用世界 Z×法向推导时
        # 受躯干倾斜影响而把“右移”算成右上。
        wall_axes = (capture_value.auto_target or {}).get("wall_axes_camera")
        if plane is not None and isinstance(wall_axes, list) and wall_axes:
            wall_x = np.asarray(wall_axes[0], dtype=float).reshape(3)
            wall_x_norm = float(np.linalg.norm(wall_x))
            if np.isfinite(wall_x).all() and wall_x_norm > 1e-6:
                plane = dict(plane)
                plane["x_axis_camera"] = (wall_x / wall_x_norm).tolist()
                if len(wall_axes) >= 3:
                    wall_z = np.asarray(wall_axes[2], dtype=float).reshape(3)
                    wall_z_norm = float(np.linalg.norm(wall_z))
                    if np.isfinite(wall_z).all() and wall_z_norm > 1e-6:
                        plane["z_axis_camera"] = (
                            wall_z / wall_z_norm
                        ).tolist()
                plane["axis_source"] = "wall_coordinate_x"
        request_body = {
            "p_camera_surface": p_camera.tolist(),
            "pixel": body.get("pixel"),
            "adjustment_camera_m": adjustment.tolist(),
            "approach_offset_m": float(body.get("approach_offset_m", 0.0)),
            "plane": plane,
            "source_frame_id": capture_value.metadata.get("source", {}).get(
                "frame_id"
            ),
            "capture_id": capture_id,
        }
        selection_source = str(body.get("selection_source") or "manual")
        model_version = body.get("model_version")
        matched_detection_name = body.get("matched_detection_name")
        target_point_slot = body.get("target_point_slot")
        if len(selection_source) > 80:
            raise ValueError("selection_source 过长")
        if model_version is not None:
            model_version = str(model_version)
            if len(model_version) > 40:
                raise ValueError("model_version 过长")
        if matched_detection_name is not None:
            matched_detection_name = str(matched_detection_name)
            if len(matched_detection_name) > 40:
                raise ValueError("matched_detection_name 过长")
        if target_point_slot is not None:
            target_point_slot = int(target_point_slot)
            if target_point_slot not in {1, 3}:
                raise ValueError("target_point_slot 仅支持 1 或 3")
        # 墙面系原始微调量（右x/入墙y/上z，mm）：flow 和网页都可能带，
        # 存档后比相机系分量直观
        adjustment_wall_mm = body.get("adjustment_wall_mm")
        if adjustment_wall_mm is not None:
            if not (isinstance(adjustment_wall_mm, dict)
                    and all(k in adjustment_wall_mm for k in ("x", "y", "z"))):
                raise ValueError("adjustment_wall_mm 需为含 x/y/z 的对象")
            adjustment_wall_mm = {k: float(adjustment_wall_mm[k])
                                  for k in ("x", "y", "z")}
            if not all(np.isfinite(v) for v in adjustment_wall_mm.values()):
                raise ValueError("adjustment_wall_mm 包含非有限数值")
        extra_wall_offsets = {}
        for key in (
            "base_adjustment_wall_mm",
            "first_round_adjustment_wall_mm",
        ):
            value = body.get(key)
            if value is not None:
                if not (
                    isinstance(value, dict)
                    and all(axis in value for axis in ("x", "y", "z"))
                ):
                    raise ValueError(f"{key} 需为含 x/y/z 的对象")
                value = {
                    axis: float(value[axis]) for axis in ("x", "y", "z")
                }
                if not all(np.isfinite(item) for item in value.values()):
                    raise ValueError(f"{key} 包含非有限数值")
            extra_wall_offsets[key] = value
        flow_round = body.get("flow_round")
        if flow_round is not None:
            flow_round = int(flow_round)
            if flow_round < 1:
                raise ValueError("flow_round 必须大于等于 1")
        request_body.update(
            {
                "selection_source": selection_source,
                "model_version": model_version,
                "target_point_slot": target_point_slot,
                "matched_detection_name": matched_detection_name,
                "adjustment_wall_mm": adjustment_wall_mm,
                **extra_wall_offsets,
                "flow_round": flow_round,
            }
        )
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
    record = _save_pick_record(
        capture_value, reference, p_camera, adjustment, request_body, result
    )
    if record:
        result["record"] = record
        result["record_url"] = f"/picks#{record}"
        # 18001 先确认三维目标，7005 随后才生成记录名；回写后，手动横移
        # 才能把拨动前后证据追加到同一条 pick_history 记录。
        try:
            attached = _http.post(
                f"{_reach_base}/api/reach/attach_pick_record",
                json={"capture_id": capture_id, "record": record},
                timeout=(2.0, 5.0),
            ).json()
            if attached.get("ok") and attached.get("revision") is not None:
                result["revision"] = attached["revision"]
        except (requests.RequestException, ValueError):
            pass
    with _capture_lock:
        if _latest is capture_value:
            capture_value.metadata["confirmed_selection"] = {
                "base_camera": reference.tolist(),
                "p_camera": p_camera.tolist(),
                "pixel": body.get("pixel"),
                "adjustment": adjustment.tolist(),
                "selection_source": selection_source,
                "model_version": model_version,
                "target_point_slot": target_point_slot,
                "matched_detection_name": matched_detection_name,
                "result": result,
            }
    return result


def _list_pick_records(limit: int = 100) -> list[dict[str, Any]]:
    if not PICK_HISTORY_DIR.is_dir():
        return []
    names = sorted(
        (p.name for p in PICK_HISTORY_DIR.iterdir()
         if p.is_dir() and _RECORD_NAME_RE.match(p.name)),
        reverse=True,
    )[:limit]
    records = []
    for name in names:
        meta: dict[str, Any] = {}
        meta_path = PICK_HISTORY_DIR / name / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        records.append({"name": name, "meta": meta})
    return records


@app.get("/api/pointcloud/picks")
def picks_list(limit: int = 100):
    return {"ok": True, "records": _list_pick_records(max(1, min(500, limit)))}


@app.get("/api/pointcloud/picks/{name}/{filename}")
def picks_file(name: str, filename: str):
    if not _RECORD_NAME_RE.match(name) or filename not in PICK_RECORD_FILES:
        return JSONResponse({"ok": False, "error": "非法记录名或文件名"},
                            status_code=400)
    path = PICK_HISTORY_DIR / name / filename
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "记录不存在"},
                            status_code=404)
    return FileResponse(path)


@app.get("/picks")
def picks_page():
    """选点记录画廊：标注截图 + 点云/数值下载链接，最新在前。"""
    rows = []
    for record in _list_pick_records():
        name, meta = record["name"], record["meta"]
        base = f"/api/pointcloud/picks/{name}"
        adj = meta.get("adjustment_mm") or [0, 0, 0]
        wall = meta.get("adjustment_wall_mm")
        if isinstance(wall, dict) and all(k in wall for k in ("x", "y", "z")):
            # 流程下发的记录带墙面系原始值：按人填的「右/上/入墙」显示
            adj_text = (f"微调·墙面系(mm) 右{wall['x']:+g} / "
                        f"上{wall['z']:+g} / 入墙{wall['y']:+g}")
        else:
            adj_text = (f"微调·相机系(mm) [{adj[0]:+.1f}, {adj[1]:+.1f}, "
                        f"{adj[2]:+.1f}]")
        p_root = (meta.get("confirm_result") or {}).get("p_root") or []
        summary = " · ".join(filter(None, [
            str(meta.get("saved_at") or name),
            str(meta.get("selection_source") or ""),
            (f"{meta.get('matched_detection_name')}·点"
             f"{meta.get('target_point_slot')}"
             if meta.get("target_point_slot") else ""),
            adj_text,
            (f"p_root [{p_root[0]:+.3f}, {p_root[1]:+.3f}, "
             f"{p_root[2]:+.3f}] m" if len(p_root) == 3 else ""),
        ]))
        rows.append(
            f'<figure id="{name}"><a href="{base}/snapshot.jpg" target="_blank">'
            f'<img src="{base}/snapshot.jpg" loading="lazy" /></a>'
            f"<figcaption>{summary}<br/>"
            f'<a href="{base}/cloud.ply">cloud.ply（旋钮附近彩色点云，'
            f"品红=粉点 绿=算法目标 红=最终目的点）</a> · "
            f'<a href="{base}/meta.json" target="_blank">meta.json</a>'
            f"</figcaption></figure>"
        )
    body = "\n".join(rows) or "<p>还没有选点记录：每次确认下发后自动保存。</p>"
    html = (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'/>"
        "<title>选点记录</title><style>"
        "body{background:#0c1220;color:#dce7f5;font:14px/1.6 sans-serif;"
        "margin:24px}h1{font-size:18px}figure{margin:0 0 26px;padding:14px;"
        "background:#131f33;border-radius:12px}img{max-width:100%;"
        "border-radius:8px}figcaption{margin-top:8px;color:#9fb4cc}"
        "a{color:#6fd3c7}</style></head><body>"
        f"<h1>选点记录（最新在前，最多保留 {PICK_HISTORY_KEEP} 条）</h1>"
        f"{body}</body></html>"
    )
    return Response(html, media_type="text/html; charset=utf-8")


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
    global _reach_base, _model, _model_name, _model_error, _names, _default_conf
    global _capability_snapshot
    import uvicorn

    parser = argparse.ArgumentParser(description="RGB/YOLO语义点云查看器（7005）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7005)
    parser.add_argument("--reach-base", default="http://127.0.0.1:18001")
    parser.add_argument("--model", default="models/Xuanniu.pt")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--capability-url", default=DEFAULT_CAPABILITY_URL,
                        help="18000 能力中心地址（启动拜访，必须可达）")
    args = parser.parse_args()
    _reach_base = args.reach_base.rstrip("/")
    _default_conf = args.conf

    # 启动拜访 18000：确认能力中心可达并留存快照（后续按配置区分行为用）。
    try:
        _capability_snapshot = fetch_snapshot(args.capability_url)
    except CapabilityUnavailable as exc:
        print(f"[pointcloud] 启动拜访 18000 失败：{exc}")
        raise SystemExit(1)
    print(f"[pointcloud] 18000 {describe_active(_capability_snapshot)}")

    model_path = Path(args.model)
    _model_name = model_path.name
    if model_path.is_file():
        _model_error = ""
        from ultralytics import YOLO

        started = time.perf_counter()
        _model = YOLO(str(model_path))
        _names = {int(key): str(value) for key, value in (_model.names or {}).items()}
        _model.predict(np.zeros((720, 1280, 3), dtype=np.uint8),
                       conf=_default_conf, verbose=False)
        print(
            f"[pointcloud] 模型 {_model_name} 加载+预热完成"
            f"（{time.perf_counter() - started:.1f}s），类别: {_names}"
        )
    else:
        _model = None
        _names = {}
        _model_error = f"YOLO 模型不存在: {model_path}"
        print(f"[pointcloud] ⚠ {_model_error}；以无语义手动选点模式启动")
    print(f"[pointcloud] RGB-D 来源: {_reach_base}/api/reach/rgbd_snapshot")
    print(f"[pointcloud] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
