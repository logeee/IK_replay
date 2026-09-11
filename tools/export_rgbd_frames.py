#!/usr/bin/env python3
"""从 18001 抓头部 RGB-D 帧，按 orbbec_rgbd_collector 数据集格式落盘。

用途：给 ``panel_anchor`` 自动选点模型采标定帧。落盘目录可以直接 rsync
到 Mac 的 ``orbbec_rgbd_collector/datasets/`` 下，用它的 17002 点云页面在
三维点云里点选点 1 / 点 3 并保存 ``annotations.jsonl``；再拷回来交给
``tools/calibrate_panel_anchor.py`` 算偏移。

目录结构（与 Mac 端 ``rgbd_collector.storage.DatasetSession`` 一致）::

    data/calibration_datasets/<YYYYmmdd_HHMMSS>_<name>/
    ├── session.json          camera.color.intrinsics / distortion（17002 必读）
    ├── manifest.jsonl        每帧一行
    └── frames/<000001_<host_time_ns>>/
        ├── color.jpg
        ├── depth_aligned.png uint16，mm（depth_scale 1.0），已对齐到 color
        ├── depth_raw.png     同 depth_aligned（18001 只给对齐深度，占位保证兼容）
        ├── frame.json
        └── yolo_boxes.json   （--model 时）7005 同款 YOLO 框 + mask，标定脚本复用

用法::

    python tools/export_rgbd_frames.py --name panel_calib            # 交互：回车拍一帧，q 退出
    python tools/export_rgbd_frames.py --name panel_calib --count 10 --interval 2
    python tools/export_rgbd_frames.py --name panel_calib --model models/Xuanniu_hhy.pt

采集建议：面板**完整入镜、离图像四边留余量**；旋钮左/右两种状态都拍；
机器人站位 / 相机俯仰稍作变化多拍几帧（≥ 8 帧每种状态）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_ROOT = ROOT / "data" / "calibration_datasets"


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


def fetch_snapshot(reach_base: str, timeout_s: float = 20.0) -> dict[str, Any]:
    import requests

    response = requests.get(f"{reach_base}/api/reach/rgbd_snapshot",
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


class ExportSession:
    def __init__(self, out_root: Path, name: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = f"{stamp}_{_safe_name(name)}"
        self.name = name
        self.path = out_root / self.session_id
        self.frames_path = self.path / "frames"
        self.frames_path.mkdir(parents=True, exist_ok=False)
        self.manifest_path = self.path / "manifest.jsonl"
        self.sequence = 0
        self.session_written = False

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
            "generator": "IK_replay/tools/export_rgbd_frames.py",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", default="panel_calib", help="会话名（目录后缀）")
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--reach-base", default="http://127.0.0.1:18001")
    parser.add_argument("--count", type=int, default=0,
                        help="非交互：连拍 N 帧后退出（0 = 交互模式）")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="连拍间隔秒")
    parser.add_argument("--model", default=None,
                        help="可选 YOLO .pt（如 models/Xuanniu_hhy.pt），"
                             "有则每帧保存 yolo_boxes.json 供标定脚本复用")
    parser.add_argument("--conf", type=float, default=0.25)
    args = parser.parse_args()

    model = None
    if args.model:
        model_path = Path(args.model)
        if not model_path.is_file():
            print(f"[export] YOLO 模型不存在: {model_path}", file=sys.stderr)
            return 1
        from ultralytics import YOLO

        model = YOLO(str(model_path))
        print(f"[export] YOLO {model_path.name} 已加载，类别 {model.names}")

    session = ExportSession(args.out_root, args.name)
    print(f"[export] 会话目录: {session.path}")
    print(f"[export] RGB-D 来源: {args.reach_base}/api/reach/rgbd_snapshot")

    def capture_one(trigger: str) -> bool:
        try:
            snapshot = fetch_snapshot(args.reach_base)
            boxes = None
            if model is not None:
                bgr = cv2.imdecode(np.frombuffer(snapshot["jpeg"], np.uint8),
                                   cv2.IMREAD_COLOR)
                boxes = run_yolo(model, bgr, args.conf)
            record = session.save(snapshot, boxes, trigger)
        except Exception as exc:
            print(f"[export] ✗ 采集失败: {exc}")
            return False
        summary = ""
        if record.get("yolo_summary") is not None:
            summary = "  YOLO: " + (", ".join(record["yolo_summary"]) or "无")
        print(f"[export] ✓ 第 {record['sequence']} 帧 {record['frame_id']}  "
              f"深度有效 {record['depth_aligned']['valid_ratio']:.1%}{summary}")
        return True

    if args.count > 0:
        for index in range(args.count):
            capture_one("timer")
            if index < args.count - 1:
                time.sleep(max(0.0, args.interval))
    else:
        print("[export] 交互模式：回车拍一帧，输入 q 回车退出")
        while True:
            try:
                line = input("> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if line in {"q", "quit", "exit"}:
                break
            capture_one("manual")
    print(f"[export] 共 {session.sequence} 帧 → {session.path}")
    print("[export] 下一步：rsync 到 Mac 的 orbbec_rgbd_collector/datasets/ 下，"
          "用 17002 页面点选点 1/点 3 并保存；再把 annotations.jsonl 拷回该目录，"
          "运行 tools/calibrate_panel_anchor.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
