from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

from .config import IK_MAX_EVALUATIONS, IK_SUCCESS_TOLERANCE_M, validate_arm
from .robot_model import RobotModel


@dataclass(frozen=True)
class IKResult:
    success: bool
    target_joints: list[float]
    error_mm: float
    message: str
    tcp_position: list[float]
    named_target_joints: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "target_joints": self.target_joints,
            "named_target_joints": self.named_target_joints,
            "error_mm": self.error_mm,
            "message": self.message,
            "tcp_position": self.tcp_position,
        }


class IKSolver:
    def solve(
        self,
        current_joints: list[float] | dict[str, float],
        target_xyz: list[float],
        tcp_offset: list[float],
        arm: str = "left",
    ) -> IKResult:
        raise NotImplementedError


class NumericalIKSolver(IKSolver):
    """Position-only IK wrapper that can later be replaced by Pinocchio or MoveIt."""

    def __init__(self, robot_model: RobotModel):
        self.robot_model = robot_model

    def solve(
        self,
        current_joints: list[float] | dict[str, float],
        target_xyz: list[float],
        tcp_offset: list[float],
        arm: str = "left",
    ) -> IKResult:
        arm = validate_arm(arm)
        joint_names = self.robot_model.arm_joint_names(arm)
        q0 = self.robot_model.coerce_arm_joints(current_joints, arm)
        lower, upper = self.robot_model.joint_limits(joint_names)
        q0 = np.clip(q0, lower, upper)
        target = np.array(target_xyz, dtype=float)
        offset = np.array(tcp_offset, dtype=float)

        def residual(q: np.ndarray) -> np.ndarray:
            position_error = self.robot_model.tcp_position(q, arm, offset) - target
            regularizer = 0.004 * (q - q0)
            return np.concatenate([position_error, regularizer])

        result = least_squares(
            residual,
            q0,
            bounds=(lower, upper),
            max_nfev=IK_MAX_EVALUATIONS,
            xtol=1e-7,
            ftol=1e-7,
            gtol=1e-7,
        )

        q = np.clip(result.x, lower, upper)
        tcp = self.robot_model.tcp_position(q, arm, offset)
        error_m = float(np.linalg.norm(tcp - target))
        success = bool(result.success and error_m <= IK_SUCCESS_TOLERANCE_M)
        message = (
            f"IK converged within {error_m * 1000.0:.1f} mm"
            if success
            else f"IK returned closest pose, residual {error_m * 1000.0:.1f} mm"
        )
        if not result.success:
            message = f"{message}; scipy status={result.status}: {result.message}"

        target_joints = [float(v) for v in q]
        return IKResult(
            success=success,
            target_joints=target_joints,
            named_target_joints=self.robot_model.named_arm_joints(target_joints, arm),
            error_mm=error_m * 1000.0,
            message=message,
            tcp_position=[float(v) for v in tcp],
        )

