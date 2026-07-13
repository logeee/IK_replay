from __future__ import annotations

from .robot_model import RobotModel
from .types import JointValues, Pose


def compute_fk(robot: RobotModel, chain_id: str, joints: JointValues, tcp_offset: Pose | None = None) -> dict:
    return {
        "tcp_pose": robot.tcp_pose(joints, chain_id, tcp_offset).to_dict(),
        "link_poses": {name: pose.to_dict() for name, pose in robot.link_poses(joints, chain_id).items()},
    }
