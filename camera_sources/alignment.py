"""SDK-free depth-to-color alignment using exported Orbbec calibration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是 JSON object")
    return value


def _require_shape(stream: dict[str, Any], name: str) -> tuple[int, int]:
    try:
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} 缺少合法 width/height") from exc
    if width <= 0 or height <= 0:
        raise ValueError(f"{name} 的 width/height 必须为正数")
    return height, width


def _intrinsic_matrix(stream: dict[str, Any], name: str) -> np.ndarray:
    intr = _require_mapping(stream.get("intrinsics"), f"{name}.intrinsics")
    try:
        fx, fy = float(intr["fx"]), float(intr["fy"])
        cx, cy = float(intr["cx"]), float(intr["cy"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{name}.intrinsics 缺少 fx/fy/cx/cy") from exc
    if fx <= 0 or fy <= 0:
        raise ValueError(f"{name}.intrinsics 的 fx/fy 必须为正数")
    expected_h, expected_w = _require_shape(stream, name)
    if int(intr.get("width", expected_w)) != expected_w:
        raise ValueError(f"{name}.intrinsics.width 与 stream width 不一致")
    if int(intr.get("height", expected_h)) != expected_h:
        raise ValueError(f"{name}.intrinsics.height 与 stream height 不一致")
    return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float64)


def _distortion(stream: dict[str, Any], name: str) -> np.ndarray:
    distortion = _require_mapping(stream.get("distortion"), f"{name}.distortion")
    model = str(distortion.get("model", "brown_conrady")).lower()
    if model not in {"brown_conrady", "brown-conrady", "opencv", "rational_polynomial"}:
        raise ValueError(f"{name}.distortion 不支持模型 {model!r}")
    order = distortion.get(
        "coefficient_order",
        ["k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6"],
    )
    values = distortion.get("coefficients")
    if not isinstance(order, list) or not isinstance(values, list) or len(order) != len(values):
        raise ValueError(f"{name}.distortion 的 coefficients/order 不合法")
    by_name = {str(key): float(value) for key, value in zip(order, values)}
    return np.array(
        [by_name.get(key, 0.0) for key in ("k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6")],
        dtype=np.float64,
    )


@dataclass(frozen=True)
class RGBDCalibration:
    """Validated calibration used by both the protocol and geometry layers."""

    path: Path
    serial: str | None
    color_shape: tuple[int, int]
    depth_shape: tuple[int, int]
    color_matrix: np.ndarray
    depth_matrix: np.ndarray
    color_distortion: np.ndarray
    depth_distortion: np.ndarray
    depth_to_color_rotation: np.ndarray
    depth_to_color_translation_mm: np.ndarray
    depth_scale_mm: float

    @classmethod
    def from_file(cls, path: str | Path) -> "RGBDCalibration":
        calibration_path = Path(path).expanduser().resolve()
        try:
            payload = json.loads(calibration_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(
                f"RGB-D 标定文件不存在: {calibration_path}。"
                "请先运行 tools/export_orbbec_rgbd_calibration.py"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取 RGB-D 标定文件 {calibration_path}: {exc}") from exc
        root = _require_mapping(payload, "calibration")
        if int(root.get("schema_version", -1)) != 1:
            raise ValueError(f"不支持 calibration schema_version={root.get('schema_version')!r}")

        color = _require_mapping(root.get("color"), "color")
        depth = _require_mapping(root.get("depth"), "depth")
        extrinsic = _require_mapping(root.get("depth_to_color"), "depth_to_color")
        scale = _require_mapping(root.get("depth_scale"), "depth_scale")
        if scale.get("unit") != "mm_per_raw_unit":
            raise ValueError(f"不支持 depth_scale.unit={scale.get('unit')!r}")
        depth_scale_mm = float(scale.get("value", 0.0))
        if depth_scale_mm <= 0:
            raise ValueError("depth_scale.value 必须为正数")
        if extrinsic.get("translation_unit") != "mm":
            raise ValueError(
                f"不支持 depth_to_color.translation_unit={extrinsic.get('translation_unit')!r}"
            )
        rotation = np.asarray(extrinsic.get("rotation_row_major"), dtype=np.float64)
        translation = np.asarray(extrinsic.get("translation"), dtype=np.float64)
        if rotation.shape != (3, 3):
            raise ValueError("depth_to_color.rotation_row_major 必须是 3x3")
        if translation.shape != (3,):
            raise ValueError("depth_to_color.translation 必须是长度 3")
        if not np.all(np.isfinite(rotation)) or not np.all(np.isfinite(translation)):
            raise ValueError("depth_to_color 含非有限数值")
        if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-3):
            raise ValueError("depth_to_color rotation 不是正交矩阵")

        device = _require_mapping(root.get("device", {}), "device")
        return cls(
            path=calibration_path,
            serial=None if device.get("serial") is None else str(device["serial"]),
            color_shape=_require_shape(color, "color"),
            depth_shape=_require_shape(depth, "depth"),
            color_matrix=_intrinsic_matrix(color, "color"),
            depth_matrix=_intrinsic_matrix(depth, "depth"),
            color_distortion=_distortion(color, "color"),
            depth_distortion=_distortion(depth, "depth"),
            depth_to_color_rotation=rotation,
            depth_to_color_translation_mm=translation,
            depth_scale_mm=depth_scale_mm,
        )

    @property
    def color_intrinsics(self) -> tuple[float, float, float, float]:
        return (
            float(self.color_matrix[0, 0]),
            float(self.color_matrix[1, 1]),
            float(self.color_matrix[0, 2]),
            float(self.color_matrix[1, 2]),
        )


class SoftwareDepthAligner:
    """Map raw depth pixels into the color image with nearest-depth z-buffering."""

    def __init__(self, calibration: RGBDCalibration):
        self.calibration = calibration
        depth_rays = self._make_depth_rays()
        # Both the undistorted rays and depth-to-color rotation are immutable.
        # Precompute R @ ray once instead of repeating a million-point BLAS
        # matrix multiplication for every incoming depth frame.
        self._rotated_depth_rays = np.ascontiguousarray(
            depth_rays @ self.calibration.depth_to_color_rotation.T,
            dtype=np.float64,
        )

    def _make_depth_rays(self) -> np.ndarray:
        height, width = self.calibration.depth_shape
        yy, xx = np.mgrid[0:height, 0:width]
        pixels = np.stack((xx, yy), axis=-1).reshape(-1, 1, 2).astype(np.float64)
        normalized = cv2.undistortPoints(
            pixels,
            self.calibration.depth_matrix,
            self.calibration.depth_distortion,
        ).reshape(-1, 2)
        rays = np.empty((normalized.shape[0], 3), dtype=np.float32)
        rays[:, :2] = normalized.astype(np.float32)
        rays[:, 2] = 1.0
        return rays

    @staticmethod
    def _distort_normalized(points: np.ndarray, coefficients: np.ndarray) -> np.ndarray:
        k1, k2, p1, p2, k3, k4, k5, k6 = coefficients
        x = points[:, 0]
        y = points[:, 1]
        r2 = x * x + y * y
        r4 = r2 * r2
        r6 = r4 * r2
        denominator = 1.0 + k4 * r2 + k5 * r4 + k6 * r6
        safe = np.abs(denominator) > 1e-12
        radial = np.ones_like(r2)
        radial[safe] = (
            (1.0 + k1 * r2[safe] + k2 * r4[safe] + k3 * r6[safe])
            / denominator[safe]
        )
        xy = x * y
        xd = x * radial + 2.0 * p1 * xy + p2 * (r2 + 2.0 * x * x)
        yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * xy
        return np.column_stack((xd, yd))

    def align(self, raw_depth: np.ndarray) -> np.ndarray:
        if raw_depth.shape != self.calibration.depth_shape:
            raise ValueError(
                f"depth shape {raw_depth.shape} 与标定 {self.calibration.depth_shape} 不一致"
            )
        if raw_depth.dtype != np.uint16:
            raise ValueError(f"depth dtype 必须是 uint16，实际为 {raw_depth.dtype}")

        raw_flat = raw_depth.reshape(-1)
        valid_indices = np.flatnonzero(raw_flat)
        color_h, color_w = self.calibration.color_shape
        output = np.zeros((color_h, color_w), dtype=np.float32)
        if valid_indices.size == 0:
            return output

        z_depth_mm = raw_flat[valid_indices].astype(np.float32)
        z_depth_mm *= np.float32(self.calibration.depth_scale_mm)
        points_color = (
            self._rotated_depth_rays[valid_indices] * z_depth_mm[:, None]
            + self.calibration.depth_to_color_translation_mm
        )
        z_color = points_color[:, 2]
        in_front = np.isfinite(z_color) & (z_color > 0.0)
        if not np.any(in_front):
            return output
        points_color = points_color[in_front]
        z_color = z_color[in_front]

        normalized = points_color[:, :2] / z_color[:, None]
        distorted = self._distort_normalized(
            normalized, self.calibration.color_distortion
        )
        fx, fy, cx, cy = self.calibration.color_intrinsics
        u_float = distorted[:, 0] * fx + cx
        v_float = distorted[:, 1] * fy + cy
        u0 = np.floor(u_float).astype(np.int64)
        v0 = np.floor(v_float).astype(np.int64)
        u1 = u0 + (u_float - u0 > 1e-6)
        v1 = v0 + (v_float - v0 > 1e-6)
        depths = z_color.astype(np.float32)
        z_buffer = np.full(color_h * color_w, np.inf, dtype=np.float32)
        # Rasterize the projected sub-pixel location onto its four neighboring
        # color pixels. This represents a depth sample's pixel footprint when
        # color resolution is higher; it is not an image-space hole-filling
        # filter. z-buffering keeps foreground/background occlusion deterministic.
        for u, v in ((u0, v0), (u1, v0), (u0, v1), (u1, v1)):
            inside = (u >= 0) & (u < color_w) & (v >= 0) & (v < color_h)
            if np.any(inside):
                np.minimum.at(z_buffer, v[inside] * color_w + u[inside], depths[inside])
        finite = np.isfinite(z_buffer)
        output.reshape(-1)[finite] = z_buffer[finite]
        return output
