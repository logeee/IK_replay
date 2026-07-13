from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .types import Pose


def parse_vec3(value: str | None, default: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> np.ndarray:
    if not value:
        return np.array(default, dtype=float)
    parts = [float(part) for part in value.replace(",", " ").split()]
    if len(parts) != 3:
        raise ValueError(f"expected a 3-vector, got {value!r}")
    return np.array(parts, dtype=float)


def rpy_matrix(rpy: list[float] | np.ndarray) -> np.ndarray:
    roll, pitch, yaw = [float(v) for v in rpy]
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]], dtype=float)
    ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=float)
    rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=float)
    return rz @ ry @ rx


def axis_angle_matrix(axis: np.ndarray, angle: float) -> np.ndarray:
    axis = np.array(axis, dtype=float)
    norm = float(np.linalg.norm(axis))
    if norm < 1e-12:
        return np.eye(3)
    x, y, z = axis / norm
    c = math.cos(float(angle))
    s = math.sin(float(angle))
    c1 = 1.0 - c
    return np.array(
        [
            [c + x * x * c1, x * y * c1 - z * s, x * z * c1 + y * s],
            [y * x * c1 + z * s, c + y * y * c1, y * z * c1 - x * s],
            [z * x * c1 - y * s, z * y * c1 + x * s, c + z * z * c1],
        ],
        dtype=float,
    )


def transform_from_xyz_rpy(xyz: list[float] | np.ndarray, rpy: list[float] | np.ndarray) -> np.ndarray:
    matrix = np.eye(4, dtype=float)
    matrix[:3, :3] = rpy_matrix(rpy)
    matrix[:3, 3] = np.array(xyz, dtype=float)
    return matrix


def transform_from_pose(pose: Pose) -> np.ndarray:
    return transform_from_xyz_rpy(pose.xyz, pose.rpy)


def transform_from_rotation_translation(rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    matrix = np.eye(4, dtype=float)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = translation
    return matrix


def translation_matrix(xyz: list[float] | np.ndarray) -> np.ndarray:
    matrix = np.eye(4, dtype=float)
    matrix[:3, 3] = np.array(xyz, dtype=float)
    return matrix


def matrix_to_rpy(rotation: np.ndarray) -> list[float]:
    sy = -float(rotation[2, 0])
    sy = max(-1.0, min(1.0, sy))
    pitch = math.asin(sy)
    cp = math.cos(pitch)
    if abs(cp) > 1e-8:
        roll = math.atan2(float(rotation[2, 1]), float(rotation[2, 2]))
        yaw = math.atan2(float(rotation[1, 0]), float(rotation[0, 0]))
    else:
        roll = math.atan2(-float(rotation[1, 2]), float(rotation[1, 1]))
        yaw = 0.0
    return [float(roll), float(pitch), float(yaw)]


def pose_from_matrix(matrix: np.ndarray) -> Pose:
    return Pose(
        xyz=[float(v) for v in matrix[:3, 3]],
        rpy=matrix_to_rpy(matrix[:3, :3]),
    )


def resolve_project_path(project_root: Path, raw_path: str | Path) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else project_root / path
