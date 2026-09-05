"""Task-independent robot tool calibration configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import numpy as np
import yaml


def _rotation_from_quaternion_wxyz(quaternion: object) -> np.ndarray:
    q = np.asarray(quaternion, dtype=np.float64).reshape(-1)
    if q.shape != (4,) or not np.all(np.isfinite(q)):
        raise ValueError("quaternion_wxyz must be one finite 4-vector")
    norm = float(np.linalg.norm(q))
    if norm <= 1.0e-12:
        raise ValueError("quaternion_wxyz must be non-zero")
    w, x, y, z = q / norm
    return np.asarray(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


@dataclass(frozen=True)
class ToolConfig:
    """Fixed calibration from a robot wrist link to one TCP frame."""

    name: str
    parent_link: str
    wrist_T_tcp: np.ndarray

    @classmethod
    def from_mapping(cls, document: Mapping[str, object]) -> "ToolConfig":
        values = document.get("tool", document)
        if not isinstance(values, Mapping):
            raise ValueError("ToolConfig must contain a 'tool' mapping")
        transform = values.get("wrist_T_tcp")
        if not isinstance(transform, Mapping):
            raise ValueError("ToolConfig.tool.wrist_T_tcp must be a mapping")
        position = np.asarray(transform.get("position"), dtype=np.float64).reshape(-1)
        if position.shape != (3,) or not np.all(np.isfinite(position)):
            raise ValueError("wrist_T_tcp.position must be one finite 3-vector")
        matrix = np.eye(4, dtype=np.float64)
        matrix[:3, :3] = _rotation_from_quaternion_wxyz(
            transform.get("quaternion_wxyz")
        )
        matrix[:3, 3] = position
        name = str(values.get("name", "")).strip()
        parent = str(values.get("parent_link", "")).strip()
        if not name or not parent:
            raise ValueError("ToolConfig requires non-empty name and parent_link")
        return cls(name, parent, matrix)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ToolConfig":
        config_path = Path(path).expanduser().resolve()
        with config_path.open(encoding="utf-8") as stream:
            return cls.from_mapping(yaml.safe_load(stream))

    @classmethod
    def from_legacy_tcp_mapping(cls, values: Mapping[str, object]) -> "ToolConfig":
        """Compatibility adapter for retained historical experiment configs."""
        import pinocchio as pin

        matrix = np.eye(4, dtype=np.float64)
        matrix[:3, :3] = pin.rpy.rpyToMatrix(
            np.asarray(values["rpy"], dtype=np.float64)
        )
        matrix[:3, 3] = np.asarray(values["xyz"], dtype=np.float64)
        return cls(
            str(values["frame_name"]),
            str(values["parent_frame"]),
            matrix,
        )
