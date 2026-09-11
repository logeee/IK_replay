"""RGB-D 标定数据集：从 18001 抓帧、按 orbbec_rgbd_collector 格式落盘、点选标注。

7003 采集台（``api.yolo_collect``）的 RGB-D 模式与 ``tools/export_rgbd_frames.py``
共用这里的实现；``tools/calibrate_panel_anchor.py`` 读这里写出的目录。

目录结构（与 Mac 端 ``rgbd_collector.storage.DatasetSession`` 一致，可直接
rsync 过去用 18006 点云页面点选）::

    data/calibration_datasets/<YYYYmmdd_HHMMSS>_<name>/
    ├── session.json          camera.color.intrinsics / distortion
    ├── manifest.jsonl        每帧一行
    ├── annotations.jsonl     点选结果（v3：每帧一行，points.{slot}.target_camera_m）
    └── frames/<000001_<host_time_ns>>/
        ├── color.jpg
        ├── depth_aligned.png uint16，mm（depth_scale 1.0），已对齐到 color
        ├── depth_raw.png     同 depth_aligned（18001 只给对齐深度，占位保证兼容）
        ├── frame.json
        └── yolo_boxes.json   7005 同款 YOLO 框 + mask，标定脚本复用
"""
from __future__ import annotations

import io
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from api.pointcloud_core import _normalized_pixels

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_ROOT = ROOT / "data" / "calibration_datasets"
ANNOTATION_SCHEMA = "rgbd-target-annotation/v3"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._")
    return cleaned[:64] or "session"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------- 抓帧 / 推理
def fetch_snapshot(reach_base: str, timeout_s: float = 20.0) -> dict[str, Any]:
    """18001 ``/api/reach/rgbd_snapshot`` npz → dict（jpeg / depth_mm / 内参 …）。"""
    import requests

    session = requests.Session()
    session.trust_env = False      # 本机服务，不走系统代理
    response = session.get(f"{reach_base}/api/reach/rgbd_snapshot",
                           timeout=(3.0, timeout_s))
    response.raise_for_status()
    with np.load(io.BytesIO(response.content), allow_pickle=False) as archive:
        snapshot = {
            "jpeg": archive["jpeg"].astype(np.uint8).tobytes(),
            "depth_mm": archive["depth_mm"].astype(np.float32),
            "intrinsics": archive["intrinsics"].astype(np.float64).reshape(-1),
            "distortion": (archive["distortion"].astype(np.float64).reshape(-1)
                           if "distortion" in archive.files
                           else np.empty(0)),
            "T_cam2root": (archive["T_cam2root"].astype(np.float64)
                           if "T_cam2root" in archive.files else None),
            "metadata": json.loads(
                archive["metadata_json"].astype(np.uint8).tobytes()
                .decode("utf-8")),
        }
    if snapshot["intrinsics"].shape != (4,):
        raise RuntimeError(f"内参 shape 异常: {snapshot['intrinsics'].shape}")
    return snapshot


def run_yolo(model, bgr: np.ndarray, conf: float) -> list[dict[str, Any]]:
    """与 7005 ``pointcloud_viewer._infer`` 同款输出（cls/name/conf/xyxy/polygon）。"""
    names = {int(k): str(v) for k, v in (model.names or {}).items()}
    boxes: list[dict[str, Any]] = []
    for result in model.predict(bgr, conf=conf, verbose=False):
        if result.boxes is None:
            continue
        masks = getattr(getattr(result, "masks", None), "xy", None) or []
        for index, box in enumerate(result.boxes):
            cls = int(box.cls[0])
            detection: dict[str, Any] = {
                "cls": cls,
                "name": names.get(cls, str(cls)),
                "conf": round(float(box.conf[0]), 4),
                "xyxy": [round(float(v), 2) for v in box.xyxy[0].tolist()],
            }
            if index < len(masks):
                polygon = np.asarray(masks[index], dtype=np.float32)
                if (polygon.ndim == 2 and polygon.shape[0] >= 3
                        and polygon.shape[1] == 2 and np.isfinite(polygon).all()):
                    detection["polygon"] = [
                        [round(float(x), 2), round(float(y), 2)] for x, y in polygon]
            boxes.append(detection)
    return boxes


