#!/usr/bin/env python3
"""Compare software alignment with a one-time SDK AlignFilter reference."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from camera_sources.alignment import RGBDCalibration, SoftwareDepthAligner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="离线比较原始 Z16 软件对齐与 SDK AlignFilter 输出（均为 .npy）"
    )
    parser.add_argument("--raw-depth", type=Path, required=True,
                        help="未对齐 uint16 Z16 .npy")
    parser.add_argument("--sdk-aligned", type=Path, required=True,
                        help="SDK AlignFilter 生成的彩色尺寸深度 mm .npy")
    parser.add_argument(
        "--calibration",
        type=Path,
        default=ROOT / "config" / "camera" / "orbbec_rgbd_calibration.json",
    )
    parser.add_argument("--save-software", type=Path, default=None)
    args = parser.parse_args()

    calibration = RGBDCalibration.from_file(args.calibration)
    raw_depth = np.load(args.raw_depth, allow_pickle=False)
    sdk_aligned = np.load(args.sdk_aligned, allow_pickle=False).astype(np.float32)
    software = SoftwareDepthAligner(calibration).align(raw_depth)
    if sdk_aligned.shape != software.shape:
        raise ValueError(
            f"SDK aligned shape {sdk_aligned.shape} 与软件输出 {software.shape} 不一致"
        )
    if args.save_software is not None:
        args.save_software.parent.mkdir(parents=True, exist_ok=True)
        np.save(args.save_software, software, allow_pickle=False)

    sdk_valid = np.isfinite(sdk_aligned) & (sdk_aligned > 0)
    software_valid = np.isfinite(software) & (software > 0)
    overlap = sdk_valid & software_valid
    union = sdk_valid | software_valid
    if not np.any(overlap):
        raise RuntimeError("SDK 与软件对齐结果没有共同有效像素")
    errors = np.abs(software[overlap] - sdk_aligned[overlap])
    report = {
        "shape": list(software.shape),
        "sdk_valid_pixels": int(sdk_valid.sum()),
        "software_valid_pixels": int(software_valid.sum()),
        "overlap_pixels": int(overlap.sum()),
        "valid_mask_iou": float(overlap.sum() / max(1, union.sum())),
        "depth_error_mm": {
            "median": float(np.median(errors)),
            "mean": float(np.mean(errors)),
            "p95": float(np.percentile(errors, 95)),
            "max": float(np.max(errors)),
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
