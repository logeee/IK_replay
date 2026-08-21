"""Geometry, semantic coloring and binary protocol for the 7005 viewer."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

import cv2
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


def detection_pixel_mask(
    u: np.ndarray,
    v: np.ndarray,
    detection: dict[str, Any],
    *,
    image_shape: tuple[int, int],
) -> np.ndarray:
    """Return pixels inside an instance polygon, falling back to its box."""
    u_values = np.asarray(u)
    v_values = np.asarray(v)
    if u_values.shape != v_values.shape:
        raise ValueError("u/v 像素数组尺寸不一致")
    height, width = image_shape
    polygon_value = detection.get("polygon")
    if polygon_value is not None:
        try:
            polygon = np.asarray(polygon_value, dtype=np.float32)
        except (TypeError, ValueError):
            polygon = np.empty((0, 2), dtype=np.float32)
        if (
            polygon.ndim == 2
            and polygon.shape[0] >= 3
            and polygon.shape[1] == 2
            and np.isfinite(polygon).all()
            and height > 0
            and width > 0
        ):
            raster = np.zeros((height, width), dtype=np.uint8)
            cv2.fillPoly(raster, [np.rint(polygon).astype(np.int32)], 1)
            finite = np.isfinite(u_values) & np.isfinite(v_values)
            pixel_u = np.zeros(u_values.shape, dtype=np.int64)
            pixel_v = np.zeros(v_values.shape, dtype=np.int64)
            pixel_u[finite] = np.rint(u_values[finite]).astype(np.int64)
            pixel_v[finite] = np.rint(v_values[finite]).astype(np.int64)
            valid = (
                finite
                & (pixel_u >= 0)
                & (pixel_u < width)
                & (pixel_v >= 0)
                & (pixel_v < height)
            )
            inside = np.zeros(u_values.shape, dtype=bool)
            inside[valid] = raster[pixel_v[valid], pixel_u[valid]] != 0
            return inside
    try:
        x1, y1, x2, y2 = [float(value) for value in detection["xyxy"]]
    except (KeyError, TypeError, ValueError):
        return np.zeros(u_values.shape, dtype=bool)
    return (
        (u_values >= max(0.0, x1))
        & (u_values <= min(width - 1.0, x2))
        & (v_values >= max(0.0, y1))
        & (v_values <= min(height - 1.0, y2))
    )


def _normalized_pixels(
    u: np.ndarray,
    v: np.ndarray,
    intrinsics: tuple[float, float, float, float] | np.ndarray,
    distortion: np.ndarray | list[float] | tuple[float, ...] | None,
) -> np.ndarray:
    fx, fy, cx, cy = [float(value) for value in intrinsics]
    if fx <= 0 or fy <= 0:
        raise ValueError("fx/fy 必须为正数")
    coefficients = np.asarray(
        [] if distortion is None else distortion,
        dtype=np.float64,
    ).reshape(-1)
    pixels = np.column_stack((u, v)).astype(np.float64).reshape(-1, 1, 2)
    if pixels.shape[0] == 0:
        return np.empty((0, 2), dtype=np.float64)
    if coefficients.size in {4, 5, 8, 12, 14} and np.any(
        np.abs(coefficients) > 1e-12
    ):
        matrix = np.array(
            [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        return cv2.undistortPoints(pixels, matrix, coefficients).reshape(-1, 2)
    result = np.empty((pixels.shape[0], 2), dtype=np.float64)
    result[:, 0] = (pixels[:, 0, 0] - cx) / fx
    result[:, 1] = (pixels[:, 0, 1] - cy) / fy
    return result


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
    dense_box_sampling: bool = True,
    box_padding_ratio: float = 0.1,
    distortion: np.ndarray | list[float] | tuple[float, ...] | None = None,
) -> PointCloud:
    """Back-project aligned depth and assign RGB and detection-box colors.

    The background uses ``stride`` sampling. By default, each valid YOLO box
    is expanded by ``box_padding_ratio`` and sampled at every pixel so that
    interactive picking remains precise near detected targets.
    """
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
    if not 0.0 <= box_padding_ratio <= 1.0:
        raise ValueError("box_padding_ratio 必须在 0~1")
    _normalized_pixels(
        np.empty(0), np.empty(0), intrinsics, distortion
    )

    height, width = depth.shape
    sampled = np.zeros((height, width), dtype=bool)
    sampled[::stride, ::stride] = True
    if dense_box_sampling:
        for box in boxes:
            try:
                x1, y1, x2, y2 = [float(value) for value in box["xyxy"]]
            except (KeyError, TypeError, ValueError):
                continue
            if not np.all(np.isfinite([x1, y1, x2, y2])) or x2 < x1 or y2 < y1:
                continue
            pad_x = (x2 - x1) * box_padding_ratio
            pad_y = (y2 - y1) * box_padding_ratio
            left = max(0, int(np.floor(x1 - pad_x)))
            top = max(0, int(np.floor(y1 - pad_y)))
            right = min(width - 1, int(np.ceil(x2 + pad_x)))
            bottom = min(height - 1, int(np.ceil(y2 + pad_y)))
            if left <= right and top <= bottom:
                sampled[top:bottom + 1, left:right + 1] = True

    vv, uu = np.nonzero(sampled)
    z = depth[vv, uu].astype(np.float32) / 1000.0
    valid = np.isfinite(z) & (z >= z_min_m) & (z <= z_max_m)
    u = uu[valid].astype(np.uint16, copy=False)
    v = vv[valid].astype(np.uint16, copy=False)
    z_valid = z[valid]
    count = int(z_valid.size)
    if count > max_points:
        raise ValueError(
            f"点数 {count} 超过上限 {max_points}，请增大 stride 或缩小深度范围"
        )

    positions = np.empty((count, 3), dtype="<f4")
    normalized = _normalized_pixels(u, v, intrinsics, distortion)
    positions[:, 0] = normalized[:, 0] * z_valid
    positions[:, 1] = normalized[:, 1] * z_valid
    positions[:, 2] = z_valid
    rgb = image[v, u, ::-1].astype(np.uint8, copy=True)
    pixels = np.column_stack((u, v)).astype("<u2", copy=False)
    class_ids = np.full(count, -1, dtype="<i2")
    semantic = np.repeat(BACKGROUND_COLOR[None, :], count, axis=0)

    # Low confidence first, so a higher-confidence overlapping box wins.
    for box in sorted(boxes, key=lambda item: float(item.get("conf", 0.0))):
        try:
            cls = int(box["cls"])
        except (KeyError, TypeError, ValueError):
            continue
        inside = detection_pixel_mask(
            u, v, box, image_shape=(height, width)
        )
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


def point_from_pixel(
    depth_mm: np.ndarray,
    intrinsics: tuple[float, float, float, float] | np.ndarray,
    u: int,
    v: int,
    *,
    search_radius: int = 6,
    z_min_m: float = 0.15,
    z_max_m: float = 3.0,
    distortion: np.ndarray | list[float] | tuple[float, ...] | None = None,
) -> dict[str, Any]:
    depth = np.asarray(depth_mm)
    if depth.ndim != 2:
        raise ValueError(f"depth 必须是 HxW，实际 {depth.shape}")
    if not 0 <= search_radius <= 50:
        raise ValueError("search_radius 必须在 0~50")
    if not 0.01 <= z_min_m < z_max_m <= 30.0:
        raise ValueError("深度范围不合法")
    height, width = depth.shape
    if not (0 <= u < width and 0 <= v < height):
        raise ValueError(f"像素 ({u},{v}) 超出 {width}x{height}")
    _normalized_pixels(
        np.empty(0), np.empty(0), intrinsics, distortion
    )

    x0, x1 = max(0, u - search_radius), min(width, u + search_radius + 1)
    y0, y1 = max(0, v - search_radius), min(height, v + search_radius + 1)
    patch = depth[y0:y1, x0:x1].astype(np.float64, copy=False)
    valid = (
        np.isfinite(patch)
        & (patch >= z_min_m * 1000.0)
        & (patch <= z_max_m * 1000.0)
    )
    ys, xs = np.nonzero(valid)
    if not len(xs):
        raise ValueError(f"点击位置附近 {search_radius}px 内没有有效深度")
    used_u = xs + x0
    used_v = ys + y0
    distance2 = (used_u - u) ** 2 + (used_v - v) ** 2
    nearest = int(np.argmin(distance2))
    actual_u = int(used_u[nearest])
    actual_v = int(used_v[nearest])
    z = float(depth[actual_v, actual_u]) / 1000.0
    normalized = _normalized_pixels(
        np.array([actual_u]),
        np.array([actual_v]),
        intrinsics,
        distortion,
    )[0]
    return {
        "requested_pixel": [int(u), int(v)],
        "pixel": [actual_u, actual_v],
        "search_distance_px": float(np.sqrt(distance2[nearest])),
        "depth_mm": z * 1000.0,
        "p_camera": [
            float(normalized[0] * z),
            float(normalized[1] * z),
            z,
        ],
    }


def fit_surface_plane(
    depth_mm: np.ndarray,
    intrinsics: tuple[float, float, float, float] | np.ndarray,
    p_camera_surface: list[float] | np.ndarray,
    *,
    radius_m: float = 0.12,
    distortion: np.ndarray | list[float] | tuple[float, ...] | None = None,
) -> dict[str, Any] | None:
    depth = np.asarray(depth_mm)
    if depth.ndim != 2:
        raise ValueError(f"depth 必须是 HxW，实际 {depth.shape}")
    target = np.asarray(p_camera_surface, dtype=np.float64).reshape(3)
    if not np.isfinite(target).all() or target[2] <= 0:
        raise ValueError("p_camera_surface 不合法")
    height, width = depth.shape
    stride = max(1, int(round(max(height, width) / 320)))
    sampled = depth[::stride, ::stride].astype(np.float64) / 1000.0
    vs, us = np.mgrid[0:height:stride, 0:width:stride]
    valid = np.isfinite(sampled) & (sampled > 0.15) & (sampled < 3.0)
    normalized = _normalized_pixels(
        us[valid], vs[valid], intrinsics, distortion
    )
    points = np.stack(
        [
            normalized[:, 0] * sampled[valid],
            normalized[:, 1] * sampled[valid],
            sampled[valid],
        ],
        axis=1,
    )
    near = points[np.linalg.norm(points - target, axis=1) < radius_m]
    if len(near) < 50:
        return None
    center = near.mean(axis=0)
    centered = near - center
    _, _, vh = np.linalg.svd(centered, full_matrices=False)
    normal = vh[-1]
    rms_m = float(np.sqrt(np.mean((centered @ normal) ** 2)))
    if float(np.dot(normal, -center)) < 0:
        normal = -normal
    return {
        "center_cam": center.tolist(),
        "normal_cam": normal.tolist(),
        "rms_mm": rms_m * 1000.0,
        "points": int(len(near)),
        "radius_m": float(radius_m),
    }


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
