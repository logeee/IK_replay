from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .robot_model import RobotModel
from .types import JointValues, Pose
from .utils import rpy_matrix


@dataclass(frozen=True)
class CollisionPrimitive:
    name: str
    role: str
    kind: str
    data: dict[str, Any]


def _as_array(values: list[float] | np.ndarray) -> np.ndarray:
    return np.array(values, dtype=float)


def _lerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    return a + (b - a) * float(t)


def _point_segment_distance(point: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    ab = b - a
    denom = float(np.dot(ab, ab))
    if denom < 1e-12:
        return float(np.linalg.norm(point - a))
    t = float(np.clip(np.dot(point - a, ab) / denom, 0.0, 1.0))
    return float(np.linalg.norm(point - (a + ab * t)))


def _point_obb_distance(point: np.ndarray, center: np.ndarray, rotation: np.ndarray, half_extents: np.ndarray) -> float:
    local = rotation.T @ (point - center)
    outside = np.maximum(np.abs(local) - half_extents, 0.0)
    return float(np.linalg.norm(outside))


def _capsule_sphere_distance(
    a: np.ndarray,
    b: np.ndarray,
    capsule_radius: float,
    center: np.ndarray,
    sphere_radius: float,
) -> float:
    return _point_segment_distance(center, a, b) - capsule_radius - sphere_radius


def _sphere_obb_distance(
    center: np.ndarray,
    radius: float,
    box_center: np.ndarray,
    rotation: np.ndarray,
    half_extents: np.ndarray,
) -> float:
    return _point_obb_distance(center, box_center, rotation, half_extents) - radius


def _capsule_obb_distance(
    a: np.ndarray,
    b: np.ndarray,
    radius: float,
    box_center: np.ndarray,
    rotation: np.ndarray,
    half_extents: np.ndarray,
    samples: int = 12,
) -> float:
    distances = [
        _point_obb_distance(_lerp(a, b, idx / (samples - 1)), box_center, rotation, half_extents) - radius
        for idx in range(samples)
    ]
    return float(min(distances))


class ConfigurableCollisionChecker:
    """Approximate collision checker built from robot YAML primitives."""

    def __init__(self, robot_model: RobotModel):
        self.robot_model = robot_model
        self.config = dict(robot_model.config.collision or {})
        self.enabled = bool(self.config.get("enabled", self.config))
        self.near_margin_m = float(self.config.get("near_margin_m", 0.05))

    def metadata(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "near_margin_m": self.near_margin_m,
            "body_shape_count": len(self.config.get("body") or []),
            "chains": {
                chain_id: {
                    "shape_count": len((self.config.get("chains") or {}).get(chain_id, {}).get("shapes") or []),
                }
                for chain_id in self.robot_model.chain_ids
            },
        }

    def check_state(
        self,
        joints: JointValues,
        chain_id: str,
        tcp_offset: Pose | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return self._unconfigured_result(chain_id)

        self.robot_model.chain_config(chain_id)
        transforms = self.robot_model.forward_kinematics(self.robot_model.named_chain_joints(joints, chain_id))
        tcp_pose = self.robot_model.tcp_pose(joints, chain_id, tcp_offset)
        shapes = self._build_shapes(transforms, chain_id, tcp_pose)
        distances = self._pair_distances(shapes)
        min_pair = min(distances, key=lambda item: item["distance_m"]) if distances else None
        min_distance = float(min_pair["distance_m"]) if min_pair else None
        status = (
            "unconfigured"
            if min_distance is None
            else "collision"
            if min_distance <= 0.0
            else "near"
            if min_distance <= self.near_margin_m
            else "safe"
        )
        return {
            "enabled": True,
            "chain_id": chain_id,
            "status": status,
            "status_label": _status_label(status),
            "near_margin_m": self.near_margin_m,
            "min_distance_m": min_distance,
            "min_distance_mm": min_distance * 1000.0 if min_distance is not None else None,
            "pair": min_pair,
            "pairs": distances,
            "shapes": {shape.name: shape.data | {"kind": shape.kind, "role": shape.role} for shape in shapes},
        }

    def check_trajectory(
        self,
        waypoints: list[Any],
        chain_id: str,
        tcp_offset: Pose | None = None,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for idx, waypoint in enumerate(waypoints):
            joints = _waypoint_joints(waypoint)
            check = self.check_state(joints, chain_id, tcp_offset)
            checks.append(
                {
                    "index": _waypoint_value(waypoint, "index", idx),
                    "t": _waypoint_value(waypoint, "t", 0.0),
                    "status": check["status"],
                    "status_label": check["status_label"],
                    "min_distance_m": check["min_distance_m"],
                    "min_distance_mm": check["min_distance_mm"],
                    "pair": check["pair"],
                    "shapes": check["shapes"],
                }
            )
        return checks

    def summarize_checks(self, checks: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "status": "unconfigured",
                "status_label": _status_label("unconfigured"),
                "collision_count": 0,
                "near_count": 0,
                "min_distance_m": None,
                "min_distance_mm": None,
                "pair": None,
                "checks": checks,
            }
        status = _overall_status(checks)
        measurable_checks = [item for item in checks if item["min_distance_m"] is not None]
        min_check = min(measurable_checks, key=lambda item: item["min_distance_m"]) if measurable_checks else None
        min_distance = float(min_check["min_distance_m"]) if min_check else None
        return {
            "enabled": True,
            "status": status,
            "status_label": _status_label(status),
            "collision_count": sum(1 for item in checks if item["status"] == "collision"),
            "near_count": sum(1 for item in checks if item["status"] == "near"),
            "min_distance_m": min_distance,
            "min_distance_mm": min_distance * 1000.0 if min_distance is not None else None,
            "pair": min_check["pair"] if min_check else None,
            "checks": checks,
        }

    def _build_shapes(
        self,
        transforms: dict[str, np.ndarray],
        chain_id: str,
        tcp_pose: Pose,
    ) -> list[CollisionPrimitive]:
        shapes: list[CollisionPrimitive] = []
        for item in self.config.get("body") or []:
            shapes.append(self._build_shape(item, "body", transforms, tcp_pose))
        chain_config = (self.config.get("chains") or {}).get(chain_id) or {}
        for item in chain_config.get("shapes") or []:
            shapes.append(self._build_shape(item, "chain", transforms, tcp_pose, chain_id))
        return shapes

    def _build_shape(
        self,
        raw: dict[str, Any],
        role: str,
        transforms: dict[str, np.ndarray],
        tcp_pose: Pose,
        chain_id: str | None = None,
    ) -> CollisionPrimitive:
        kind = str(raw.get("kind") or "")
        name = str(raw.get("name") or f"{role}_{kind}")
        if role == "chain" and chain_id:
            name = f"{chain_id}_{name}"

        if kind == "sphere":
            center = self._point(raw, transforms, tcp_pose)
            return CollisionPrimitive(
                name=name,
                role=role,
                kind=kind,
                data={
                    "center": _to_list(center),
                    "radius": float(raw["radius"]),
                },
            )
        if kind == "box":
            transform = self._link_transform(raw, transforms)
            center = self._local_point(transform, raw.get("xyz"))
            rotation = transform[:3, :3] @ rpy_matrix([float(v) for v in raw.get("rpy", [0.0, 0.0, 0.0])])
            return CollisionPrimitive(
                name=name,
                role=role,
                kind=kind,
                data={
                    "center": _to_list(center),
                    "half_extents": [float(v) for v in raw["half_extents"]],
                    "rotation": rotation.tolist(),
                },
            )
        if kind == "capsule":
            a = self._endpoint(raw["a"], transforms, tcp_pose)
            b = self._endpoint(raw["b"], transforms, tcp_pose)
            return CollisionPrimitive(
                name=name,
                role=role,
                kind=kind,
                data={
                    "a": _to_list(a),
                    "b": _to_list(b),
                    "radius": float(raw["radius"]),
                },
            )
        raise ValueError(f"unsupported collision shape kind: {kind!r}")

    def _point(self, raw: dict[str, Any], transforms: dict[str, np.ndarray], tcp_pose: Pose) -> np.ndarray:
        if raw.get("tcp"):
            return np.array(tcp_pose.xyz, dtype=float) + np.array(raw.get("xyz", [0.0, 0.0, 0.0]), dtype=float)
        return self._endpoint(raw, transforms, tcp_pose)

    def _endpoint(self, raw: dict[str, Any], transforms: dict[str, np.ndarray], tcp_pose: Pose) -> np.ndarray:
        if raw.get("tcp"):
            base = np.array(tcp_pose.xyz, dtype=float) + np.array(raw.get("xyz", [0.0, 0.0, 0.0]), dtype=float)
        else:
            transform = self._link_transform(raw, transforms)
            base = self._local_point(transform, raw.get("xyz"))
        toward = raw.get("toward")
        if toward:
            target_transform = self._link_transform({"link": toward}, transforms)
            target = self._local_point(target_transform, raw.get("toward_xyz"))
            return _lerp(base, target, float(raw.get("t", 0.0)))
        return base

    def _link_transform(self, raw: dict[str, Any], transforms: dict[str, np.ndarray]) -> np.ndarray:
        link = str(raw.get("link") or "")
        if not link:
            raise ValueError("collision shape endpoint must define link or tcp: true")
        if link not in transforms:
            raise ValueError(f"collision shape references unknown link {link!r}")
        return transforms[link]

    @staticmethod
    def _local_point(transform: np.ndarray, xyz: list[float] | None = None) -> np.ndarray:
        local = np.array([*(xyz or [0.0, 0.0, 0.0]), 1.0], dtype=float)
        return (transform @ local)[:3]

    def _pair_distances(self, shapes: list[CollisionPrimitive]) -> list[dict[str, Any]]:
        body = [shape for shape in shapes if shape.role == "body"]
        chain = [shape for shape in shapes if shape.role == "chain"]
        distances: list[dict[str, Any]] = []
        for left in chain:
            for right in body:
                distance = self._distance_between(left, right)
                distances.append(
                    {
                        "a": left.name,
                        "b": right.name,
                        "distance_m": float(distance),
                        "distance_mm": float(distance * 1000.0),
                    }
                )
        distances.sort(key=lambda item: item["distance_m"])
        return distances

    def _distance_between(self, a: CollisionPrimitive, b: CollisionPrimitive) -> float:
        if a.kind == "capsule" and b.kind == "sphere":
            return _capsule_sphere_distance(
                _as_array(a.data["a"]),
                _as_array(a.data["b"]),
                float(a.data["radius"]),
                _as_array(b.data["center"]),
                float(b.data["radius"]),
            )
        if a.kind == "sphere" and b.kind == "capsule":
            return self._distance_between(b, a)
        if a.kind == "sphere" and b.kind == "box":
            return _sphere_obb_distance(
                _as_array(a.data["center"]),
                float(a.data["radius"]),
                _as_array(b.data["center"]),
                np.array(b.data["rotation"], dtype=float),
                _as_array(b.data["half_extents"]),
            )
        if a.kind == "sphere" and b.kind == "sphere":
            center_distance = float(np.linalg.norm(_as_array(a.data["center"]) - _as_array(b.data["center"])))
            return center_distance - float(a.data["radius"]) - float(b.data["radius"])
        if a.kind == "capsule" and b.kind == "box":
            return _capsule_obb_distance(
                _as_array(a.data["a"]),
                _as_array(a.data["b"]),
                float(a.data["radius"]),
                _as_array(b.data["center"]),
                np.array(b.data["rotation"], dtype=float),
                _as_array(b.data["half_extents"]),
            )
        if a.kind == "box" and b.kind in {"sphere", "capsule"}:
            return self._distance_between(b, a)
        raise ValueError(f"unsupported primitive pair: {a.kind}, {b.kind}")

    def _unconfigured_result(self, chain_id: str) -> dict[str, Any]:
        return {
            "enabled": False,
            "chain_id": chain_id,
            "status": "unconfigured",
            "status_label": _status_label("unconfigured"),
            "near_margin_m": self.near_margin_m,
            "min_distance_m": None,
            "min_distance_mm": None,
            "pair": None,
            "pairs": [],
            "shapes": {},
        }


def _status_label(status: str) -> str:
    return {
        "safe": "安全",
        "near": "接近",
        "collision": "碰撞",
        "unconfigured": "未配置",
    }.get(status, status)


def _overall_status(checks: list[dict[str, Any]]) -> str:
    statuses = {item["status"] for item in checks}
    if "collision" in statuses:
        return "collision"
    if "near" in statuses:
        return "near"
    if "safe" in statuses:
        return "safe"
    return "unconfigured"


def _to_list(values: np.ndarray) -> list[float]:
    return [float(v) for v in values]


def _waypoint_joints(waypoint: Any) -> JointValues:
    if isinstance(waypoint, dict):
        return waypoint.get("named_joints") or waypoint["joints"]
    return getattr(waypoint, "named_joints", None) or getattr(waypoint, "joints")


def _waypoint_value(waypoint: Any, key: str, fallback: Any) -> Any:
    if isinstance(waypoint, dict):
        return waypoint.get(key, fallback)
    return getattr(waypoint, key, fallback)
