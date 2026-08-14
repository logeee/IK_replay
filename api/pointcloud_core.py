"""Geometry, semantic coloring and binary protocol for the 7005 viewer."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

import numpy as np


MAGIC = b"PCV1"
VERSION = 1
HEADER = struct.Struct("<4sIII")  # magic, version, point_count, reserved
BACKGROUND_COLOR = np.array([42, 46, 56], dtype=np.uint8)
PALETTE = np.array([
    [239, 83, 80], [66, 165, 245], [102, 187, 106], [255, 202, 40],
    [171, 71, 188], [255, 112, 67], [38, 198, 218], [141, 110, 99],
    [236, 64, 122], [124, 179, 66], [126, 87, 194], [255, 167, 38],
], dtype=np.uint8)


@dataclass(frozen=True)
class PointCloud:
    positions: np.ndarray
    rgb: np.ndarray
    semantic: np.ndarray
    pixels: np.ndarray
    class_ids: np.ndarray

    @property
    def count(self) -> int:
        return int(self.positions.shape[0])


def build_pointcloud(
    depth_mm: np.ndarray,
    bgr: np.ndarray,
    intrinsics: tuple[float, float, float, float] | np.ndarray,
    boxes: list[dict[str, Any]],
    *,
    stride: int = 4,
    z_min_m: float = 0.15,
    z_max_m: float = 3.0,
    max_points: int = 350_000,
) -> PointCloud:
    """Back-project aligned depth and assign RGB and detection-box colors."""
    depth = np.asarray(depth_mm)
    image = np.asarray(bgr)
    if depth.ndim != 2:
        raise ValueError(f"depth 必须是 HxW，实际 {depth.shape}")
    if image.ndim != 3 or image.shape[:2] != depth.shape or image.shape[2] != 3:
        raise ValueError(
            f"彩色图 {image.shape} 与对齐深度 {depth.shape} 尺寸不一致"
        )
    if stride < 1 or stride > 32:
        raise ValueError("stride 必须在 1~32")
    if not (0.01 <= z_min_m < z_max_m <= 30.0):
        raise ValueError("深度范围不合法")
    fx, fy, cx, cy = [float(v) for v in intrinsics]
    if fx <= 0 or fy <= 0:
        raise ValueError("fx/fy 必须为正数")

    height, width = depth.shape
    vv, uu = np.mgrid[0:height:stride, 0:width:stride]
    z = depth[::stride, ::stride].astype(np.float32) / 1000.0
    valid = np.isfinite(z) & (z >= z_min_m) & (z <= z_max_m)
    u = uu[valid].astype(np.uint16)
    v = vv[valid].astype(np.uint16)
    z_valid = z[valid]
    count = int(z_valid.size)
    if count > max_points:
        raise ValueError(
            f"点数 {count} 超过上限 {max_points}，请增大 stride 或缩小深度范围"
        )

    positions = np.empty((count, 3), dtype="<f4")
    positions[:, 0] = (u.astype(np.float32) - cx) * z_valid / fx
    positions[:, 1] = (v.astype(np.float32) - cy) * z_valid / fy
    positions[:, 2] = z_valid
    rgb = image[v, u, ::-1].astype(np.uint8, copy=True)
    pixels = np.column_stack((u, v)).astype("<u2", copy=False)
    class_ids = np.full(count, -1, dtype="<i2")
    semantic = np.repeat(BACKGROUND_COLOR[None, :], count, axis=0)

    # Low confidence first, so a higher-confidence overlapping box wins.
    for box in sorted(boxes, key=lambda item: float(item.get("conf", 0.0))):
        try:
            cls = int(box["cls"])
            x1, y1, x2, y2 = [float(value) for value in box["xyxy"]]
        except (KeyError, TypeError, ValueError):
            continue
        inside = ((u >= max(0.0, x1)) & (u <= min(width - 1.0, x2))
                  & (v >= max(0.0, y1)) & (v <= min(height - 1.0, y2)))
        if np.any(inside):
            class_ids[inside] = cls
            semantic[inside] = PALETTE[cls % len(PALETTE)]

    return PointCloud(
        positions=np.ascontiguousarray(positions),
        rgb=np.ascontiguousarray(rgb),
        semantic=np.ascontiguousarray(semantic),
        pixels=np.ascontiguousarray(pixels),
        class_ids=np.ascontiguousarray(class_ids),
    )


def encode_pointcloud(cloud: PointCloud) -> bytes:
    """Encode arrays as PCV1: header then five contiguous little-endian arrays."""
    count = cloud.count
    expected = {
        "positions": (count, 3),
        "rgb": (count, 3),
        "semantic": (count, 3),
        "pixels": (count, 2),
        "class_ids": (count,),
    }
    for name, shape in expected.items():
        if getattr(cloud, name).shape != shape:
            raise ValueError(f"{name} shape 不合法: {getattr(cloud, name).shape}")
    return b"".join([
        HEADER.pack(MAGIC, VERSION, count, 0),
        np.asarray(cloud.positions, dtype="<f4").tobytes(order="C"),
        np.asarray(cloud.rgb, dtype=np.uint8).tobytes(order="C"),
        np.asarray(cloud.semantic, dtype=np.uint8).tobytes(order="C"),
        np.asarray(cloud.pixels, dtype="<u2").tobytes(order="C"),
        np.asarray(cloud.class_ids, dtype="<i2").tobytes(order="C"),
    ])


def decode_pointcloud(data: bytes) -> PointCloud:
    """Reference decoder used by tests and protocol diagnostics."""
    if len(data) < HEADER.size:
        raise ValueError("点云数据头不完整")
    magic, version, count, _ = HEADER.unpack_from(data)
    if magic != MAGIC or version != VERSION:
        raise ValueError(f"不支持的点云协议 {magic!r}/v{version}")
    expected_size = HEADER.size + count * 24
    if len(data) != expected_size:
        raise ValueError(f"点云数据长度 {len(data)}，期望 {expected_size}")
    offset = HEADER.size

    def take(dtype, shape, size):
        nonlocal offset
        result = np.frombuffer(data, dtype=dtype, count=size, offset=offset)
        offset += result.nbytes
        return result.reshape(shape).copy()

    return PointCloud(
        positions=take("<f4", (count, 3), count * 3),
        rgb=take(np.uint8, (count, 3), count * 3),
        semantic=take(np.uint8, (count, 3), count * 3),
        pixels=take("<u2", (count, 2), count * 2),
        class_ids=take("<i2", (count,), count),
    )
