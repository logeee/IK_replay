#!/usr/bin/env python3
"""调试期一次性导出 Orbbec RGB-D 标定参数。

这个脚本只在调试期主动打开相机 SDK。生产运行不应调用它；
reach_server 的默认 ZMQ 路径只读取本脚本生成的 JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config" / "camera" / "orbbec_rgbd_calibration.json"


def _format_name(value: Any) -> str:
    text = str(value)
    return text.rsplit(".", 1)[-1]


def _profile_summary(profile) -> str:
    return (
        f"{profile.get_width()}x{profile.get_height()}@{profile.get_fps()} "
        f"{_format_name(profile.get_format())}"
    )


def _select_profile(profiles, *, width: int, height: int, fps: int,
                    requested_format: str | None, preferences: tuple[str, ...]):
    candidates = []
    available = []
    for index in range(profiles.get_count()):
        try:
            profile = profiles.get_stream_profile_by_index(index).as_video_stream_profile()
        except Exception:
            continue
        available.append(_profile_summary(profile))
        if (profile.get_width(), profile.get_height(), profile.get_fps()) != (width, height, fps):
            continue
        fmt = _format_name(profile.get_format()).upper()
        if requested_format and fmt != requested_format.upper():
            continue
        try:
            rank = preferences.index(fmt)
        except ValueError:
            rank = len(preferences)
        candidates.append((rank, profile))
    if not candidates:
        requested = f"{width}x{height}@{fps}"
        if requested_format:
            requested += f" {requested_format}"
        raise RuntimeError(
            f"找不到 profile {requested}。设备可用 profile: {available}"
        )
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _intrinsic_dict(profile) -> dict[str, Any]:
    intr = profile.get_intrinsic()
    return {
        "width": int(intr.width),
        "height": int(intr.height),
        "fx": float(intr.fx),
        "fy": float(intr.fy),
        "cx": float(intr.cx),
        "cy": float(intr.cy),
    }


def _distortion_dict(profile) -> dict[str, Any]:
    distortion = profile.get_distortion()
    names = ("k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6")
    return {
        "model": "brown_conrady",
        "coefficients": [float(getattr(distortion, name)) for name in names],
        "coefficient_order": list(names),
    }


def _stream_dict(profile) -> dict[str, Any]:
    return {
        "width": int(profile.get_width()),
        "height": int(profile.get_height()),
        "fps": int(profile.get_fps()),
        "format": _format_name(profile.get_format()),
        "intrinsics": _intrinsic_dict(profile),
        "distortion": _distortion_dict(profile),
    }


def _device_info(device) -> dict[str, Any]:
    info = device.get_device_info()

    def optional(method: str) -> str | None:
        func = getattr(info, method, None)
        if func is None:
            return None
        try:
            return str(func())
        except Exception:
            return None

    return {
        "serial": optional("get_serial_number"),
        "name": optional("get_name"),
        "firmware_version": optional("get_firmware_version"),
    }


def _atomic_json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _make_device(ob, serial: str | None):
    context = ob.Context()
    devices = context.query_devices()
    if devices.get_count() == 0:
        raise RuntimeError("SDK 未发现 Orbbec 设备")
    found = []
    for index in range(devices.get_count()):
        device = devices.get_device_by_index(index)
        candidate = device.get_device_info().get_serial_number()
        found.append(candidate)
        if serial is None or candidate == serial:
            return device
    raise RuntimeError(f"未找到序列号 {serial!r}；已发现 {found}")


def export(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import pyorbbecsdk as ob
    except ImportError as exc:
        raise RuntimeError(
            "找不到 pyorbbecsdk。请在装有 Orbbec SDK 的调试环境运行此脚本。"
        ) from exc

    device = _make_device(ob, args.serial)
    pipeline = ob.Pipeline(device)
    color_profiles = pipeline.get_stream_profile_list(ob.OBSensorType.COLOR_SENSOR)
    depth_profiles = pipeline.get_stream_profile_list(ob.OBSensorType.DEPTH_SENSOR)
    color_profile = _select_profile(
        color_profiles,
        width=args.color_width,
        height=args.color_height,
        fps=args.fps,
        requested_format=args.color_format,
        preferences=("MJPG", "RGB", "YUYV", "NV12"),
    )
    depth_profile = _select_profile(
        depth_profiles,
        width=args.depth_width,
        height=args.depth_height,
        fps=args.fps,
        requested_format=args.depth_format,
        preferences=("Y16", "Z16"),
    )

    extrinsic = depth_profile.get_extrinsic_to(color_profile)
    rotation_raw = getattr(extrinsic, "rot", None)
    translation_raw = getattr(extrinsic, "transform", None)
    if translation_raw is None:
        translation_raw = getattr(extrinsic, "trans", None)
    if rotation_raw is None or translation_raw is None:
        raise RuntimeError("SDK 外参缺少 rot / transform(trans)")
    rotation = [float(value) for value in rotation_raw]
    translation = [float(value) for value in translation_raw]
    if len(rotation) != 9 or len(translation) != 3:
        raise RuntimeError(
            f"SDK 返回异常外参长度: rotation={len(rotation)}, translation={len(translation)}"
        )

    config = ob.Config()
    config.enable_stream(color_profile)
    config.enable_stream(depth_profile)
    try:
        config.set_frame_aggregate_output_mode(
            ob.OBFrameAggregateOutputMode.FULL_FRAME_REQUIRE
        )
    except Exception:
        pass

    depth_scale = None
    raw_depth_sample = None
    sdk_aligned_sample = None
    pipeline.start(config)
    try:
        for _ in range(30):
            frames = pipeline.wait_for_frames(1000)
            if frames is None:
                continue
            depth_frame = frames.get_depth_frame()
            if depth_frame is not None:
                depth_scale = float(depth_frame.get_depth_scale())
                if args.sample_dir is not None:
                    raw_depth_sample = np.frombuffer(
                        depth_frame.get_data(), dtype=np.uint16
                    ).reshape(
                        depth_frame.get_height(), depth_frame.get_width()
                    ).copy()
                    align = ob.AlignFilter(align_to_stream=ob.OBStreamType.COLOR_STREAM)
                    aligned = align.process(frames)
                    if aligned:
                        aligned_depth = aligned.as_frame_set().get_depth_frame()
                        if aligned_depth is not None:
                            aligned_scale = float(aligned_depth.get_depth_scale())
                            sdk_aligned_sample = np.frombuffer(
                                aligned_depth.get_data(), dtype=np.uint16
                            ).reshape(
                                aligned_depth.get_height(), aligned_depth.get_width()
                            ).astype(np.float32) * aligned_scale
                break
    finally:
        pipeline.stop()
    if depth_scale is None:
        raise RuntimeError("相机已启动，但未能取得 depth frame/depth scale")
    if args.sample_dir is not None:
        if raw_depth_sample is None or sdk_aligned_sample is None:
            raise RuntimeError("已请求 --sample-dir，但未取得 SDK 对齐样本")
        args.sample_dir.mkdir(parents=True, exist_ok=True)
        np.save(args.sample_dir / "raw_depth_z16.npy", raw_depth_sample, allow_pickle=False)
        np.save(
            args.sample_dir / "sdk_aligned_depth_mm.npy",
            sdk_aligned_sample,
            allow_pickle=False,
        )

    get_version = getattr(ob, "get_version", None)
    sdk_version = str(get_version()) if get_version is not None else None
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "IK_replay/tools/export_orbbec_rgbd_calibration.py",
        "sdk_version": sdk_version,
        "device": _device_info(device),
        "color": _stream_dict(color_profile),
        "depth": _stream_dict(depth_profile),
        "depth_to_color": {
            "rotation_row_major": [
                rotation[0:3],
                rotation[3:6],
                rotation[6:9],
            ],
            "translation": translation,
            "translation_unit": "mm",
            "convention": "p_color_mm = R @ p_depth_mm + t_mm",
        },
        "depth_scale": {
            "value": depth_scale,
            "unit": "mm_per_raw_unit",
        },
    }
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "一次性启动 Orbbec SDK，导出软件 depth-to-color 对齐所需参数。"
            "运行前需暂时停止占用相机的推流服务。"
        )
    )
    parser.add_argument("--serial", default=None)
    parser.add_argument("--color-width", type=int, default=1920)
    parser.add_argument("--color-height", type=int, default=1080)
    parser.add_argument("--depth-width", type=int, default=1280)
    parser.add_argument("--depth-height", type=int, default=800)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--color-format", default=None, help="例如 MJPG；默认自动选择")
    parser.add_argument("--depth-format", default=None, help="例如 Y16；默认自动选择")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--sample-dir",
        type=Path,
        default=None,
        help="可选：同时保存 raw Z16 与 SDK AlignFilter 的 .npy 对照样本",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        payload = export(args)
        _atomic_json_dump(args.output, payload)
    except Exception as exc:
        print(f"[calibration] 导出失败: {exc}", file=sys.stderr)
        return 1
    print(f"[calibration] 已写入 {args.output}")
    print(
        "[calibration] "
        f"color={payload['color']['width']}x{payload['color']['height']} "
        f"depth={payload['depth']['width']}x{payload['depth']['height']} "
        f"serial={payload['device']['serial']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
