from __future__ import annotations

import numpy as np

from core.robot_model import RobotModel
from core.types import TrajectoryRequest, Waypoint

from .base import BaseTrajectoryPlanner


class LinearTrajectoryPlanner(BaseTrajectoryPlanner):
    name = "linear"

    def __init__(self, robot_model: RobotModel):
        self.robot_model = robot_model

    def plan(self, request: TrajectoryRequest) -> list[Waypoint]:
        return _interpolate(self.robot_model, request, lambda u: u)


def _interpolate(robot_model: RobotModel, request: TrajectoryRequest, blend) -> list[Waypoint]:
    steps = max(2, min(1000, int(request.steps)))
    duration = max(0.05, float(request.duration))
    q0 = robot_model.coerce_chain_joints(request.current_joints, request.chain_id)
    q1 = robot_model.coerce_chain_joints(request.target_joints, request.chain_id)
    waypoints: list[Waypoint] = []
    for idx in range(steps):
        u = idx / (steps - 1)
        q = q0 + (q1 - q0) * float(blend(u))
        waypoints.append(
            Waypoint(
                index=idx,
                t=float(duration * u),
                joints=[float(v) for v in q],
                named_joints=robot_model.named_chain_joints(q, request.chain_id),
                tcp_pose=robot_model.tcp_pose(q, request.chain_id, request.tcp_offset),
                link_poses=robot_model.link_poses(q, request.chain_id),
            )
        )
    return waypoints