# ----------------------------------------------------------------- 会话落盘
class ExportSession:
    """一个数据集目录。``ExportSession(root, name)`` 新建；``open(path)`` 续写。"""

    def __init__(self, out_root: Path, name: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = f"{stamp}_{_safe_name(name)}"
        self.name = name
        self.path = Path(out_root) / self.session_id
        self.frames_path = self.path / "frames"
        self.frames_path.mkdir(parents=True, exist_ok=False)
        self.manifest_path = self.path / "manifest.jsonl"
        self.sequence = 0
        self.session_written = False

    @classmethod
    def open(cls, path: Path) -> "ExportSession":
        path = Path(path)
        if not (path / "frames").is_dir():
            raise FileNotFoundError(f"不是数据集目录（没有 frames/）: {path}")
        self = cls.__new__(cls)
        self.path = path
        self.session_id = path.name
        self.frames_path = path / "frames"
        self.manifest_path = path / "manifest.jsonl"
        self.session_written = (path / "session.json").is_file()
        self.name = path.name
        if self.session_written:
            try:
                meta = json.loads((path / "session.json").read_text(encoding="utf-8"))
                self.name = str(meta.get("session_name") or path.name)
            except (OSError, json.JSONDecodeError):
                pass
        self.sequence = 0
        for record in self.manifest_records():
            self.sequence = max(self.sequence, int(record.get("sequence") or 0))
        return self

    def manifest_records(self) -> list[dict[str, Any]]:
        if not self.manifest_path.is_file():
            return []
        out = []
        for raw in self.manifest_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return out

    def _write_session_json(self, snapshot: dict[str, Any], bgr_shape) -> None:
        fx, fy, cx, cy = [float(v) for v in snapshot["intrinsics"]]
        height, width = int(bgr_shape[0]), int(bgr_shape[1])
        distortion = [float(v) for v in snapshot["distortion"]]
        order = ("k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6")
        payload = {
            "schema_version": 1,
            "session_id": self.session_id,
            "session_name": self.name,
            "created_at": _utc_now(),
            "generator": "IK_replay/api/rgbd_dataset.py",
            "camera": {
                "sdk": {"package": "IK_replay reach_server rgbd_snapshot"},
                "device": {
                    "name": "robot head RGB-D (via 18001)",
                    "serial": str((snapshot["metadata"] or {}).get("serial")
                                  or (snapshot["metadata"] or {}).get("camera")
                                  or ""),
                },
                "color": {
                    "width": width, "height": height,
                    "intrinsics": {"width": width, "height": height,
                                   "fx": fx, "fy": fy, "cx": cx, "cy": cy},
                    "distortion": {
                        "model": "brown_conrady",
                        "coefficients": distortion,
                        "coefficient_order": list(order[:len(distortion)]),
                    },
                },
                "depth": {"note": "18001 只输出已对齐到彩色的深度；"
                                  "depth_raw.png 与 depth_aligned.png 相同"},
                "alignment": {"method": "reach_server aligned depth",
                              "target_stream": "COLOR_STREAM"},
                "depth_scale": {"value": 1.0, "unit": "mm_per_raw_unit"},
                "T_cam2root": (snapshot["T_cam2root"].tolist()
                               if snapshot["T_cam2root"] is not None else None),
                "source_metadata": snapshot["metadata"],
            },
            "storage": {
                "color": {"file": "color.jpg", "encoding": "JPEG"},
                "depth_raw": {"file": "depth_raw.png", "encoding": "PNG",
                              "dtype": "uint16", "geometry": "aligned_to_color"},
                "depth_aligned": {"file": "depth_aligned.png", "encoding": "PNG",
                                  "dtype": "uint16", "geometry": "aligned_to_color"},
                "manifest": "manifest.jsonl",
            },
        }
        _write_json(self.path / "session.json", payload)
        self.session_written = True

    def save(self, snapshot: dict[str, Any], boxes: list[dict[str, Any]] | None,
             trigger: str) -> dict[str, Any]:
        bgr = cv2.imdecode(np.frombuffer(snapshot["jpeg"], np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError("JPEG 解码失败")
        depth = np.asarray(snapshot["depth_mm"], dtype=np.float32)
        if depth.shape != bgr.shape[:2]:
            raise RuntimeError(
                f"深度 {depth.shape} 与彩色 {bgr.shape[:2]} 尺寸不一致，"
                "不是对齐深度")
        depth_u16 = np.where(np.isfinite(depth), depth, 0.0)
        depth_u16 = np.clip(np.rint(depth_u16), 0, 65535).astype(np.uint16)
        if not self.session_written:
            self._write_session_json(snapshot, bgr.shape)

        self.sequence += 1
        host_time_ns = time.time_ns()
        frame_id = f"{self.sequence:06d}_{host_time_ns}"
        frame_dir = self.frames_path / frame_id
        frame_dir.mkdir()
        if not cv2.imwrite(str(frame_dir / "color.jpg"), bgr,
                           [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise OSError("写 color.jpg 失败")
        for name in ("depth_aligned.png", "depth_raw.png"):
            if not cv2.imwrite(str(frame_dir / name), depth_u16,
                               [cv2.IMWRITE_PNG_COMPRESSION, 3]):
                raise OSError(f"写 {name} 失败")
        valid = int(np.count_nonzero(depth_u16))
        record = {
            "schema_version": 1,
            "session_id": self.session_id,
            "frame_id": frame_id,
            "sequence": self.sequence,
            "trigger": trigger,
            "queued_at": _utc_now(),
            "saved_at": _utc_now(),
            "files": {
                "color": f"frames/{frame_id}/color.jpg",
                "depth_raw": f"frames/{frame_id}/depth_raw.png",
                "depth_aligned": f"frames/{frame_id}/depth_aligned.png",
                "metadata": f"frames/{frame_id}/frame.json",
            },
            "host_time_ns": host_time_ns,
            "depth_scale": {"value": 1.0, "unit": "mm_per_raw_unit"},
            "color": {"width": int(bgr.shape[1]), "height": int(bgr.shape[0]),
                      "dtype": "uint8"},
            "depth_aligned": {
                "width": int(depth_u16.shape[1]), "height": int(depth_u16.shape[0]),
                "dtype": "uint16",
                "valid_pixels": valid,
                "valid_ratio": round(valid / depth_u16.size, 6),
                "min_raw": int(depth_u16[depth_u16 > 0].min()) if valid else 0,
                "max_raw": int(depth_u16.max()),
            },
            "T_cam2root": (snapshot["T_cam2root"].tolist()
                           if snapshot["T_cam2root"] is not None else None),
            "source_metadata": snapshot["metadata"],
        }
        record["depth_raw"] = record["depth_aligned"]
        if boxes is not None:
            record["files"]["yolo_boxes"] = f"frames/{frame_id}/yolo_boxes.json"
            record["yolo_summary"] = [
                f"{b['name']}({b['conf']:.2f}{',mask' if 'polygon' in b else ''})"
                for b in boxes]
            _write_json(frame_dir / "yolo_boxes.json", {"boxes": boxes})
        _write_json(frame_dir / "frame.json", record)
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    # ------------------------------------------------------------- 读取
    def load_frame(self, frame_id: str) -> dict[str, Any]:
        """读回一帧：bgr / depth_mm(float32) / intrinsics / distortion / boxes。"""
        if "/" in frame_id or "\\" in frame_id or ".." in frame_id:
            raise ValueError("非法 frame_id")
        frame_dir = self.frames_path / frame_id
        frame_meta = json.loads((frame_dir / "frame.json").read_text(encoding="utf-8"))
        bgr = cv2.imread(str(frame_dir / "color.jpg"), cv2.IMREAD_COLOR)
        depth_raw = cv2.imread(str(frame_dir / "depth_aligned.png"), cv2.IMREAD_UNCHANGED)
        if bgr is None or depth_raw is None:
            raise FileNotFoundError(f"{frame_id}: color.jpg / depth_aligned.png 读取失败")
        if depth_raw.dtype != np.uint16 or depth_raw.shape != bgr.shape[:2]:
            raise ValueError(f"{frame_id}: 对齐深度格式异常 {depth_raw.dtype}/{depth_raw.shape}")
        scale = float(frame_meta.get("depth_scale", {}).get("value", 1.0))
        session = json.loads((self.path / "session.json").read_text(encoding="utf-8"))
        color = session["camera"]["color"]
        intr = color["intrinsics"]
        boxes = None
        boxes_file = frame_dir / "yolo_boxes.json"
        if boxes_file.is_file():
            boxes = json.loads(boxes_file.read_text(encoding="utf-8")).get("boxes")
        return {
            "frame_id": frame_id,
            "bgr": bgr,
            "depth_mm": depth_raw.astype(np.float32) * np.float32(scale),
            "intrinsics": (float(intr["fx"]), float(intr["fy"]),
                           float(intr["cx"]), float(intr["cy"])),
            "distortion": np.asarray(
                (color.get("distortion") or {}).get("coefficients") or [],
                dtype=np.float64),
            "boxes": boxes,
            "meta": frame_meta,
        }

    # ------------------------------------------------------------- 标注
    @property
    def annotations_path(self) -> Path:
        return self.path / "annotations.jsonl"

    def load_annotations(self) -> dict[str, dict[str, Any]]:
        """frame_id → 该帧最后一条 v3 记录（points 字典）。"""
        out: dict[str, dict[str, Any]] = {}
        if not self.annotations_path.is_file():
            return out
        for raw in self.annotations_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            frame_id = record.get("frame_id")
            if isinstance(frame_id, str) and isinstance(record.get("points"), dict):
                out[frame_id] = record
        return out

    def _rewrite_annotations(self, records: dict[str, dict[str, Any]]) -> None:
        tmp = self.annotations_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for frame_id in sorted(records):
                handle.write(json.dumps(records[frame_id], ensure_ascii=False) + "\n")
        os.replace(tmp, self.annotations_path)

    def annotate(self, frame_id: str, slot: int, u: float, v: float,
                 window_px: int = 5) -> dict[str, Any]:
        """把点击像素 (u, v) 反投影成相机系 3D 点写入 annotations.jsonl。

        深度取以 (u, v) 为中心 ``window_px`` 方窗内有效深度的中位数（抗单像素
        噪声/空洞），像素→归一化坐标与 7005 建点云走同一函数（含畸变）。
        同帧同点位重复标注覆盖旧值；其他点位保留。
        """
        frame = self.load_frame(frame_id)
        depth = frame["depth_mm"]
        height, width = depth.shape
        ui, vi = int(round(u)), int(round(v))
        if not (0 <= ui < width and 0 <= vi < height):
            raise ValueError(f"像素 ({ui},{vi}) 超出图像 {width}x{height}")
        half = max(0, int(window_px) // 2)
        patch = depth[max(0, vi - half):vi + half + 1, max(0, ui - half):ui + half + 1]
        valid = patch[np.isfinite(patch) & (patch > 0)]
        if valid.size == 0:
            raise ValueError(f"像素 ({ui},{vi}) 附近 {window_px}px 窗内没有有效深度，换个位置点")
        depth_m = float(np.median(valid)) / 1000.0
        normalized = _normalized_pixels(
            np.asarray([float(u)]), np.asarray([float(v)]),
            frame["intrinsics"], frame["distortion"])[0]
        target = np.array([normalized[0] * depth_m, normalized[1] * depth_m, depth_m])

        records = self.load_annotations()
        record = records.get(frame_id) or {
            "schema": ANNOTATION_SCHEMA,
            "session_id": self.session_id,
            "frame_id": frame_id,
            "points": {},
        }
        record["updated_at"] = _utc_now()
        record["points"][str(int(slot))] = {
            "point_slot": int(slot),
            "target_camera_m": [round(float(x), 6) for x in target],
            "pixel": [ui, vi],
            "depth_mm": round(depth_m * 1000.0, 2),
            "depth_window_px": int(window_px),
            "depth_window_valid": int(valid.size),
            "depth_window_spread_mm": round(float(valid.max() - valid.min()), 2),
            "source": "yolo_collect-7003",
        }
        records[frame_id] = record
        self._rewrite_annotations(records)
        return record["points"][str(int(slot))]

    def remove_annotation(self, frame_id: str, slot: int) -> None:
        records = self.load_annotations()
        record = records.get(frame_id)
        if not record:
            return
        record["points"].pop(str(int(slot)), None)
        if record["points"]:
            record["updated_at"] = _utc_now()
        else:
            records.pop(frame_id)
        self._rewrite_annotations(records)


def list_sessions(root: Path) -> list[dict[str, Any]]:
    """已有数据集目录（按名字倒序 = 时间倒序），带帧数/标注数。"""
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.iterdir(), reverse=True):
        if not (path / "frames").is_dir():
            continue
        try:
            session = ExportSession.open(path)
            frames = session.manifest_records()
            annotated = session.load_annotations()
        except Exception:
            continue
        out.append({
            "session_id": path.name,
            "name": session.name,
            "frames": len(frames),
            "annotated_frames": len(annotated),
            "points": sum(len(r.get("points") or {}) for r in annotated.values()),
        })
    return out
