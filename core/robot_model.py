from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .config import ARM_END_LINKS, ARM_JOINTS, ARM_LINKS, DEFAULT_TCP_OFFSET, validate_arm


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


def _parse_vec(value: str | None, default: tuple[float, float, float]) -> np.ndarray:
    if not value:
        return np.array(default, dtype=float)
    parts = [float(part) for part in value.replace(",", " ").split()]
    if len(parts) != 3:
        raise ValueError(f"expected 3-vector, got {value!r}")
    return np.array(parts, dtype=float)


def rpy_matrix(rpy: np.ndarray) -> np.ndarray:
    roll, pitch, yaw = [float(v) for v in rpy]
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]], dtype=float)
    ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=float)
    rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=float)
    return rz @ ry @ rx


def axis_angle_matrix(axis: np.ndarray, angle: float) -> np.ndarray:
    axis = np.array(axis, dtype=float)
    norm = np.linalg.norm(axis)
    if norm < 1e-12:
        return np.eye(3)
    x, y, z = axis / norm
    c = math.cos(angle)
    s = math.sin(angle)
    c1 = 1.0 - c
    return np.array(
        [
            [c + x * x * c1, x * y * c1 - z * s, x * z * c1 + y * s],
            [y * x * c1 + z * s, c + y * y * c1, y * z * c1 - x * s],
            [z * x * c1 - y * s, z * y * c1 + x * s, c + z * z * c1],
        ],
        dtype=float,
    )


def transform_from_xyz_rpy(xyz: np.ndarray, rpy: np.ndarray) -> np.ndarray:
    matrix = np.eye(4, dtype=float)
    matrix[:3, :3] = rpy_matrix(rpy)
    matrix[:3, 3] = xyz
    return matrix


