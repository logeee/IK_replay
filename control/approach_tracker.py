"""Simulator-independent tracking of a WORLD TCP Approach reference."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

import numpy as np
import pinocchio as pin

from control.base_pose_predictor import (
    BasePosePredictor,
    RAW_PREDICTION,
    ROOT_PREDICTION_MODES,
    RootPredictionDiagnostics,
)
from control.interfaces import ArmState
from control.trajectory_reference import WorldTCPReferenceTrajectory


class DifferentialIKController(Protocol):
    def reset(self, q_actual: np.ndarray) -> None: ...

    def compute(
        self,
        q_actual: np.ndarray,
        dq_actual: np.ndarray,
        T_root_tcp_desired: np.ndarray,
        dt: float,
    ) -> tuple[np.ndarray, np.ndarray, object]: ...


@dataclass(frozen=True)
class ApproachTrackerConfig:
    trajectory_preview_s: float = 0.0667
    root_prediction_s: float = 0.0667
    predictor_history: int = 8
    root_prediction_mode: str = RAW_PREDICTION
    twist_filter_enabled: bool = True
    twist_filter_time_constant_s: float = 0.08
    root_linear_velocity_clamp_m_s: float = np.inf
    root_angular_velocity_clamp_rad_s: float = np.inf

    def __post_init__(self) -> None:
        if not np.isfinite(self.trajectory_preview_s) or self.trajectory_preview_s < 0.0:
            raise ValueError("trajectory_preview_s must be finite and non-negative")
        if not np.isfinite(self.root_prediction_s) or self.root_prediction_s < 0.0:
            raise ValueError("root_prediction_s must be finite and non-negative")
        if self.predictor_history < 2:
            raise ValueError("predictor_history must be at least two")
        if self.root_prediction_mode not in ROOT_PREDICTION_MODES:
            raise ValueError(
                f"Unknown root_prediction_mode {self.root_prediction_mode!r}; "
                f"choices={sorted(ROOT_PREDICTION_MODES)}"
            )
        if (
            not np.isfinite(self.twist_filter_time_constant_s)
            or self.twist_filter_time_constant_s <= 0.0
        ):
            raise ValueError("twist_filter_time_constant_s must be positive and finite")
        for name, value in (
            ("root_linear_velocity_clamp_m_s", self.root_linear_velocity_clamp_m_s),
            ("root_angular_velocity_clamp_rad_s", self.root_angular_velocity_clamp_rad_s),
        ):
            if np.isnan(value) or value <= 0.0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "ApproachTrackerConfig":
        prediction = values.get("root_prediction", {})
        if prediction is None:
            prediction = {}
        if not isinstance(prediction, Mapping):
            raise ValueError("root_prediction must be a mapping")
        twist_filter = prediction.get("twist_filter", {})
        clamp = prediction.get("clamp", {})
        if not isinstance(twist_filter, Mapping) or not isinstance(clamp, Mapping):
            raise ValueError("root prediction filter and clamp must be mappings")
        return cls(
            trajectory_preview_s=float(values.get("trajectory_preview_s", 0.0667)),
            root_prediction_s=float(
                prediction.get(
                    "horizon_s", values.get("root_prediction_s", 0.0667)
                )
            ),
            predictor_history=int(values.get("predictor_history", 8)),
            root_prediction_mode=str(
                prediction.get(
                    "mode", values.get("root_prediction_mode", RAW_PREDICTION)
                )
            ).upper(),
            twist_filter_enabled=bool(twist_filter.get("enabled", True)),
            twist_filter_time_constant_s=float(
                twist_filter.get("time_constant_s", 0.08)
            ),
            root_linear_velocity_clamp_m_s=float(
                clamp.get("linear_velocity_m_s", np.inf)
            ),
            root_angular_velocity_clamp_rad_s=float(
                clamp.get("angular_velocity_rad_s", np.inf)
            ),
        )


@dataclass(frozen=True)
class ApproachTrackerOutput:
    timestamp_s: float
    true_reference_world_T_tcp: np.ndarray
    control_reference_world_T_tcp: np.ndarray
    root_for_control_world_T_root: np.ndarray
    control_target_root_T_tcp: np.ndarray
    qdot_cmd: np.ndarray
    q_target: np.ndarray
    controller_diagnostics: object
    root_prediction_diagnostics: RootPredictionDiagnostics
    live_target_root_T_tcp: np.ndarray
    feedback_task_twist_tcp: np.ndarray
    feedforward_task_twist_tcp: np.ndarray
    total_task_twist_tcp: np.ndarray


class ApproachTracker:
    """Apply preview and predicted-root compensation before standalone PINK.

    Tracking evaluation must use ``true_reference_world_T_tcp``. The previewed
    pose is only the controller target and is intentionally reported separately.
    """

    def __init__(
        self,
        controller: DifferentialIKController,
        reference: WorldTCPReferenceTrajectory,
        config: ApproachTrackerConfig,
    ) -> None:
        self.controller = controller
        self.reference = reference
        self.config = config
        self.predictor = BasePosePredictor(
            config.predictor_history,
            mode=config.root_prediction_mode,
            filter_enabled=config.twist_filter_enabled,
            filter_time_constant_s=config.twist_filter_time_constant_s,
            max_linear_velocity_m_s=config.root_linear_velocity_clamp_m_s,
            max_angular_velocity_rad_s=config.root_angular_velocity_clamp_rad_s,
        )
        self._last_observation_time: float | None = None
        self._last_observation_pose: np.ndarray | None = None
        self._reset = False

    def reset(
        self,
        q_actual: np.ndarray,
        timestamp_s: float | None = None,
        world_T_root: np.ndarray | None = None,
    ) -> None:
        self.controller.reset(q_actual)
        self.predictor.reset()
        self._last_observation_time = None
        self._last_observation_pose = None
        self._reset = True
        if (timestamp_s is None) != (world_T_root is None):
            raise ValueError("timestamp_s and world_T_root must be supplied together")
        if timestamp_s is not None and world_T_root is not None:
            self.observe_root(timestamp_s, world_T_root)

    def observe_root(self, timestamp_s: float, world_T_root: np.ndarray) -> None:
        if not self._reset:
            raise RuntimeError("Call reset() before adding root observations")
        timestamp = float(timestamp_s)
        pose = np.asarray(world_T_root, dtype=np.float64)
        if pose.shape != (4, 4) or not np.all(np.isfinite(pose)):
            raise ValueError("world_T_root must be one finite homogeneous transform")
        if self._last_observation_time is not None and timestamp == self._last_observation_time:
            if not np.allclose(pose, self._last_observation_pose, atol=1.0e-12):
                raise ValueError("Conflicting root poses at the same timestamp")
            return
        self.predictor.update(timestamp, pose)
        self._last_observation_time = timestamp
        self._last_observation_pose = pose.copy()

    def compute(
        self,
        timestamp_s: float,
        state: ArmState,
        dt: float,
    ) -> ApproachTrackerOutput:
        if not self._reset:
            raise RuntimeError("Call reset() before compute()")
        timestamp = float(timestamp_s)
        if self._last_observation_time != timestamp:
            self.observe_root(timestamp, state.world_T_root)
        elif not np.allclose(
            state.world_T_root, self._last_observation_pose, atol=1.0e-12
        ):
            raise ValueError("ArmState root differs from the observed root pose")

        true_time = min(max(timestamp, 0.0), self.reference.duration_s)
        control_time = min(
            max(timestamp + self.config.trajectory_preview_s, 0.0),
            self.reference.duration_s,
        )
        true_reference = self.reference.sample(true_time)
        control_reference = self.reference.sample(control_time)
        root_for_control = self.predictor.predict(
            timestamp + self.config.root_prediction_s
        )
        live_root_T_target = np.linalg.inv(state.world_T_root) @ true_reference
        root_T_target = np.linalg.inv(root_for_control) @ control_reference
        root_T_tcp_actual = np.linalg.inv(state.world_T_root) @ state.world_T_tcp
        actual = pin.SE3(root_T_tcp_actual[:3, :3], root_T_tcp_actual[:3, 3])
        live_target = pin.SE3(
            live_root_T_target[:3, :3], live_root_T_target[:3, 3]
        )
        control_target = pin.SE3(root_T_target[:3, :3], root_T_target[:3, 3])
        frame_task = getattr(self.controller, "frame_task", None)
        frame_gain = float(getattr(frame_task, "gain", 1.0))
        feedback_twist = (
            frame_gain * pin.log6(actual.inverse() * live_target).vector / dt
        )
        total_twist = (
            frame_gain * pin.log6(actual.inverse() * control_target).vector / dt
        )
        feedforward_twist = total_twist - feedback_twist
        qdot_cmd, q_target, diagnostics = self.controller.compute(
            state.q_actual, state.dq_actual, root_T_target, dt
        )
        return ApproachTrackerOutput(
            timestamp,
            true_reference,
            control_reference,
            root_for_control,
            root_T_target,
            np.asarray(qdot_cmd, dtype=np.float64).copy(),
            np.asarray(q_target, dtype=np.float64).copy(),
            diagnostics,
            self.predictor.diagnostics,
            live_root_T_target.copy(),
            np.asarray(feedback_twist, dtype=np.float64).copy(),
            np.asarray(feedforward_twist, dtype=np.float64).copy(),
            np.asarray(total_twist, dtype=np.float64).copy(),
        )
