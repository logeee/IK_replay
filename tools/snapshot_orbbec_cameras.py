"""逐台连接 Orbbec 相机、各拍一张彩色照片，用序列号命名保存。

用途：快速对照"序列号 <-> 哪台相机 / 拍到什么画面"。

用法（需在装有 pyorbbecsdk 的环境运行）::

    /home/robot/miniconda3/envs/fastapi/bin/python tools/snapshot_orbbec_cameras.py
    # 可选：--out data/camera_snapshots --ext png --timeout-ms 3000

输出：<out>/<序列号>.<ext>，默认 data/camera_snapshots/。
被其他进程占用的相机会报错并跳过，不影响其余相机。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT / "data" / "camera_snapshots"


def _pick_color_profile(ob, profiles):
    """优先选无需解码的 RGB/BGR，其次 MJPG（用 cv2 解）。"""
    count = profiles.get_count()
    candidates = [profiles.get_stream_profile_by_index(i).as_video_stream_profile()
                  for i in range(count)]
    order = ["RGB", "BGR", "MJPG", "YUYV", "NV12"]

    def rank(p):
        name = str(p.get_format()).split(".")[-1]
        fmt = order.index(name) if name in order else len(order)
        # 同格式下取较大分辨率
        return (fmt, -(p.get_width() * p.get_height()))

    candidates.sort(key=rank)
    return candidates[0]


def _frame_to_bgr(ob, frame) -> np.ndarray:
    fmt = str(frame.get_format()).split(".")[-1]
    h, w = frame.get_height(), frame.get_width()
    data = np.frombuffer(frame.get_data(), dtype=np.uint8)
    if fmt == "RGB":
        return cv2.cvtColor(data.reshape(h, w, 3), cv2.COLOR_RGB2BGR)
    if fmt == "BGR":
        return data.reshape(h, w, 3).copy()
    if fmt == "MJPG":
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("MJPG 解码失败")
        return img
    if fmt == "YUYV":
        return cv2.cvtColor(data.reshape(h, w, 2), cv2.COLOR_YUV2BGR_YUYV)
    if fmt == "NV12":
        return cv2.cvtColor(data.reshape(h * 3 // 2, w), cv2.COLOR_YUV2BGR_NV12)
    raise RuntimeError(f"暂不支持的彩色格式: {fmt}")


def snapshot_device(ob, device, timeout_ms: int, tries: int) -> np.ndarray:
    pipeline = ob.Pipeline(device)
    profiles = pipeline.get_stream_profile_list(ob.OBSensorType.COLOR_SENSOR)
    profile = _pick_color_profile(ob, profiles)
    config = ob.Config()
    config.enable_stream(profile)
    pipeline.start(config)
    try:
        # 前几帧常常曝光未稳定/为空，多等几帧再取
        last = None
        for i in range(tries):
            frames = pipeline.wait_for_frames(timeout_ms)
            if frames is None:
                continue
            color = frames.get_color_frame()
            if color is None:
                continue
            last = _frame_to_bgr(ob, color)
            if i >= tries // 2:
                break
        if last is None:
            raise RuntimeError("超时未取得彩色帧")
        return last
    finally:
        pipeline.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    parser.add_argument("--ext", default="jpg", choices=["jpg", "png"], help="图片后缀")
    parser.add_argument("--timeout-ms", type=int, default=2000)
    parser.add_argument("--tries", type=int, default=10, help="每台相机最多等待帧数")
    args = parser.parse_args()

    try:
        import pyorbbecsdk as ob
    except ImportError:
        print("找不到 pyorbbecsdk，请用 fastapi 环境的 python 运行。", file=sys.stderr)
        return 2

    context = ob.Context()
    devices = context.query_devices()
    count = devices.get_count()
    if count == 0:
        print("SDK 未发现任何 Orbbec 设备。", file=sys.stderr)
        return 1

    # 先只读枚举信息拿序列号，再按序列号逐个打开，避免一台被占用拖垮全部
    serials = []
    for i in range(count):
        try:
            serials.append(devices.get_device_serial_number_by_index(i))
        except Exception:
            serials.append(None)

    args.out.mkdir(parents=True, exist_ok=True)
    ok = 0
    for i, serial in enumerate(serials):
        try:
            if serial:
                device = devices.get_device_by_serial_number(serial)
            else:
                device = devices.get_device_by_index(i)
                serial = device.get_device_info().get_serial_number()
            name = device.get_device_info().get_name()
            img = snapshot_device(ob, device, args.timeout_ms, args.tries)
            path = args.out / f"{serial}.{args.ext}"
            cv2.imwrite(str(path), img)
            print(f"[OK]   {serial}  {name}  {img.shape[1]}x{img.shape[0]}  -> {path}")
            ok += 1
        except Exception as exc:
            print(f"[FAIL] {serial or f'index {i}'}: {exc}", file=sys.stderr)
        finally:
            device = None

    print(f"完成：{ok}/{count} 台相机已保存到 {args.out}")
    return 0 if ok == count else 1


if __name__ == "__main__":
    sys.exit(main())
