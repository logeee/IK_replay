from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .robot_config import PROJECT_ROOT, RobotConfig, project_relative
from .types import JointValues, Pose
from .utils import (
    axis_angle_matrix,
    parse_vec3,
    pose_from_matrix,
    transform_from_pose,
    transform_from_rotation_translation,
    transform_from_xyz_rpy,
    translation_matrix,
)


@dataclass(frozen=True)
class Visual:
    filename: str | None
    xyz: np.ndarray
    rpy: np.ndarray
    scale: np.ndarray
    color: list[float] | None = None


@dataclass(frozen=True)
class Link:
    name: str
    visuals: list[Visual] = field(default_factory=list)


@dataclass(frozen=True)
class Joint:
    name: str
    joint_type: str
    parent: str
    child: str
    xyz: np.ndarray
    rpy: np.ndarray
    axis: np.ndarray
    lower: float
    upper: float


class RobotModel:
    """Generic URDF loader and FK engine for offline IK debugging."""

    def __init__(self, config: RobotConfig):
        self.config = config
        self.urdf_path = Path(config.urdf_path)
        if not self.urdf_path.exists():
            raise FileNotFoundError(f"URDF not found: {self.urdf_path}")
        self.links: dict[str, Link] = {}
        self.joints: dict[str, Joint] = {}
        self.parent_to_joints: dict[str, list[Joint]] = {}
        self.child_to_joint: dict[str, Joint] = {}
        self.root_links: list[str] = []
        self._load_urdf()
        self._validate_config()

    @property
    def chain_ids(self) -> list[str]:
        return list(self.config.chains)

    def chain_config(self, chain_id: str):
        if chain_id not in self.config.chains:
            raise ValueError(f"unknown chain_id {chain_id!r}; available: {self.chain_ids}")
        return self.config.chains[chain_id]

    def joint_names(self, chain_id: str) -> list[str]:
        return list(self.chain_config(chain_id).joints)

    def base_link(self, chain_id: str) -> str:
        return self.chain_config(chain_id).base_link

    def end_link(self, chain_id: str) -> str:
        return self.chain_config(chain_id).end_link

    def tcp_offset(self, chain_id: str) -> Pose:
        self.chain_config(chain_id)
        return self.config.tcp_offsets[chain_id]

    def _load_urdf(self) -> None:
        root = ET.parse(self.urdf_path).getroot()
        for link_el in root.findall("link"):
            name = link_el.attrib["name"]
            visuals: list[Visual] = []
            for visual_el in link_el.findall("visual"):
                origin_el = visual_el.find("origin")
                geometry_el = visual_el.find("geometry")
                mesh_el = geometry_el.find("mesh") if geometry_el is not None else None
                filename = mesh_el.attrib.get("filename") if mesh_el is not None else None
                scale = parse_vec3(mesh_el.attrib.get("scale") if mesh_el is not None else None, (1.0, 1.0, 1.0))
                material_el = visual_el.find("material")
                color_el = material_el.find("color") if material_el is not None else None
                color = None
                if color_el is not None and color_el.attrib.get("rgba"):
                    color = [float(part) for part in color_el.attrib["rgba"].split()]
                visuals.append(
                    Visual(
                        filename=filename,
                        xyz=parse_vec3(origin_el.attrib.get("xyz") if origin_el is not None else None),
                        rpy=parse_vec3(origin_el.attrib.get("rpy") if origin_el is not None else None),
                        scale=scale,
                        color=color,
                    )
                )
            self.links[name] = Link(name=name, visuals=visuals)

        for joint_el in root.findall("joint"):
            parent_el = joint_el.find("parent")
            child_el = joint_el.find("child")
            if parent_el is None or child_el is None:
                continue
            origin_el = joint_el.find("origin")
            axis_el = joint_el.find("axis")
            limit_el = joint_el.find("limit")
            joint_type = joint_el.attrib.get("type", "fixed")
            lower, upper = self._default_limits(joint_type)
            if limit_el is not None:
                lower = float(limit_el.attrib.get("lower", lower))
                upper = float(limit_el.attrib.get("upper", upper))
            joint = Joint(
                name=joint_el.attrib["name"],
                joint_type=joint_type,
                parent=parent_el.attrib["link"],
                child=child_el.attrib["link"],
                xyz=parse_vec3(origin_el.attrib.get("xyz") if origin_el is not None else None),
                rpy=parse_vec3(origin_el.attrib.get("rpy") if origin_el is not None else None),
                axis=parse_vec3(axis_el.attrib.get("xyz") if axis_el is not None else None, (0.0, 0.0, 1.0)),
                lower=lower,
                upper=upper,
            )
            self.joints[joint.name] = joint
            self.parent_to_joints.setdefault(joint.parent, []).append(joint)
            self.child_to_joint[joint.child] = joint

        child_links = set(self.child_to_joint)
        self.root_links = [name for name in self.links if name not in child_links]

    @staticmethod
    def _default_limits(joint_type: str) -> tuple[float, float]:
        if joint_type == "continuous":
            return -math.pi, math.pi
        if joint_type == "prismatic":
            return -0.5, 0.5
        if joint_type == "fixed":
            return 0.0, 0.0
        return -math.pi, math.pi

    def _validate_config(self) -> None:
        for chain_id, chain in self.config.chains.items():
            missing_joints = [name for name in chain.joints if name not in self.joints]
            missing_links = [name for name in [chain.base_link, chain.end_link] if name not in self.links]
            if missing_joints:
                raise ValueError(f"configured joints not found in URDF for {chain_id}: {missing_joints}")
            if missing_links:
                raise ValueError(f"configured links not found in URDF for {chain_id}: {missing_links}")

    def coerce_chain_joints(self, joints: JointValues | None, chain_id: str) -> np.ndarray:
        names = self.joint_names(chain_id)
        if joints is None:
            return np.zeros(len(names), dtype=float)
        if isinstance(joints, dict):
            return np.array([float(joints.get(name, 0.0)) for name in names], dtype=float)
        if len(joints) != len(names):
            raise ValueError(f"expected {len(names)} joints for {chain_id}, got {len(joints)}")
        return np.array([float(v) for v in joints], dtype=float)

    def named_chain_joints(self, joints: JointValues | np.ndarray, chain_id: str) -> dict[str, float]:
        names = self.joint_names(chain_id)
        if isinstance(joints, dict):
            return {name: float(joints.get(name, 0.0)) for name in names}
        values = [float(v) for v in joints]
        if len(values) != len(names):
            raise ValueError(f"expected {len(names)} joints for {chain_id}, got {len(values)}")
        return dict(zip(names, values, strict=True))

    def initial_joints(self, chain_id: str) -> dict[str, float]:
        return self.named_chain_joints(self.config.initial_joints[chain_id], chain_id)

    def joint_limits(self, chain_id: str, joint_names: list[str] | None = None) -> tuple[np.ndarray, np.ndarray]:
        names = joint_names or self.joint_names(chain_id)
        lower = []
        upper = []
        for name in names:
            joint = self.joints[name]
            lower.append(joint.lower)
            upper.append(joint.upper)
        return np.array(lower, dtype=float), np.array(upper, dtype=float)

    def joint_limit_dicts(self, chain_id: str) -> list[dict[str, float | str]]:
        lower, upper = self.joint_limits(chain_id)
        return [
            {"name": name, "lower": float(lower[idx]), "upper": float(upper[idx])}
            for idx, name in enumerate(self.joint_names(chain_id))
        ]

    def forward_kinematics(self, joint_values: dict[str, float] | None = None) -> dict[str, np.ndarray]:
        values = joint_values or {}
        transforms: dict[str, np.ndarray] = {}

        def visit_link(link_name: str, parent_transform: np.ndarray) -> None:
            transforms[link_name] = parent_transform
            for joint in self.parent_to_joints.get(link_name, []):
                value = float(values.get(joint.name, 0.0))
                visit_link(joint.child, parent_transform @ self._joint_transform(joint, value))

        for root_link in self.root_links:
            visit_link(root_link, np.eye(4, dtype=float))
        return transforms

    def _joint_transform(self, joint: Joint, value: float) -> np.ndarray:
        origin = transform_from_xyz_rpy(joint.xyz, joint.rpy)
        if joint.joint_type in {"revolute", "continuous"}:
            motion = transform_from_rotation_translation(axis_angle_matrix(joint.axis, value), np.zeros(3))
        elif joint.joint_type == "prismatic":
            motion = translation_matrix(joint.axis * value)
        else:
            motion = np.eye(4, dtype=float)
        return origin @ motion

    def link_pose(self, joints: JointValues | np.ndarray, link_name: str, chain_id: str) -> Pose:
        transforms = self.forward_kinematics(self.named_chain_joints(joints, chain_id))
        if link_name not in transforms:
            raise ValueError(f"link {link_name!r} not found in FK result")
        return pose_from_matrix(transforms[link_name])

    def tcp_pose(self, joints: JointValues | np.ndarray, chain_id: str, tcp_offset: Pose | None = None) -> Pose:
        offset = tcp_offset or self.tcp_offset(chain_id)
        transforms = self.forward_kinematics(self.named_chain_joints(joints, chain_id))
        tcp_matrix = transforms[self.end_link(chain_id)] @ transform_from_pose(offset)
        return pose_from_matrix(tcp_matrix)

    def tcp_matrix(self, joints: JointValues | np.ndarray, chain_id: str, tcp_offset: Pose | None = None) -> np.ndarray:
        offset = tcp_offset or self.tcp_offset(chain_id)
        transforms = self.forward_kinematics(self.named_chain_joints(joints, chain_id))
        return transforms[self.end_link(chain_id)] @ transform_from_pose(offset)

    def link_poses(
        self,
        joints: JointValues | np.ndarray,
        chain_id: str,
        link_names: list[str] | None = None,
    ) -> dict[str, Pose]:
        transforms = self.forward_kinematics(self.named_chain_joints(joints, chain_id))
        names = link_names or self.display_links(chain_id)
        return {name: pose_from_matrix(transforms[name]) for name in names if name in transforms}

    def chain_links(self, chain_id: str) -> list[str]:
        links = [self.end_link(chain_id)]
        current = self.end_link(chain_id)
        while current in self.child_to_joint:
            joint = self.child_to_joint[current]
            current = joint.parent
            links.append(current)
            if current == self.base_link(chain_id):
                break
        return list(reversed(links))

    def display_links(self, chain_id: str) -> list[str]:
        configured = self.chain_config(chain_id).display_links
        return configured if configured else self.chain_links(chain_id)

    def mesh_base_url(self) -> str:
        rel = project_relative(self.config.mesh_root)
        return f"/{rel}/"

    def urdf_url(self) -> str:
        return f"/{project_relative(self.urdf_path)}"

    def metadata(self) -> dict[str, Any]:
        return {
            "robot": {
                "name": self.config.name,
                "display_name": self.config.display_name,
                "urdf_path": project_relative(self.urdf_path),
                "mesh_root": project_relative(self.config.mesh_root),
                "urdf_url": self.urdf_url(),
                "mesh_base_url": self.mesh_base_url(),
                "preview_links": list(self.config.preview_links),
            },
            "chains": {
                chain_id: {
                    "name": chain.name,
                    "display_name": chain.display_name,
                    "subtitle": chain.subtitle,
                    "panel_side": chain.panel_side,
                    "base_link": chain.base_link,
                    "end_link": chain.end_link,
                    "target_visual_link": chain.target_visual_link or chain.end_link,
                    "joint_names": self.joint_names(chain_id),
                    "display_links": self.display_links(chain_id),
                    "chain_links": self.chain_links(chain_id),
                    "joint_limits": self.joint_limit_dicts(chain_id),
                    "default_current_joints": self.initial_joints(chain_id),
                    "default_tcp_offset": self.tcp_offset(chain_id).to_dict(),
                }
                for chain_id, chain in self.config.chains.items()
            },
            "links": sorted(self.links),
            "joints": sorted(self.joints),
            "root_links": self.root_links,
            "viewer_frames": list(self.config.viewer_frames),
        }


def pose_to_dict(pose: Pose) -> dict[str, list[float]]:
    return pose.to_dict()
