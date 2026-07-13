from __future__ import annotations

from core.robot_model import RobotModel
from core.types import TrajectoryRequest, Waypoint

from .base import BaseTrajectoryPlanner
from .linear import _interpolate


class QuinticTrajectoryPlanner(BaseTrajectoryPlanner):
    name = "quintic"

    def __init__(self, robot_model: RobotModel):
        self.robot_model = robot_model

    def plan(self, request: TrajectoryRequest) -> list[Waypoint]:
        return _interpolate(self.robot_model, request, _quintic_blend)


def _quintic_blend(u: float) -> float:
    u = max(0.0, min(1.0, float(u)))
    return 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5
