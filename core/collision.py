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
        # 可选的环境障碍点云（URDF 根坐标系，运行时由外部注入，如深度相机扫描）
        self.environment_points: np.ndarray | None = None
        self.environment_radius: float = 0.03
        # 可选的环境平面：柜面等平整障碍直接用拟合平面表示，零膨胀、距离
        # 解析精确。可带边界（矩形）——柜面是有限宽的，无限半空间会把柜子
        # 侧面以外的空区域也拦掉。每项为 dict，见 set_environment_planes。
        self.environment_planes: list[dict[str, Any]] = []
        # 豁免球列表 [(center, radius)]：这些区域内的环境点不参与检查（如抓取目标附近）
        self.environment_exclusions: list[tuple[np.ndarray, float]] = []

    def set_environment(self, points: np.ndarray | list, radius: float) -> None:
        arr = np.asarray(points, dtype=float).reshape(-1, 3)
        self.environment_points = arr if len(arr) else None
        self.environment_radius = float(radius)

    def set_environment_planes(self, planes: list[dict]) -> None:
        """每项: {"point", "normal"} 必填；可选 {"dir", "u_range", "v_range"}
        给平面加矩形边界——u 沿 dir、v 沿 normal×dir（都相对 point 计），
        超出边界的区域不算障碍。不带边界即无限半空间。normal 指向自由侧。
        """
        self.environment_planes = []
        for raw in planes:
            n = np.asarray(raw["normal"], dtype=float).reshape(3)
            n = n / (np.linalg.norm(n) or 1.0)
            item: dict[str, Any] = {
                "point": np.asarray(raw["point"], dtype=float).reshape(3),
                "normal": n,
            }
            if raw.get("dir") is not None and raw.get("u_range") is not None:
                u = np.asarray(raw["dir"], dtype=float).reshape(3)
                u = u / (np.linalg.norm(u) or 1.0)
                v = np.cross(n, u)
                v = v / (np.linalg.norm(v) or 1.0)
                item["dir"] = u
                item["v_axis"] = v
                item["u_range"] = (float(raw["u_range"][0]), float(raw["u_range"][1]))
                vr = raw.get("v_range")
                item["v_range"] = (float(vr[0]), float(vr[1])) if vr is not None \
                    else (-1e9, 1e9)
            self.environment_planes.append(item)

    def clear_environment(self) -> None:
        self.environment_points = None
        self.environment_planes = []
        self.environment_exclusions = []

    def set_environment_exclusions(self, spheres: list[tuple[list | np.ndarray, float]]) -> None:
        self.environment_exclusions = [
            (np.asarray(center, dtype=float).reshape(3), float(radius))
            for center, radius in spheres
        ]

    def _active_environment_points(self) -> np.ndarray | None:
        points = self.environment_points
        if points is None:
            return None
        mask = np.ones(len(points), dtype=bool)
        for center, radius in self.environment_exclusions:
            mask &= np.linalg.norm(points - center, axis=1) > radius
        active = points[mask]
        return active if len(active) else None

    def metadata(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "near_margin_m": self.near_margin_m,
            "environment_point_count": 0 if self.environment_points is None else int(len(self.environment_points)),
            "environment_plane_count": len(self.environment_planes),
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
            "shapes": {shape.name: _serializable_shape(shape) for shape in shapes},
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
        env = self._active_environment_points()
        if env is not None:
            shapes.append(CollisionPrimitive(
                name="environment",
                role="body",
                kind="cloud",
                data={"points": env, "radius": self.environment_radius, "count": int(len(env))},
            ))
        for i, plane in enumerate(self.environment_planes):
            name = "environment_wall" if len(self.environment_planes) == 1 \
                else f"environment_wall_{i}"
            data = {k: (_to_list(v) if isinstance(v, np.ndarray) else v)
                    for k, v in plane.items()}
            shapes.append(CollisionPrimitive(
                name=name,
                role="body",
                kind="plane",
                data=data,
            ))
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
        if raw.get("tcp_foot"):
            # TCP 点向平面（link 原点 + link 系 axis 为法线）做垂线的垂足
            spec = raw["tcp_foot"]
            transform = self._link_transform(spec, transforms)
            origin = transform[:3, 3]
            axis = np.array(spec.get("axis", [1.0, 0.0, 0.0]), dtype=float)
            normal = transform[:3, :3] @ (axis / np.linalg.norm(axis))
            tcp = np.array(tcp_pose.xyz, dtype=float)
            return tcp - float(np.dot(tcp - origin, normal)) * normal
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
        if b.kind == "cloud":
            return self._distance_to_cloud(a, b)
        if a.kind == "cloud":
            return self._distance_to_cloud(b, a)
        if b.kind == "plane":
            return self._distance_to_plane(a, b)
        if a.kind == "plane":
            return self._distance_to_plane(b, a)
        raise ValueError(f"unsupported primitive pair: {a.kind}, {b.kind}")

    def _distance_to_plane(self, shape: CollisionPrimitive, plane: CollisionPrimitive) -> float:
        """chain 几何体到环境平面的有符号距离（法线指向自由侧，负=穿墙）。

        解析精确、零膨胀。平面可带矩形边界（柜面是有限大的）：边界内按
        垂距，边界外按到矩形最近点的欧氏距离（恒正——绕过柜边是允许的）。
        豁免球同样生效——形状最近点在墙面上的垂足落在任一豁免球内
        （如抓取目标附近）时，该平面不参与检查。
        """
        p0 = _as_array(plane.data["point"])
        n = _as_array(plane.data["normal"])
        udir = plane.data.get("dir")
        u = _as_array(udir) if udir is not None else None
        vax = _as_array(plane.data["v_axis"]) if u is not None else None
        ur = plane.data.get("u_range")
        vr = plane.data.get("v_range")

        def point_distance(p: np.ndarray) -> tuple[float, np.ndarray]:
            w = float(np.dot(n, p - p0))
            if u is None:
                return w, p - w * n
            uu = float(np.dot(u, p - p0))
            vv = float(np.dot(vax, p - p0))
            uc = min(max(uu, ur[0]), ur[1])
            vc = min(max(vv, vr[0]), vr[1])
            if uc == uu and vc == vv:
                return w, p - w * n
            foot = p0 + uc * u + vc * vax
            return float(np.linalg.norm(p - foot)), foot

        if shape.kind == "sphere":
            d, foot = point_distance(_as_array(shape.data["center"]))
        elif shape.kind == "capsule":
            a = _as_array(shape.data["a"])
            b = _as_array(shape.data["b"])
            d, foot = min((point_distance(_lerp(a, b, i / 11)) for i in range(12)),
                          key=lambda item: item[0])
        else:
            raise ValueError(f"unsupported primitive vs plane: {shape.kind!r}")
        for center, radius in self.environment_exclusions:
            if float(np.linalg.norm(foot - center)) <= radius:
                return 9.99   # 豁免区内：按远距离处理（有限值，便于 JSON 序列化）
        return d - float(shape.data["radius"])

    @staticmethod
    def _distance_to_cloud(shape: CollisionPrimitive, cloud: CollisionPrimitive) -> float:
        """chain 几何体到环境点云的最小距离（向量化，点数几千也很快）。"""
        points = cloud.data["points"]
        cloud_radius = float(cloud.data["radius"])
        if shape.kind == "sphere":
            distances = np.linalg.norm(points - _as_array(shape.data["center"]), axis=1)
            return float(distances.min()) - float(shape.data["radius"]) - cloud_radius
        if shape.kind == "capsule":
            a = _as_array(shape.data["a"])
            b = _as_array(shape.data["b"])
            ab = b - a
            denom = float(np.dot(ab, ab))
            if denom < 1e-12:
                distances = np.linalg.norm(points - a, axis=1)
            else:
                t = np.clip((points - a) @ ab / denom, 0.0, 1.0)
                distances = np.linalg.norm(points - (a + t[:, None] * ab), axis=1)
            return float(distances.min()) - float(shape.data["radius"]) - cloud_radius
        raise ValueError(f"unsupported primitive vs cloud: {shape.kind!r}")

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


def _serializable_shape(shape: CollisionPrimitive) -> dict[str, Any]:
    """点云不逐点序列化（每个 waypoint 都带一份会撑爆响应），只留摘要。"""
    if shape.kind == "cloud":
        return {
            "kind": shape.kind,
            "role": shape.role,
            "radius": shape.data["radius"],
            "count": shape.data["count"],
        }
    return shape.data | {"kind": shape.kind, "role": shape.role}


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
