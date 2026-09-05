"""H2 浮动基座估计：由 IMU 姿态 + 支撑腿运动学得到骨盆/躯干在世界系的位姿。

来源：同事 arm-motion-middleware v1.0.3 的 ``hardware/h2/floating_base_state.py``
（commit f276d08）。估计算法逐行保留；改动仅限于：

* 输入从其 ``H2HardwareState``/``MappedJointState`` 换成本模块自带的
  :class:`FloatingBaseInput`（全身关节名→角度字典 + IMU 四元数/角速度）；
* 增加 ``world_T_root``（默认 ``torso_link``）输出，手臂控制直接使用；
* 增加 :meth:`SupportKinematicFloatingBase.anchor` 显式（重）锚定接口——
  取点/执行前机器人站定时调用，走动后必须重新锚定。

原理：rt/lowstate 没有里程计和触地信息，第一版把锚定时刻的双脚位置固定为
世界系锚点，仅在双脚站定时有效；不积分 IMU 加速度。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import numpy as np
import pinocchio as pin
import yaml


def quaternion_wxyz_to_rotation(quaternion_wxyz: np.ndarray) -> np.ndarray:
    quaternion = np.asarray(quaternion_wxyz, dtype=np.float64).reshape(4)
    quaternion = quaternion / np.linalg.norm(quaternion)
    w, x, y, z = quaternion
    return np.asarray(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def rotation_to_quaternion_wxyz(rotation: np.ndarray) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    quaternion_xyzw = pin.Quaternion(matrix).coeffs()
    result = np.asarray(
        [quaternion_xyzw[3], quaternion_xyzw[0], quaternion_xyzw[1], quaternion_xyzw[2]],
        dtype=np.float64,
    )
    if result[0] < 0.0:
        result = -result
    return result / np.linalg.norm(result)


def _rotation_z(angle_rad: float) -> np.ndarray:
    cosine = float(np.cos(angle_rad))
    sine = float(np.sin(angle_rad))
    return np.asarray(
        [[cosine, -sine, 0.0], [sine, cosine, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def _rotation_distance(first: np.ndarray, second: np.ndarray) -> float:
    cosine = float(np.clip((np.trace(first.T @ second) - 1.0) * 0.5, -1.0, 1.0))
    return float(np.arccos(cosine))


@dataclass(frozen=True)
class FloatingBaseConfig:
    source: str
    orientation_source: str
    quaternion_order: str
    imu_convention: str
    pelvis_R_imu: np.ndarray
    yaw_alignment: str
    support_mode: str
    initial_pelvis_position_world_m: np.ndarray
    left_foot_frame: str
    right_foot_frame: str
    root_frame: str
    full_body_urdf: Path
    anchor_warning_m: float
    walking_translation_supported: bool
    # rt/lowstate motor_state 序号 -> (URDF 关节名, sign, offset_rad)
    motor_index_to_joint: dict[int, tuple[str, float, float]]

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        *,
        project_root: str | Path,
    ) -> "FloatingBaseConfig":
        config_path = Path(path).resolve()
        document: Mapping[str, object] = yaml.safe_load(
            config_path.read_text(encoding="utf-8")
        )
        values = document["floating_base"]
        root = Path(project_root).resolve()
        urdf = Path(str(values["full_body_urdf"]))
        if not urdf.is_absolute():
            urdf = root / urdf
        quaternion = np.asarray(
            values["pelvis_R_imu_quaternion_wxyz"], dtype=np.float64
        )
        mapping_raw = document.get("motor_index_to_joint") or {}
        mapping: dict[int, tuple[str, float, float]] = {}
        for index, entry in mapping_raw.items():
            mapping[int(index)] = (
                str(entry["joint"]),
                float(entry.get("sign", 1.0)),
                float(entry.get("offset_rad", 0.0)),
            )
        config = cls(
            source=str(values["source"]),
            orientation_source=str(values["orientation_source"]),
            quaternion_order=str(values["quaternion_order"]),
            imu_convention=str(values["imu_convention"]),
            pelvis_R_imu=quaternion_wxyz_to_rotation(quaternion),
            yaw_alignment=str(values["yaw_alignment"]),
            support_mode=str(values["support_mode"]),
            initial_pelvis_position_world_m=np.asarray(
                values["initial_pelvis_position_world_m"], dtype=np.float64
            ).reshape(3),
            left_foot_frame=str(values["left_foot_frame"]),
            right_foot_frame=str(values["right_foot_frame"]),
            root_frame=str(values.get("root_frame", "torso_link")),
            full_body_urdf=urdf.resolve(),
            anchor_warning_m=float(values["anchor_warning_m"]),
            walking_translation_supported=bool(
                values["walking_translation_supported"]
            ),
            motor_index_to_joint=mapping,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.source != "SUPPORT_KINEMATIC_ODOMETRY":
            raise ValueError(f"unsupported floating-base source {self.source!r}")
        if self.quaternion_order != "WXYZ":
            raise ValueError("H2 floating-base estimator requires WXYZ quaternion order")
        if self.imu_convention != "FLU_ENU":
            raise ValueError("H2 floating-base estimator requires FLU_ENU convention")
        if self.yaw_alignment != "STARTUP_ZERO":
            raise ValueError("only STARTUP_ZERO yaw alignment is implemented")
        if self.support_mode != "DOUBLE_ASSUMED":
            raise ValueError("only DOUBLE_ASSUMED support is available without contacts")
        if self.walking_translation_supported:
            raise ValueError("walking translation cannot be enabled without odometry/contact")
        if not self.full_body_urdf.is_file():
            raise FileNotFoundError(self.full_body_urdf)
        if self.anchor_warning_m <= 0.0:
            raise ValueError("anchor_warning_m must be positive")
        if not self.motor_index_to_joint:
            raise ValueError("motor_index_to_joint mapping must not be empty")

    def map_motor_q(self, motor_q: Mapping[int, float]) -> dict[str, float]:
        """按 ``q_model = sign * q_hardware + offset`` 把电机角映射为关节角字典。"""
        joints: dict[str, float] = {}
        for index, value in motor_q.items():
            entry = self.motor_index_to_joint.get(int(index))
            if entry is None:
                continue
            name, sign, offset = entry
            joints[name] = sign * float(value) + offset
        return joints


@dataclass(frozen=True)
class FloatingBaseInput:
    """一帧估计输入：时间戳、全身关节角（URDF 关节名→rad）、IMU 姿态与角速度。

    ``joint_q`` 至少要包含双腿 12 个关节；若要输出 ``world_T_root``（torso），
    还需要 3 个腰关节。缺失的关节按 URDF 中性位（0）处理。
    """

    timestamp_s: float
    joint_q: Mapping[str, float]
    imu_quaternion_wxyz: np.ndarray
    imu_gyroscope_rad_s: np.ndarray
    sequence: int | None = None

    def __post_init__(self) -> None:
        quaternion = np.asarray(self.imu_quaternion_wxyz, dtype=np.float64).reshape(-1)
        gyroscope = np.asarray(self.imu_gyroscope_rad_s, dtype=np.float64).reshape(-1)
        if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
            raise ValueError("IMU quaternion must be one finite WXYZ 4-vector")
        norm = float(np.linalg.norm(quaternion))
        if norm <= 1.0e-12:
            raise ValueError("IMU quaternion must be non-zero")
        if gyroscope.shape != (3,) or not np.all(np.isfinite(gyroscope)):
            raise ValueError("IMU gyroscope must be one finite 3-vector")
        if not np.isfinite(self.timestamp_s):
            raise ValueError("timestamp_s must be finite")
        object.__setattr__(self, "imu_quaternion_wxyz", quaternion / norm)
        object.__setattr__(self, "imu_gyroscope_rad_s", gyroscope.copy())
        object.__setattr__(self, "joint_q", dict(self.joint_q))


@dataclass(frozen=True)
class FloatingBaseState:
    """Estimated pelvis pose in the fixed task world.

    COM is deliberately absent: the mirrored articulation root is the pelvis.
    ``world_T_root`` 是 ``root_frame``（默认 torso_link）在世界系的位姿，由骨盆
    位姿乘全身 FK 得到，是手臂控制真正消费的量。
    """

    timestamp_s: float
    position_world: np.ndarray
    quaternion_world_wxyz: np.ndarray
    linear_velocity_world: np.ndarray
    angular_velocity_world: np.ndarray
    world_T_root: np.ndarray
    source: str
    quality: str
    support_state: str
    active_anchor: str
    base_pose_jump_m: float
    base_pose_jump_rad: float
    left_foot_position_world: np.ndarray
    right_foot_position_world: np.ndarray
    left_anchor_error_m: float
    right_anchor_error_m: float
    walking_translation_supported: bool

    def __post_init__(self) -> None:
        for name in (
            "position_world",
            "linear_velocity_world",
            "angular_velocity_world",
            "left_foot_position_world",
            "right_foot_position_world",
        ):
            value = np.asarray(getattr(self, name), dtype=np.float64).reshape(-1)
            if value.shape != (3,) or not np.all(np.isfinite(value)):
                raise ValueError(f"{name} must be one finite 3-vector")
            object.__setattr__(self, name, value.copy())
        quaternion = np.asarray(self.quaternion_world_wxyz, dtype=np.float64).reshape(-1)
        if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
            raise ValueError("quaternion_world_wxyz must be one finite 4-vector")
        norm = float(np.linalg.norm(quaternion))
        if norm <= 1.0e-12:
            raise ValueError("quaternion_world_wxyz must be non-zero")
        object.__setattr__(self, "quaternion_world_wxyz", quaternion / norm)
        root = np.asarray(self.world_T_root, dtype=np.float64)
        if root.shape != (4, 4) or not np.all(np.isfinite(root)):
            raise ValueError("world_T_root must be one finite 4x4 transform")
        object.__setattr__(self, "world_T_root", root.copy())
        for name in (
            "timestamp_s",
            "base_pose_jump_m",
            "base_pose_jump_rad",
            "left_anchor_error_m",
            "right_anchor_error_m",
        ):
            if not np.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")

    @property
    def world_T_pelvis(self) -> np.ndarray:
        transform = np.eye(4, dtype=np.float64)
        transform[:3, :3] = quaternion_wxyz_to_rotation(self.quaternion_world_wxyz)
        transform[:3, 3] = self.position_world
        return transform

    @property
    def anchor_consistent(self) -> bool:
        return self.quality == "DOUBLE_SUPPORT_GOOD"

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp_s": self.timestamp_s,
            "position_world": self.position_world.tolist(),
            "quaternion_world_wxyz": self.quaternion_world_wxyz.tolist(),
            "world_T_root": self.world_T_root.tolist(),
            "quality": self.quality,
            "support_state": self.support_state,
            "active_anchor": self.active_anchor,
            "base_pose_jump_m": self.base_pose_jump_m,
            "base_pose_jump_rad": self.base_pose_jump_rad,
            "left_anchor_error_m": self.left_anchor_error_m,
            "right_anchor_error_m": self.right_anchor_error_m,
        }


class SupportKinematicFloatingBase:
    """Estimate ``T_WORLD_PELVIS`` from IMU orientation and anchored leg FK."""

    def __init__(self, config: FloatingBaseConfig) -> None:
        self.config = config
        self.model = pin.buildModelFromUrdf(str(config.full_body_urdf))
        self.data = self.model.createData()
        self.left_foot_id = self._frame_id(config.left_foot_frame)
        self.right_foot_id = self._frame_id(config.right_foot_frame)
        self.root_id = self._frame_id(config.root_frame)
        self._alignment: np.ndarray | None = None
        self._left_anchor_world: np.ndarray | None = None
        self._right_anchor_world: np.ndarray | None = None
        self._last_state: FloatingBaseState | None = None
        self._last_sequence: int | None = None
        self._anchor_count = 0
        self._joint_ids = {
            name: self.model.getJointId(name)
            for name in self.model.names[1:]
        }

    # ------------------------------------------------------------------ 帧/FK
    def _frame_id(self, name: str) -> int:
        frame_id = int(self.model.getFrameId(name))
        if frame_id >= len(self.model.frames):
            raise ValueError(f"full-body URDF is missing frame {name!r}")
        return frame_id

    def _full_body_fk(self, joint_q: Mapping[str, float]) -> None:
        q = pin.neutral(self.model)
        for name, value in joint_q.items():
            joint_id = self._joint_ids.get(name)
            if joint_id is None:
                continue
            joint = self.model.joints[joint_id]
            if joint.nq != 1:
                raise ValueError(f"expected scalar H2 joint {name!r}, got nq={joint.nq}")
            q[joint.idx_q] = float(value)
        pin.forwardKinematics(self.model, self.data, q)
        pin.updateFramePlacements(self.model, self.data)

    def _leg_fk(self, joint_q: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """返回骨盆系下的 (左脚位置, 右脚位置, pelvis_T_root)。"""
        self._full_body_fk(joint_q)
        return (
            np.asarray(self.data.oMf[self.left_foot_id].translation, dtype=np.float64).copy(),
            np.asarray(self.data.oMf[self.right_foot_id].translation, dtype=np.float64).copy(),
            np.asarray(self.data.oMf[self.root_id].homogeneous, dtype=np.float64).copy(),
        )

    def _pelvis_rotation_world(self, sample: FloatingBaseInput) -> np.ndarray:
        world_R_imu = quaternion_wxyz_to_rotation(sample.imu_quaternion_wxyz)
        world_R_pelvis_raw = world_R_imu @ self.config.pelvis_R_imu.T
        if self._alignment is None:
            startup_yaw = float(
                np.arctan2(world_R_pelvis_raw[1, 0], world_R_pelvis_raw[0, 0])
            )
            self._alignment = _rotation_z(-startup_yaw)
        return self._alignment @ world_R_pelvis_raw

    # ------------------------------------------------------------------ 锚定
    @property
    def anchored(self) -> bool:
        return self._left_anchor_world is not None and self._right_anchor_world is not None

    @property
    def anchor_count(self) -> int:
        return self._anchor_count

    def anchor(self, sample: FloatingBaseInput) -> FloatingBaseState:
        """（重新）锚定世界系：yaw 归零、双脚当前位置固定为锚点。

        机器人必须双脚站定。走动（h2_gait）之后旧锚点失效，必须再调用一次。
        """
        self._alignment = None
        self._left_anchor_world = None
        self._right_anchor_world = None
        self._last_state = None
        self._last_sequence = None
        self._anchor_count += 1
        return self.update(sample)

    # ------------------------------------------------------------------ 更新
    def update(self, sample: FloatingBaseInput) -> FloatingBaseState:
        if (
            self._last_state is not None
            and sample.sequence is not None
            and sample.sequence == self._last_sequence
        ):
            return self._last_state

        world_R_pelvis = self._pelvis_rotation_world(sample)
        pelvis_p_left_foot, pelvis_p_right_foot, pelvis_T_root = self._leg_fk(sample.joint_q)
        if self._left_anchor_world is None or self._right_anchor_world is None:
            initial = self.config.initial_pelvis_position_world_m
            self._left_anchor_world = initial + world_R_pelvis @ pelvis_p_left_foot
            self._right_anchor_world = initial + world_R_pelvis @ pelvis_p_right_foot
            if self._anchor_count == 0:
                self._anchor_count = 1

        left_pelvis_estimate = (
            self._left_anchor_world - world_R_pelvis @ pelvis_p_left_foot
        )
        right_pelvis_estimate = (
            self._right_anchor_world - world_R_pelvis @ pelvis_p_right_foot
        )
        pelvis_position = 0.5 * (left_pelvis_estimate + right_pelvis_estimate)
        left_foot_world = pelvis_position + world_R_pelvis @ pelvis_p_left_foot
        right_foot_world = pelvis_position + world_R_pelvis @ pelvis_p_right_foot
        left_error = float(np.linalg.norm(left_foot_world - self._left_anchor_world))
        right_error = float(np.linalg.norm(right_foot_world - self._right_anchor_world))

        previous = self._last_state
        if previous is None:
            linear_velocity = np.zeros(3, dtype=np.float64)
            position_jump = 0.0
            orientation_jump = 0.0
        else:
            dt = sample.timestamp_s - previous.timestamp_s
            linear_velocity = (
                (pelvis_position - previous.position_world) / dt
                if dt > 1.0e-6
                else previous.linear_velocity_world.copy()
            )
            position_jump = float(
                np.linalg.norm(pelvis_position - previous.position_world)
            )
            previous_rotation = quaternion_wxyz_to_rotation(
                previous.quaternion_world_wxyz
            )
            orientation_jump = _rotation_distance(previous_rotation, world_R_pelvis)

        pelvis_omega = self.config.pelvis_R_imu @ sample.imu_gyroscope_rad_s
        angular_velocity_world = world_R_pelvis @ pelvis_omega
        quality = (
            "DOUBLE_SUPPORT_GOOD"
            if max(left_error, right_error) <= self.config.anchor_warning_m
            else "DOUBLE_SUPPORT_INCONSISTENT"
        )
        world_T_pelvis = np.eye(4, dtype=np.float64)
        world_T_pelvis[:3, :3] = world_R_pelvis
        world_T_pelvis[:3, 3] = pelvis_position
        world_T_root = world_T_pelvis @ pelvis_T_root

        result = FloatingBaseState(
            timestamp_s=sample.timestamp_s,
            position_world=pelvis_position,
            quaternion_world_wxyz=rotation_to_quaternion_wxyz(world_R_pelvis),
            linear_velocity_world=linear_velocity,
            angular_velocity_world=angular_velocity_world,
            world_T_root=world_T_root,
            source=self.config.source,
            quality=quality,
            support_state=self.config.support_mode,
            active_anchor=f"LEFT+RIGHT_ANCHOR_{self._anchor_count}",
            base_pose_jump_m=position_jump,
            base_pose_jump_rad=orientation_jump,
            left_foot_position_world=left_foot_world,
            right_foot_position_world=right_foot_world,
            left_anchor_error_m=left_error,
            right_anchor_error_m=right_error,
            walking_translation_supported=False,
        )
        self._last_state = result
        self._last_sequence = sample.sequence
        return result

    @property
    def last_state(self) -> FloatingBaseState | None:
        return self._last_state
