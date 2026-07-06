from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .config import COLLISION_NEAR_MARGIN_M, validate_arm
from .robot_model import RobotModel


@dataclass(frozen=True)
class CollisionPrimitive:
    name: str
    kind: str
    data: dict


def _as_array(values: list[float] | np.ndarray) -> np.ndarray:
    return np.array(values, dtype=float)


def _lerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    return a + (b - a) * t


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


def _capsule_sphere_distance(a: np.ndarray, b: np.ndarray, capsule_radius: float, center: np.ndarray, sphere_radius: float) -> float:
    return _point_segment_distance(center, a, b) - capsule_radius - sphere_radius


def _sphere_obb_distance(center: np.ndarray, radius: float, box_center: np.ndarray, rotation: np.ndarray, half_extents: np.ndarray) -> float:
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


class SimpleCollisionChecker:
    """Simplified collision model for visualization, not safety certification."""

    def __init__(self, robot_model: RobotModel, near_margin_m: float = COLLISION_NEAR_MARGIN_M):
        self.robot_model = robot_model
        self.near_margin_m = float(near_margin_m)

    def check_state(
        self,
        joints: list[float] | dict[str, float],
        arm: str = "left",
        tcp_offset: list[float] | None = None,
    ) -> dict:
        arm = validate_arm(arm)
        shapes = self._build_shapes(joints, arm, tcp_offset)
        signed_distances = self._pair_distances(shapes)
        min_pair = min(signed_distances, key=lambda item: item["distance_m"]) if signed_distances else None
        min_distance = float(min_pair["distance_m"]) if min_pair else math.inf
        status = "collision" if min_distance <= 0.0 else "near" if min_distance <= self.near_margin_m else "safe"
        return {
            "status": status,
            "min_distance_m": min_distance,
            "min_distance_mm": min_distance * 1000.0,
            "pair": min_pair,
            "pairs": signed_distances,
            "shapes": {shape.name: shape.data | {"kind": shape.kind} for shape in shapes},
        }

    def check_trajectory(
        self,
        waypoints: list[dict],
        arm: str = "left",
        tcp_offset: list[float] | None = None,
    ) -> list[dict]:
        checks = []
        for waypoint in waypoints:
            check = self.check_state(waypoint["joints"], arm, tcp_offset)
            checks.append(
                {
                    "index": waypoint["index"],
                    "timestamp": waypoint["timestamp"],
                    "status": check["status"],
                    "min_distance_m": check["min_distance_m"],
                    "min_distance_mm": check["min_distance_mm"],
                    "pair": check["pair"],
                    "shapes": check["shapes"],
                }
            )
        return checks

    def _build_shapes(
        self,
        joints: list[float] | dict[str, float],
        arm: str,
        tcp_offset: list[float] | None,
    ) -> list[CollisionPrimitive]:
        transforms = self.robot_model.forward_kinematics(self.robot_model.merged_joint_values(joints, arm))
        torso_transform = transforms["torso_link"]
        torso_rotation = torso_transform[:3, :3]
        torso_center = torso_transform @ np.array([0.02, 0.0, 0.20, 1.0], dtype=float)
        head_center = torso_transform @ np.array([0.03, 0.0, 0.48, 1.0], dtype=float)

        shoulder = transforms[f"{arm}_shoulder_yaw_link"][:3, 3]
        elbow = transforms[f"{arm}_elbow_link"][:3, 3]
        wrist = transforms[f"{arm}_wrist_yaw_link"][:3, 3]
        palm = transforms[f"{arm}_hand_palm_link"][:3, 3]
        tcp = self.robot_model.tcp_position(joints, arm, tcp_offset)

        upper_start = _lerp(shoulder, elbow, 0.22)
        forearm_end = _lerp(wrist, palm, 0.85)

        return [
            CollisionPrimitive(
                "torso_box",
                "box",
                {
                    "center": [float(v) for v in torso_center[:3]],
                    "half_extents": [0.13, 0.085, 0.23],
                    "rotation": torso_rotation.tolist(),
                },
            ),
            CollisionPrimitive(
                "head_sphere",
                "sphere",
                {
                    "center": [float(v) for v in head_center[:3]],
                    "radius": 0.13,
                },
            ),
            CollisionPrimitive(
                f"{arm}_upper_arm",
                "capsule",
                {
                    "a": [float(v) for v in upper_start],
                    "b": [float(v) for v in elbow],
                    "radius": 0.045,
                },
            ),
            CollisionPrimitive(
                f"{arm}_forearm",
                "capsule",
                {
                    "a": [float(v) for v in elbow],
                    "b": [float(v) for v in forearm_end],
                    "radius": 0.04,
                },
            ),
            CollisionPrimitive(
                f"{arm}_hand_tcp",
                "sphere",
                {
                    "center": [float(v) for v in tcp],
                    "radius": 0.05,
                },
            ),
        ]

    def _pair_distances(self, shapes: list[CollisionPrimitive]) -> list[dict]:
        by_name = {shape.name: shape for shape in shapes}
        torso = by_name["torso_box"]
        head = by_name["head_sphere"]
        checked_pairs = [
            (by_name[name], torso)
            for name in by_name
            if name.endswith("_forearm") or name.endswith("_hand_tcp") or name.endswith("_upper_arm")
        ] + [
            (by_name[name], head)
            for name in by_name
            if name.endswith("_forearm") or name.endswith("_hand_tcp") or name.endswith("_upper_arm")
        ]

        distances = []
        for left, right in checked_pairs:
            if left.name == right.name:
                continue
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
