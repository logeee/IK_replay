#!/usr/bin/env python3
"""命令行版 RGB-D 标定帧采集（无界面备用）。

日常请用 7003 采集台的「RGB-D 标定」模式（``python -m api.yolo_collect``），
网页里拍帧 + 直接点选点 1 / 点 3。本脚本只是没有浏览器时的备用入口，
落盘格式与 7003 完全一致（见 ``api/rgbd_dataset.py``），也可以 rsync 到
Mac 的 orbbec_rgbd_collector/datasets/ 下用 18006 点云页面点选。

用法::

    python tools/export_rgbd_frames.py --name panel_calib            # 交互：回车拍一帧，q 退出
    python tools/export_rgbd_frames.py --name panel_calib --count 10 --interval 2
    python tools/export_rgbd_frames.py --name panel_calib --model models/Xuanniu_hhy.pt

采集建议：面板**完整入镜、离图像四边留余量**；旋钮左/右两种状态都拍；
机器人站位 / 相机俯仰稍作变化多拍几帧（≥ 8 帧每种状态）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.rgbd_dataset import (  # noqa: E402
    DEFAULT_DATASET_ROOT,
    ExportSession,
    fetch_snapshot,
    run_yolo,
)

OUT_ROOT = DEFAULT_DATASET_ROOT


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
    print("[export] 下一步：在 7003 采集台「RGB-D 标定」里选中该会话补标注，"
          "或 rsync 到 Mac 用 18006 点选；然后运行 tools/calibrate_panel_anchor.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