def transform_from_rotation_translation(rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    matrix = np.eye(4, dtype=float)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = translation
    return matrix


def translation_matrix(xyz: np.ndarray) -> np.ndarray:
    matrix = np.eye(4, dtype=float)
    matrix[:3, 3] = xyz
    return matrix


class RobotModel:
    """Small URDF loader and FK engine for offline demo use."""

    def __init__(self, urdf_path: str | Path):
        self.urdf_path = Path(urdf_path)
        if not self.urdf_path.exists():
            raise FileNotFoundError(f"URDF not found: {self.urdf_path}")
        self.links: dict[str, Link] = {}
        self.joints: dict[str, Joint] = {}
        self.parent_to_joints: dict[str, list[Joint]] = {}
        self.child_to_joint: dict[str, Joint] = {}
        self.root_links: list[str] = []
        self._load_urdf()

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
                scale = _parse_vec(mesh_el.attrib.get("scale") if mesh_el is not None else None, (1, 1, 1))
                material_el = visual_el.find("material")
                color_el = material_el.find("color") if material_el is not None else None
                color = None
                if color_el is not None and color_el.attrib.get("rgba"):
                    color = [float(part) for part in color_el.attrib["rgba"].split()]
                visuals.append(
                    Visual(
                        filename=filename,
                        xyz=_parse_vec(origin_el.attrib.get("xyz") if origin_el is not None else None, (0, 0, 0)),
                        rpy=_parse_vec(origin_el.attrib.get("rpy") if origin_el is not None else None, (0, 0, 0)),
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
                xyz=_parse_vec(origin_el.attrib.get("xyz") if origin_el is not None else None, (0, 0, 0)),
                rpy=_parse_vec(origin_el.attrib.get("rpy") if origin_el is not None else None, (0, 0, 0)),
                axis=_parse_vec(axis_el.attrib.get("xyz") if axis_el is not None else None, (0, 0, 1)),
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

    def arm_joint_names(self, arm: str) -> list[str]:
        return list(ARM_JOINTS[validate_arm(arm)])

    def arm_end_link(self, arm: str) -> str:
        return ARM_END_LINKS[validate_arm(arm)]

    def joint_limits(self, joint_names: list[str]) -> tuple[np.ndarray, np.ndarray]:
        lower = []
        upper = []
        for name in joint_names:
            joint = self.joints[name]
            lower.append(joint.lower)
            upper.append(joint.upper)
        return np.array(lower, dtype=float), np.array(upper, dtype=float)

    def coerce_arm_joints(self, joints: list[float] | dict[str, float] | None, arm: str) -> np.ndarray:
        names = self.arm_joint_names(arm)
        if joints is None:
            return np.zeros(len(names), dtype=float)
        if isinstance(joints, dict):
            return np.array([float(joints.get(name, 0.0)) for name in names], dtype=float)
        if len(joints) != len(names):
            raise ValueError(f"expected {len(names)} joints for {arm} arm, got {len(joints)}")
        return np.array([float(v) for v in joints], dtype=float)

    def named_arm_joints(self, joints: list[float] | np.ndarray | dict[str, float], arm: str) -> dict[str, float]:
        names = self.arm_joint_names(arm)
        if isinstance(joints, dict):
            return {name: float(joints.get(name, 0.0)) for name in names}
        values = [float(v) for v in joints]
        if len(values) != len(names):
            raise ValueError(f"expected {len(names)} joints for {arm} arm, got {len(values)}")
        return dict(zip(names, values, strict=True))

    def merged_joint_values(self, arm_joints: list[float] | np.ndarray | dict[str, float], arm: str) -> dict[str, float]:
        return self.named_arm_joints(arm_joints, arm)

    def forward_kinematics(self, joint_values: dict[str, float] | None = None) -> dict[str, np.ndarray]:
        values = joint_values or {}
        transforms: dict[str, np.ndarray] = {}

        def visit_link(link_name: str, parent_transform: np.ndarray) -> None:
            transforms[link_name] = parent_transform
            for joint in self.parent_to_joints.get(link_name, []):
                visit_link(joint.child, parent_transform @ self._joint_transform(joint, values.get(joint.name, 0.0)))

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

    def tcp_pose(
        self,
        arm_joints: list[float] | np.ndarray | dict[str, float],
        arm: str,
        tcp_offset: list[float] | np.ndarray | None = None,
    ) -> np.ndarray:
        arm = validate_arm(arm)
        offset = np.array(tcp_offset if tcp_offset is not None else DEFAULT_TCP_OFFSET, dtype=float)
        transforms = self.forward_kinematics(self.merged_joint_values(arm_joints, arm))
        end_link = self.arm_end_link(arm)
        return transforms[end_link] @ translation_matrix(offset)

    def tcp_position(
        self,
        arm_joints: list[float] | np.ndarray | dict[str, float],
        arm: str,
        tcp_offset: list[float] | np.ndarray | None = None,
    ) -> np.ndarray:
        return self.tcp_pose(arm_joints, arm, tcp_offset)[:3, 3]

    def link_positions(
        self,
        arm_joints: list[float] | np.ndarray | dict[str, float],
        arm: str,
        link_names: list[str] | None = None,
    ) -> dict[str, list[float]]:
        arm = validate_arm(arm)
        transforms = self.forward_kinematics(self.merged_joint_values(arm_joints, arm))
        names = link_names or ARM_LINKS[arm]
        return {
            name: [float(v) for v in transforms[name][:3, 3]]
            for name in names
            if name in transforms
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "urdf_url": "/assets/g1_d_description/g1_d.urdf",
            "mesh_base_url": "/assets/g1_d_description/",
            "root_links": self.root_links,
            "arms": {
                arm: {
                    "joint_names": names,
                    "end_link": ARM_END_LINKS[arm],
                    "limits": [
                        {"name": name, "lower": self.joints[name].lower, "upper": self.joints[name].upper}
                        for name in names
                    ],
                }
                for arm, names in ARM_JOINTS.items()
            },
        }

