from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from core.robot_model import RobotModel
from core.types import IKRequest, IKResult
from core.utils import transform_from_pose

from .base import BaseIKSolver


class NumericalIKSolver(BaseIKSolver):
    name = "numerical"

    def __init__(self, robot_model: RobotModel, default_options: dict[str, Any] | None = None):
        self.robot_model = robot_model
        self.default_options = default_options or {}

    def solve(self, request: IKRequest) -> IKResult:
        options = dict(self.default_options)
        options.update(request.solver_options or {})
        max_iterations = int(options.get("max_iterations", 160))
        tolerance_mm = float(options.get("tolerance_mm", 5.0))
        rotation_tolerance = math.radians(float(options.get("rotation_tolerance_deg", 8.0)))
        position_weight = float(options.get("position_weight", 1.0))
        rotation_weight = float(options.get("rotation_weight", 0.2))
        regularization_weight = float(options.get("regularization_weight", 0.003))
        solve_orientation = bool(options.get("solve_orientation", True))

        q_current = self.robot_model.coerce_chain_joints(request.current_joints, request.chain_id)
        q_seed = (
            self.robot_model.coerce_chain_joints(request.seed, request.chain_id)
            if request.seed is not None
            else q_current.copy()
        )
        lower, upper = self.robot_model.joint_limits(request.chain_id, request.joint_names)
        q_seed = np.clip(q_seed, lower, upper)

        target_matrix = transform_from_pose(request.target_pose)
        target_xyz = target_matrix[:3, 3]
        target_rot = target_matrix[:3, :3]

        def residual(q: np.ndarray) -> np.ndarray:
            tcp_matrix = self.robot_model.tcp_matrix(q, request.chain_id, request.tcp_offset)
            position_error = (tcp_matrix[:3, 3] - target_xyz) * position_weight
            parts = [position_error]
            if solve_orientation:
                rot_error = _rotation_error_vector(tcp_matrix[:3, :3], target_rot) * rotation_weight
                parts.append(rot_error)
            if regularization_weight > 0.0:
                parts.append((q - q_seed) * regularization_weight)
            return np.concatenate(parts)

        result = least_squares(
            residual,
            q_seed,
            bounds=(lower, upper),
            max_nfev=max_iterations,
            xtol=1e-8,
            ftol=1e-8,
            gtol=1e-8,
        )

        q = np.clip(result.x, lower, upper)
        tcp_matrix = self.robot_model.tcp_matrix(q, request.chain_id, request.tcp_offset)
        tcp_pose = self.robot_model.tcp_pose(q, request.chain_id, request.tcp_offset)
        error_position = float(np.linalg.norm(tcp_matrix[:3, 3] - target_xyz))
        error_rotation = float(np.linalg.norm(_rotation_error_vector(tcp_matrix[:3, :3], target_rot)))
        success = bool(
            result.success
            and error_position <= tolerance_mm / 1000.0
            and (not solve_orientation or error_rotation <= rotation_tolerance)
        )
        if success:
            message = f"IK converged: {error_position * 1000.0:.2f} mm, {math.degrees(error_rotation):.2f} deg."
        else:
            message = f"IK returned closest pose: {error_position * 1000.0:.2f} mm, {math.degrees(error_rotation):.2f} deg."
        if not result.success:
            message = f"{message} scipy status={result.status}: {result.message}"

        return IKResult(
            success=success,
            target_joints=[float(v) for v in q],
            named_target_joints=self.robot_model.named_chain_joints(q, request.chain_id),
            error_position=error_position,
            error_rotation=error_rotation,
            error_mm=error_position * 1000.0,
            iterations=int(result.nfev),
            message=message,
            tcp_pose=tcp_pose,
        )


def _rotation_error_vector(current: np.ndarray, target: np.ndarray) -> np.ndarray:
    delta = target @ current.T
    return Rotation.from_matrix(delta).as_rotvec()
