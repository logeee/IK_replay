"""Convert nominal joint paths to WORLD TCP references and sample them smoothly."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pinocchio as pin


def rotation_to_quaternion_wxyz(rotation: np.ndarray) -> np.ndarray:
    quaternion = pin.Quaternion(np.asarray(rotation, dtype=np.float64))
    coefficients_xyzw = np.asarray(quaternion.coeffs(), dtype=np.float64)
    result = np.roll(coefficients_xyzw, 1)
    if result[0] < 0.0:
        result = -result
    return result / np.linalg.norm(result)


def quaternion_wxyz_to_rotation(quaternion: np.ndarray) -> np.ndarray:
    wxyz = np.asarray(quaternion, dtype=np.float64)
    wxyz /= np.linalg.norm(wxyz)
    return pin.Quaternion(*wxyz.tolist()).matrix()


def slerp_wxyz(left: np.ndarray, right: np.ndarray, alpha: float) -> np.ndarray:
    q0 = np.asarray(left, dtype=np.float64) / np.linalg.norm(left)
    q1 = np.asarray(right, dtype=np.float64) / np.linalg.norm(right)
    dot = float(np.dot(q0, q1))
    if dot < 0.0:
        q1 = -q1
        dot = -dot
    dot = float(np.clip(dot, -1.0, 1.0))
    if dot > 0.9995:
        result = q0 + alpha * (q1 - q0)
        return result / np.linalg.norm(result)
    angle = np.arccos(dot)
    sin_angle = np.sin(angle)
    return (
        np.sin((1.0 - alpha) * angle) / sin_angle * q0
        + np.sin(alpha * angle) / sin_angle * q1
    )


@dataclass(frozen=True)
class WorldTCPReferenceTrajectory:
    """Timestamped WORLD TCP poses with linear translation and quaternion SLERP."""

    time_s: np.ndarray
    world_T_tcp: np.ndarray

    def __post_init__(self) -> None:
        times = np.asarray(self.time_s, dtype=np.float64)
        transforms = np.asarray(self.world_T_tcp, dtype=np.float64)
        if times.ndim != 1 or transforms.shape != (times.size, 4, 4):
            raise ValueError("Expected time [N] and transforms [N,4,4]")
        if times.size < 2 or np.any(np.diff(times) <= 0.0):
            raise ValueError("Reference timestamps must be strictly increasing")
        if not np.all(np.isfinite(times)) or not np.all(np.isfinite(transforms)):
            raise ValueError("Reference trajectory contains NaN or infinity")

    @property
    def duration_s(self) -> float:
        return float(self.time_s[-1])

    def sample(self, elapsed_s: float) -> np.ndarray:
        if elapsed_s <= self.time_s[0]:
            return self.world_T_tcp[0].copy()
        if elapsed_s >= self.time_s[-1]:
            return self.world_T_tcp[-1].copy()
        right = int(np.searchsorted(self.time_s, elapsed_s, side="right"))
        left = right - 1
        alpha = float(
            (elapsed_s - self.time_s[left]) / (self.time_s[right] - self.time_s[left])
        )
        transform = np.eye(4, dtype=np.float64)
        transform[:3, 3] = (
            (1.0 - alpha) * self.world_T_tcp[left, :3, 3]
            + alpha * self.world_T_tcp[right, :3, 3]
        )
        q_left = rotation_to_quaternion_wxyz(self.world_T_tcp[left, :3, :3])
        q_right = rotation_to_quaternion_wxyz(self.world_T_tcp[right, :3, :3])
        transform[:3, :3] = quaternion_wxyz_to_rotation(slerp_wxyz(q_left, q_right, alpha))
        return transform

    @staticmethod
    def from_nominal_joint_trajectory(
        time_s: np.ndarray,
        q_nominal: np.ndarray,
        urdf_path: str | Path,
        wrist_frame: str,
        wrist_T_tcp: np.ndarray,
        world_T_planning_root: np.ndarray,
    ) -> "WorldTCPReferenceTrajectory":
        model = pin.buildModelFromUrdf(str(Path(urdf_path).resolve()))
        data = model.createData()
        wrist_frame_id = model.getFrameId(wrist_frame)
        tcp_offset = pin.SE3(
            np.asarray(wrist_T_tcp[:3, :3], dtype=np.float64),
            np.asarray(wrist_T_tcp[:3, 3], dtype=np.float64),
        )
        transforms = []
        for q in np.asarray(q_nominal, dtype=np.float64):
            pin.forwardKinematics(model, data, q)
            pin.updateFramePlacements(model, data)
            root_T_tcp = data.oMf[wrist_frame_id] * tcp_offset
            world_T_tcp = np.asarray(world_T_planning_root, dtype=np.float64) @ root_T_tcp.homogeneous
            transforms.append(world_T_tcp)
        return WorldTCPReferenceTrajectory(
            np.asarray(time_s, dtype=np.float64), np.asarray(transforms, dtype=np.float64)
        )
