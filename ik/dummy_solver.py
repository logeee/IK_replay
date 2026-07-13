from __future__ import annotations

import numpy as np

from core.robot_model import RobotModel
from core.types import IKRequest, IKResult

from .base import BaseIKSolver


class DummyIKSolver(BaseIKSolver):
    name = "dummy"

    def __init__(self, robot_model: RobotModel):
        self.robot_model = robot_model

    def solve(self, request: IKRequest) -> IKResult:
        q = self.robot_model.coerce_chain_joints(request.current_joints, request.chain_id)
        tcp_pose = self.robot_model.tcp_pose(q, request.chain_id, request.tcp_offset)
        error_position = float(np.linalg.norm(np.array(tcp_pose.xyz) - np.array(request.target_pose.xyz)))
        return IKResult(
            success=False,
            target_joints=[float(v) for v in q],
            named_target_joints=self.robot_model.named_chain_joints(q, request.chain_id),
            error_position=error_position,
            error_rotation=0.0,
            error_mm=error_position * 1000.0,
            iterations=0,
            message="Dummy solver returned the current joint state.",
            tcp_pose=tcp_pose,
        )
