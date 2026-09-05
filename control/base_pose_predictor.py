"""Simulator-independent SE(3) constant-twist base-pose prediction."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
import pinocchio as pin


LIVE_ROOT = "LIVE_ROOT"
RAW_PREDICTION = "RAW_PREDICTION"
FILTERED_PREDICTION = "FILTERED_PREDICTION"
ROOT_PREDICTION_MODES = frozenset(
    (LIVE_ROOT, RAW_PREDICTION, FILTERED_PREDICTION)
)


@dataclass(frozen=True)
class RootPredictionDiagnostics:
    mode: str
    measurement_dt_s: float
    prediction_horizon_s: float
    raw_body_twist: np.ndarray
    filtered_body_twist: np.ndarray
    twist_used: np.ndarray
    linear_clamped: bool
    angular_clamped: bool
    predicted_world_T_root: np.ndarray


def _as_se3(transform: np.ndarray) -> pin.SE3:
    matrix = np.asarray(transform, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError("Expected one finite homogeneous transform [4,4]")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1.0e-7):
        raise ValueError("Transform rotation is not orthonormal")
    if not np.isclose(np.linalg.det(rotation), 1.0, atol=1.0e-7):
        raise ValueError("Transform rotation determinant is not +1")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1.0e-12):
        raise ValueError("Invalid homogeneous transform bottom row")
    return pin.SE3(rotation, matrix[:3, 3])


def interpolate_se3(left: np.ndarray, right: np.ndarray, alpha: float) -> np.ndarray:
    """Interpolate two poses along their SE(3) geodesic."""
    if not 0.0 <= alpha <= 1.0:
        raise ValueError("Interpolation alpha must be in [0, 1]")
    left_se3 = _as_se3(left)
    right_se3 = _as_se3(right)
    delta = left_se3.inverse() * right_se3
    return (left_se3 * pin.exp6(pin.log6(delta).vector * float(alpha))).homogeneous


def sample_pose_history(
    timestamps_s: np.ndarray,
    world_T_root: np.ndarray,
    timestamp_s: float,
) -> np.ndarray:
    """Sample timestamped poses without extrapolating beyond the recorded interval."""
    timestamps = np.asarray(timestamps_s, dtype=np.float64)
    transforms = np.asarray(world_T_root, dtype=np.float64)
    if timestamps.ndim != 1 or transforms.shape != (timestamps.size, 4, 4):
        raise ValueError("Expected timestamps [N] and transforms [N,4,4]")
    if timestamps.size == 0 or np.any(np.diff(timestamps) <= 0.0):
        raise ValueError("Pose-history timestamps must be non-empty and increasing")
    timestamp = float(timestamp_s)
    if timestamp < timestamps[0] or timestamp > timestamps[-1]:
        raise ValueError("Requested timestamp is outside the recorded pose history")
    if timestamp == timestamps[-1]:
        return transforms[-1].copy()
    right = int(np.searchsorted(timestamps, timestamp, side="right"))
    left = max(0, right - 1)
    if timestamp == timestamps[left]:
        return transforms[left].copy()
    alpha = (timestamp - timestamps[left]) / (timestamps[right] - timestamps[left])
    return interpolate_se3(transforms[left], transforms[right], float(alpha))


class BasePosePredictor:
    """Predict a future base pose from timestamped pose measurements only."""

    def __init__(
        self,
        max_history: int = 8,
        *,
        mode: str = RAW_PREDICTION,
        filter_enabled: bool = True,
        filter_time_constant_s: float = 0.08,
        max_linear_velocity_m_s: float = np.inf,
        max_angular_velocity_rad_s: float = np.inf,
    ) -> None:
        if max_history < 2:
            raise ValueError("max_history must be at least two")
        mode = str(mode).upper()
        if mode not in ROOT_PREDICTION_MODES:
            raise ValueError(
                f"Unknown root prediction mode {mode!r}; "
                f"choices={sorted(ROOT_PREDICTION_MODES)}"
            )
        if not np.isfinite(filter_time_constant_s) or filter_time_constant_s <= 0.0:
            raise ValueError("filter_time_constant_s must be positive and finite")
        for name, value in (
            ("max_linear_velocity_m_s", max_linear_velocity_m_s),
            ("max_angular_velocity_rad_s", max_angular_velocity_rad_s),
        ):
            if np.isnan(value) or value <= 0.0:
                raise ValueError(f"{name} must be positive")
        self._history: deque[tuple[float, pin.SE3]] = deque(maxlen=max_history)
        self.mode = mode
        self.filter_enabled = bool(filter_enabled)
        self.filter_time_constant_s = float(filter_time_constant_s)
        self.max_linear_velocity_m_s = float(max_linear_velocity_m_s)
        self.max_angular_velocity_rad_s = float(max_angular_velocity_rad_s)
        self._raw_twist = np.zeros(6, dtype=np.float64)
        self._filtered_twist = np.zeros(6, dtype=np.float64)
        self._measurement_dt_s = 0.0
        self._filter_ready = False
        self._diagnostics: RootPredictionDiagnostics | None = None

    @property
    def sample_count(self) -> int:
        return len(self._history)

    @property
    def ready(self) -> bool:
        return len(self._history) >= 2

    def reset(self) -> None:
        self._history.clear()
        self._raw_twist.fill(0.0)
        self._filtered_twist.fill(0.0)
        self._measurement_dt_s = 0.0
        self._filter_ready = False
        self._diagnostics = None

    @property
    def diagnostics(self) -> RootPredictionDiagnostics:
        if self._diagnostics is None:
            raise RuntimeError("Call predict() before requesting diagnostics")
        return self._diagnostics

    def update(self, timestamp_s: float, world_T_root: np.ndarray) -> None:
        timestamp = float(timestamp_s)
        if not np.isfinite(timestamp):
            raise ValueError("Timestamp must be finite")
        if self._history and timestamp <= self._history[-1][0]:
            raise ValueError("Base-pose timestamps must be strictly increasing")
        pose = _as_se3(world_T_root)
        if self._history:
            previous_time, previous_pose = self._history[-1]
            measurement_dt = timestamp - previous_time
            raw_twist = (
                pin.log6(previous_pose.inverse() * pose).vector / measurement_dt
            )
            if not self.filter_enabled:
                self._filtered_twist = raw_twist.copy()
                self._filter_ready = True
            elif self._filter_ready:
                alpha = 1.0 - np.exp(
                    -measurement_dt / self.filter_time_constant_s
                )
                self._filtered_twist += alpha * (
                    raw_twist - self._filtered_twist
                )
            else:
                self._filtered_twist = raw_twist.copy()
                self._filter_ready = True
            self._raw_twist = raw_twist
            self._measurement_dt_s = measurement_dt
        self._history.append((timestamp, pose))

    @staticmethod
    def _clamp_vector(vector: np.ndarray, limit: float) -> tuple[np.ndarray, bool]:
        norm = float(np.linalg.norm(vector))
        if norm <= limit:
            return vector.copy(), False
        return vector * (limit / norm), True

    def predict(self, timestamp_s: float) -> np.ndarray:
        if not self._history:
            raise RuntimeError("Cannot predict without a base-pose measurement")
        target_time = float(timestamp_s)
        latest_time, latest_pose = self._history[-1]
        if target_time < latest_time:
            raise ValueError("BasePosePredictor only extrapolates forward")
        horizon = target_time - latest_time
        linear_clamped = False
        angular_clamped = False
        if self.mode == LIVE_ROOT or not self.ready or horizon == 0.0:
            twist_used = np.zeros(6, dtype=np.float64)
            predicted = latest_pose.homogeneous.copy()
        else:
            twist_used = (
                self._raw_twist.copy()
                if self.mode == RAW_PREDICTION
                else self._filtered_twist.copy()
            )
            if self.mode == FILTERED_PREDICTION:
                twist_used[:3], linear_clamped = self._clamp_vector(
                    twist_used[:3], self.max_linear_velocity_m_s
                )
                twist_used[3:], angular_clamped = self._clamp_vector(
                    twist_used[3:], self.max_angular_velocity_rad_s
                )
            predicted = (latest_pose * pin.exp6(twist_used * horizon)).homogeneous
        self._diagnostics = RootPredictionDiagnostics(
            mode=self.mode,
            measurement_dt_s=self._measurement_dt_s,
            prediction_horizon_s=horizon,
            raw_body_twist=self._raw_twist.copy(),
            filtered_body_twist=self._filtered_twist.copy(),
            twist_used=twist_used,
            linear_clamped=linear_clamped,
            angular_clamped=angular_clamped,
            predicted_world_T_root=predicted.copy(),
        )
        return predicted
