from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


NumberList = list[float]
JointValues = list[float] | dict[str, float]


@dataclass(frozen=True)
class Pose:
    xyz: NumberList
    rpy: NumberList = field(default_factory=lambda: [0.0, 0.0, 0.0])

    def to_dict(self) -> dict[str, NumberList]:
        return {"xyz": [float(v) for v in self.xyz], "rpy": [float(v) for v in self.rpy]}


@dataclass(frozen=True)
class IKRequest:
    chain_id: str
    current_joints: JointValues
    target_pose: Pose
    tcp_offset: Pose
    base_link: str
    end_link: str
    joint_names: list[str]
    seed: JointValues | None = None
    solver_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IKResult:
    success: bool
    target_joints: NumberList
    named_target_joints: dict[str, float]
    error_position: float
    error_rotation: float
    error_mm: float
    iterations: int
    message: str
    tcp_pose: Pose

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "target_joints": self.target_joints,
            "named_target_joints": self.named_target_joints,
            "error_position": self.error_position,
            "error_rotation": self.error_rotation,
            "error_mm": self.error_mm,
            "iterations": self.iterations,
            "message": self.message,
            "tcp_pose": self.tcp_pose.to_dict(),
        }


@dataclass(frozen=True)
class TrajectoryRequest:
    chain_id: str
    current_joints: JointValues
    target_joints: JointValues
    tcp_offset: Pose
    duration: float
    steps: int
    planner_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Waypoint:
    index: int
    t: float
    joints: NumberList
    named_joints: dict[str, float]
    tcp_pose: Pose
    link_poses: dict[str, Pose]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "t": self.t,
            "joints": self.joints,
            "named_joints": self.named_joints,
            "tcp_pose": self.tcp_pose.to_dict(),
            "link_poses": {name: pose.to_dict() for name, pose in self.link_poses.items()},
        }


class BaseInputProvider(Protocol):
    def get_target_pose(self) -> Pose:
        raise NotImplementedError


class ManualInputProvider:
    def __init__(self, pose: Pose) -> None:
        self.pose = pose

    def get_target_pose(self) -> Pose:
        return self.pose
