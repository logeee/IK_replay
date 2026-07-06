from __future__ import annotations

import numpy as np

from .config import DEFAULT_DURATION, DEFAULT_STEPS, validate_arm
from .robot_model import RobotModel


def quintic_blend(u: float) -> float:
    u = min(1.0, max(0.0, float(u)))
    return 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5


class TrajectoryPlanner:
    def __init__(self, robot_model: RobotModel):
        self.robot_model = robot_model

    def plan(
        self,
        current_joints: list[float] | dict[str, float],
        target_joints: list[float] | dict[str, float],
        arm: str = "left",
        tcp_offset: list[float] | None = None,
        duration: float = DEFAULT_DURATION,
        steps: int = DEFAULT_STEPS,
    ) -> list[dict]:
        arm = validate_arm(arm)
        steps = int(max(2, min(500, steps)))
        duration = float(max(0.1, duration))
        q0 = self.robot_model.coerce_arm_joints(current_joints, arm)
        q1 = self.robot_model.coerce_arm_joints(target_joints, arm)
        waypoints: list[dict] = []

        for idx in range(steps):
            u = idx / (steps - 1)
            s = quintic_blend(u)
            q = q0 + (q1 - q0) * s
            tcp = self.robot_model.tcp_position(q, arm, tcp_offset)
            named = self.robot_model.named_arm_joints(q, arm)
            waypoints.append(
                {
                    "index": idx,
                    "timestamp": duration * u,
                    "joints": [float(v) for v in q],
                    "named_joints": named,
                    "tcp_position": [float(v) for v in tcp],
                    "link_positions": self.robot_model.link_positions(q, arm),
                }
            )
        return waypoints

