"""Standalone Pink differential-IK controller for the H2 seven-DoF left arm.

This module intentionally has no Isaac Sim imports. The caller supplies measured
joint state and a root-frame TCP target, then sends the returned joint target to
its own command sink.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import pinocchio as pin
import pink
from pink.tasks import DampingTask, FrameTask, PostureTask
from pink.tasks.task import Task
import qpsolvers
import yaml

from control.tool_config import ToolConfig


@dataclass(frozen=True)
class PinkDiagnostics:
    qp_success: bool
    qp_solver: str
    solve_time_s: float
    position_error_m: float
    orientation_error_rad: float
    qdot_norm_rad_s: float
    qdot_max_abs_rad_s: float
    measured_dq_norm_rad_s: float
    message: str = ""
    # Position-limit crossings are phase-local diagnostics when the caller
    # selects WARN.  The executor still applies its independent hard limits.
    joint_limit_policy: str = "STRICT"
    joint_limit_warning: bool = False
    joint_limit_warning_joints: tuple[str, ...] = ()
    joint_limit_warning_max_rad: float = 0.0
    # Solver output after PINK's model velocity limits (not an unconstrained
    # demand; use compute_unconstrained_diagnostic() for that).
    qdot_unclipped_rad_s: tuple[float, ...] = ()

    def to_dict(self) -> dict[str, object]:
        values = asdict(self)
        values["qdot_solver_limited_rad_s"] = values["qdot_unclipped_rad_s"]
        return values

    @property
    def qdot_solver_limited_rad_s(self) -> tuple[float, ...]:
        """Compatibility-safe name for the model-limited solver output."""
        return self.qdot_unclipped_rad_s


def _as_se3(transform: pin.SE3 | np.ndarray) -> pin.SE3:
    if isinstance(transform, pin.SE3):
        return transform.copy()
    matrix = np.asarray(transform, dtype=np.float64)
    if matrix.shape != (4, 4):
        raise ValueError(f"Expected a 4x4 transform, got {matrix.shape}")
    return pin.SE3(matrix[:3, :3], matrix[:3, 3])


class NullspaceQdotContinuityTask(Task):
    """Penalize qdot changes only in the primary frame-task nullspace."""

    def __init__(self, frame_task: FrameTask, cost: float) -> None:
        super().__init__(cost=float(cost), gain=1.0, lm_damping=0.0)
        self.frame_task = frame_task
        self.previous_delta = None

    def set_last_integration(self, qdot_previous: np.ndarray, dt_s: float) -> None:
        self.previous_delta = np.asarray(qdot_previous, dtype=np.float64) * float(
            dt_s
        )

    def _projector(self, configuration: pink.Configuration) -> np.ndarray:
        jacobian = self.frame_task.compute_jacobian(configuration)
        return np.eye(configuration.model.nv) - np.linalg.pinv(
            jacobian, rcond=1.0e-4
        ) @ jacobian

    def compute_jacobian(self, configuration: pink.Configuration) -> np.ndarray:
        return self._projector(configuration)

    def compute_error(self, configuration: pink.Configuration) -> np.ndarray:
        previous = (
            np.zeros(configuration.model.nv, dtype=np.float64)
            if self.previous_delta is None
            else self.previous_delta
        )
        return -self._projector(configuration) @ previous

    def __repr__(self) -> str:
        return f"NullspaceQdotContinuityTask(cost={self.cost})"


class PinkArmController:
    """Feedback QP for one fixed-base, seven-joint arm model."""

    def __init__(
        self,
        urdf_path: str | Path,
        config: dict[str, Any],
        tool_config: ToolConfig | None = None,
        joint_limit_policy: str | None = None,
    ):
        self.urdf_path = Path(urdf_path).expanduser().resolve()
        self.config = config
        self.model_cfg = config["model"]
        self.pink_cfg = config["pink"]
        configured_policy = (
            self.pink_cfg.get("joint_limit_policy", "STRICT")
            if joint_limit_policy is None
            else joint_limit_policy
        )
        self.joint_limit_policy = self._normalize_joint_limit_policy(
            configured_policy
        )
        if tool_config is None:
            if "tcp" not in config:
                raise ValueError("A separate ToolConfig is required")
            tool_config = ToolConfig.from_legacy_tcp_mapping(config["tcp"])
        self.tool_config = tool_config
        self.joint_names = tuple(self.model_cfg["controlled_joints"])
        self.root_frame = str(self.model_cfg["root_frame"])
        self.wrist_frame = str(self.model_cfg["wrist_frame"])
        self.tcp_frame = self.tool_config.name
        self.qp_solver = str(self.pink_cfg["qp_solver"])
        if self.qp_solver not in qpsolvers.available_solvers:
            raise RuntimeError(
                f"QP solver {self.qp_solver!r} is unavailable; available={qpsolvers.available_solvers}"
            )

        self.model = pin.buildModelFromUrdf(str(self.urdf_path))
        model_joint_names = tuple(self.model.names[index] for index in range(1, self.model.njoints))
        if model_joint_names != self.joint_names:
            raise RuntimeError(
                f"URDF joint order {model_joint_names} does not match controller order {self.joint_names}"
            )
        if self.model.nq != 7 or self.model.nv != 7:
            raise RuntimeError(f"Pink arm model must be fixed-base 7-DoF, got nq={self.model.nq}, nv={self.model.nv}")

        self._urdf_lower_position_limit = np.asarray(
            self.model.lowerPositionLimit, dtype=np.float64
        ).copy()
        self._urdf_upper_position_limit = np.asarray(
            self.model.upperPositionLimit, dtype=np.float64
        ).copy()

        tcp_parent = self.tool_config.parent_link
        if tcp_parent != self.wrist_frame:
            raise RuntimeError(f"TCP parent {tcp_parent!r} must match wrist frame {self.wrist_frame!r}")
        parent_frame_id = self.model.getFrameId(tcp_parent)
        if parent_frame_id >= self.model.nframes:
            raise RuntimeError(f"TCP parent frame {tcp_parent!r} does not exist")
        parent_frame = self.model.frames[parent_frame_id]
        self.wrist_T_tcp = pin.SE3(
            self.tool_config.wrist_T_tcp[:3, :3],
            self.tool_config.wrist_T_tcp[:3, 3],
        )
        tcp_frame = pin.Frame(
            self.tcp_frame,
            parent_frame.parentJoint,
            parent_frame_id,
            parent_frame.placement * self.wrist_T_tcp,
            pin.FrameType.OP_FRAME,
        )
        self.model.addFrame(tcp_frame, False)

        configured_limit = float(self.pink_cfg["max_joint_velocity_rad_s"])
        self._urdf_velocity_limit = np.asarray(
            self.model.velocityLimit, dtype=np.float64
        ).copy()
        self.model.velocityLimit = np.minimum(
            self._urdf_velocity_limit,
            np.full(self.model.nv, configured_limit, dtype=np.float64),
        )
        self.data = self.model.createData()
        initial_q = np.zeros(self.model.nq, dtype=np.float64)
        self.configuration = pink.Configuration(self.model, self.data, initial_q)
        self.frame_task = FrameTask(
            self.tcp_frame,
            position_cost=float(self.pink_cfg["position_cost"]),
            orientation_cost=float(self.pink_cfg["orientation_cost"]),
            gain=float(self.pink_cfg["frame_gain"]),
            lm_damping=float(self.pink_cfg["frame_lm_damping"]),
        )
        self.posture_task = PostureTask(cost=float(self.pink_cfg["posture_cost"]))
        self.damping_task = DampingTask(cost=float(self.pink_cfg["damping_cost"]))
        self.tasks = [self.frame_task, self.posture_task, self.damping_task]
        self.low_acceleration_task: NullspaceQdotContinuityTask | None = None
        self._previous_qdot = np.zeros(self.model.nv, dtype=np.float64)
        self._max_joint_velocity = configured_limit
        self._is_reset = False

    @classmethod
    def from_yaml(
        cls,
        config_path: str | Path,
        tool_config_path: str | Path | None = None,
        *,
        joint_limit_policy: str | None = None,
    ) -> "PinkArmController":
        config_path = Path(config_path).expanduser().resolve()
        with config_path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        urdf_spec = Path(config["model"]["urdf_path"])
        if urdf_spec.is_absolute():
            urdf_path = urdf_spec.resolve()
        else:
            candidates = [parent / urdf_spec for parent in config_path.parents]
            urdf_path = next((path.resolve() for path in candidates if path.is_file()), None)
            if urdf_path is None:
                raise FileNotFoundError(
                    f"Could not resolve URDF {urdf_spec} from {config_path}"
                )
        tool_config = (
            ToolConfig.from_yaml(tool_config_path)
            if tool_config_path is not None
            else None
        )
        return cls(
            urdf_path,
            config,
            tool_config,
            joint_limit_policy=joint_limit_policy,
        )

    def reset(self, q_actual: np.ndarray) -> None:
        q = self._validate_joint_vector("q_actual", q_actual)
        self.configuration.update(q)
        self.posture_task.set_target(q)
        self.frame_task.set_target(
            self.configuration.get_transform_frame_to_world(self.tcp_frame)
        )
        self._previous_qdot.fill(0.0)
        self._is_reset = True

    @property
    def max_joint_velocity_rad_s(self) -> float:
        return float(self._max_joint_velocity)

    def set_posture_reference(self, q_reference: np.ndarray) -> None:
        """Update the secondary posture task without changing the primary TCP task."""
        q = self._validate_joint_vector("q_reference", q_reference)
        self.posture_task.set_target(q)

    def set_posture_cost(self, cost: float) -> None:
        """Set the secondary posture cost for diagnostic counterfactuals."""
        value = float(cost)
        if not np.isfinite(value) or value < 0.0:
            raise ValueError("posture cost must be finite and non-negative")
        self.posture_task.cost = value

    def compute_unconstrained_diagnostic(
        self,
        q_actual: np.ndarray,
        dq_actual: np.ndarray,
        T_root_tcp_desired: pin.SE3 | np.ndarray,
        dt: float,
        *,
        diagnostic_velocity_limit_rad_s: float = 100.0,
    ) -> tuple[np.ndarray, np.ndarray, PinkDiagnostics]:
        """Solve the same PINK task without the runtime velocity bound.

        This is intentionally diagnostic-only. It temporarily raises the
        model velocity limits and the explicit command ceiling, then restores
        both settings before returning. Callers must never use this result for
        a robot command.
        """
        limit = float(diagnostic_velocity_limit_rad_s)
        if not np.isfinite(limit) or limit <= 0.0:
            raise ValueError("diagnostic velocity limit must be positive and finite")
        saved_model_limits = np.asarray(self.model.velocityLimit, dtype=np.float64).copy()
        saved_ceiling = float(self._max_joint_velocity)
        saved_previous = self._previous_qdot.copy()
        try:
            self.model.velocityLimit = np.full(self.model.nv, limit, dtype=np.float64)
            self._max_joint_velocity = limit
            return self.compute(q_actual, dq_actual, T_root_tcp_desired, dt)
        finally:
            self.model.velocityLimit = saved_model_limits
            self._max_joint_velocity = saved_ceiling
            self._previous_qdot = saved_previous

    def set_joint_velocity_ceiling(self, limit_rad_s: float) -> None:
        """Set an explicit phase-local PINK velocity ceiling."""
        limit = float(limit_rad_s)
        if not np.isfinite(limit) or limit <= 0.0:
            raise ValueError("joint velocity ceiling must be positive and finite")
        self.model.velocityLimit = np.minimum(
            self._urdf_velocity_limit,
            np.full(self.model.nv, limit, dtype=np.float64),
        )
        self._max_joint_velocity = limit

    def set_position_limits(
        self,
        lower_position_limit: np.ndarray,
        upper_position_limit: np.ndarray,
    ) -> None:
        """Use execution-command limits for both the PINK QP and integration."""
        lower = self._validate_joint_vector(
            "lower_position_limit", lower_position_limit
        )
        upper = self._validate_joint_vector(
            "upper_position_limit", upper_position_limit
        )
        lower = np.maximum(lower, self._urdf_lower_position_limit)
        upper = np.minimum(upper, self._urdf_upper_position_limit)
        if np.any(lower >= upper):
            raise ValueError("execution limits do not overlap URDF hard limits")
        self.model.lowerPositionLimit = lower
        self.model.upperPositionLimit = upper

    def set_qdot_continuity_regularization(self, cost: float) -> None:
        """Enable a secondary low-acceleration task without filtering q targets."""
        value = float(cost)
        if not np.isfinite(value) or value < 0.0:
            raise ValueError("qdot continuity cost must be finite and non-negative")
        if value == 0.0:
            self.low_acceleration_task = None
            self.tasks = [self.frame_task, self.posture_task, self.damping_task]
        else:
            self.low_acceleration_task = NullspaceQdotContinuityTask(
                self.frame_task, cost=value
            )
            self.tasks = [
                self.frame_task,
                self.posture_task,
                self.low_acceleration_task,
                self.damping_task,
            ]
        self._previous_qdot.fill(0.0)

    def compute(
        self,
        q_actual: np.ndarray,
        dq_actual: np.ndarray,
        T_root_tcp_desired: pin.SE3 | np.ndarray,
        dt: float,
    ) -> tuple[np.ndarray, np.ndarray, PinkDiagnostics]:
        """Return ``qdot_cmd``, one-step ``q_target`` and solver diagnostics."""
        if not self._is_reset:
            raise RuntimeError("Call reset(q_actual) before compute()")
        if not np.isfinite(dt) or dt <= 0.0:
            raise ValueError(f"dt must be positive and finite, got {dt}")
        q = self._validate_joint_vector("q_actual", q_actual)
        dq = self._validate_joint_vector("dq_actual", dq_actual)
        target = _as_se3(T_root_tcp_desired)
        self.configuration.update(q)
        self.frame_task.set_target(target)
        if self.low_acceleration_task is not None:
            self.low_acceleration_task.set_last_integration(
                self._previous_qdot, dt
            )
        task_error = self.frame_task.compute_error(self.configuration)
        lower = np.asarray(self.model.lowerPositionLimit, dtype=np.float64)
        upper = np.asarray(self.model.upperPositionLimit, dtype=np.float64)
        warning_indices = set(
            np.flatnonzero((q < lower) | (q > upper)).astype(int).tolist()
        )
        warning_max = float(
            max(
                np.max(np.maximum(lower - q, 0.0)),
                np.max(np.maximum(q - upper, 0.0)),
            )
        )
        solver_safety_break = self.joint_limit_policy != "WARN"
        limit_error_detected = False
        qdot_unclipped = np.zeros(self.model.nv, dtype=np.float64)
        start = perf_counter()
        try:
            qdot_cmd = pink.solve_ik(
                self.configuration,
                self.tasks,
                dt,
                solver=self.qp_solver,
                damping=float(self.pink_cfg["qp_damping"]),
                safety_break=solver_safety_break,
            )
            solve_time = perf_counter() - start
            qdot_unclipped = np.asarray(qdot_cmd, dtype=np.float64).reshape(self.model.nv)
            qdot_cmd = qdot_unclipped.copy()
            qdot_cmd = np.clip(qdot_cmd, -self._max_joint_velocity, self._max_joint_velocity)
            integrated_target = np.asarray(
                self.configuration.integrate(qdot_cmd, dt), dtype=np.float64
            )
            integrated_violation = np.maximum(lower - integrated_target, 0.0)
            integrated_violation = np.maximum(
                integrated_violation,
                np.maximum(integrated_target - upper, 0.0),
            )
            warning_indices.update(
                np.flatnonzero(integrated_violation > 0.0).astype(int).tolist()
            )
            warning_max = max(warning_max, float(np.max(integrated_violation)))
            q_target = np.clip(integrated_target, lower, upper)
            success = bool(np.all(np.isfinite(qdot_cmd)) and np.all(np.isfinite(q_target)))
            message = "" if success else "non-finite Pink output"
            if not success:
                qdot_cmd = np.zeros(self.model.nv, dtype=np.float64)
                q_target = q.copy()
        except Exception as error:
            solve_time = perf_counter() - start
            error_text = f"{type(error).__name__}: {error}"
            is_limit_error = self._looks_like_joint_limit_error(error_text)
            if self.joint_limit_policy == "WARN" and is_limit_error:
                limit_error_detected = True
                # Keep the control cycle alive for validation/replay.  The
                # hard-limit check in the executor remains authoritative.
                qdot_cmd = np.zeros(self.model.nv, dtype=np.float64)
                q_target = np.clip(q, lower, upper)
                warning_indices.update(
                    np.flatnonzero((q < lower) | (q > upper)).astype(int).tolist()
                )
                warning_max = max(
                    warning_max,
                    float(
                        max(
                            np.max(np.maximum(lower - q, 0.0)),
                            np.max(np.maximum(q - upper, 0.0)),
                        )
                    ),
                )
                # A warning policy changes the handling of the limit crossing,
                # not the meaning of solver success.  The solve raised before
                # producing a valid QP result, so callers must see a failure
                # and let the supervisor decide whether to recover/hold.
                success = False
                message = f"joint-limit warning: {error_text}"
            else:
                qdot_cmd = np.zeros(self.model.nv, dtype=np.float64)
                q_target = q.copy()
                success = False
                message = error_text
        self._previous_qdot = qdot_cmd.copy()
        warning_joints = tuple(self.joint_names[index] for index in sorted(warning_indices))
        if warning_joints and not message:
            message = "joint-limit warning: " + ", ".join(warning_joints)
        diagnostics = PinkDiagnostics(
            qp_success=success,
            qp_solver=self.qp_solver,
            solve_time_s=solve_time,
            position_error_m=float(np.linalg.norm(task_error[:3])),
            orientation_error_rad=float(np.linalg.norm(task_error[3:])),
            qdot_norm_rad_s=float(np.linalg.norm(qdot_cmd)),
            qdot_max_abs_rad_s=float(np.max(np.abs(qdot_cmd))),
            measured_dq_norm_rad_s=float(np.linalg.norm(dq)),
            message=message,
            joint_limit_policy=self.joint_limit_policy,
            joint_limit_warning=bool(warning_joints) or limit_error_detected,
            joint_limit_warning_joints=warning_joints,
            joint_limit_warning_max_rad=float(warning_max),
            qdot_unclipped_rad_s=tuple(float(value) for value in qdot_unclipped),
        )
        return qdot_cmd, q_target, diagnostics

    def root_T_tcp_actual(self, q_actual: np.ndarray) -> pin.SE3:
        q = self._validate_joint_vector("q_actual", q_actual)
        self.configuration.update(q)
        return self.configuration.get_transform_frame_to_world(self.tcp_frame)

    def _validate_joint_vector(self, name: str, value: np.ndarray) -> np.ndarray:
        vector = np.asarray(value, dtype=np.float64).reshape(-1)
        if vector.shape != (self.model.nq,):
            raise ValueError(f"{name} must have shape ({self.model.nq},), got {vector.shape}")
        if not np.all(np.isfinite(vector)):
            raise ValueError(f"{name} contains NaN or infinity")
        return vector.copy()

    @staticmethod
    def _normalize_joint_limit_policy(policy: str) -> str:
        normalized = str(policy).strip().upper()
        if normalized not in {"STRICT", "WARN"}:
            raise ValueError("joint_limit_policy must be STRICT or WARN")
        return normalized

    @staticmethod
    def _looks_like_joint_limit_error(message: str) -> bool:
        text = str(message).lower()
        return "limit" in text and ("joint" in text or "position" in text)
