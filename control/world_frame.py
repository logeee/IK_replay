"""世界系估计：把一帧 rt/lowstate 采样变成 ``world_T_root``（torso_link 在世界系的位姿）。

本模块不接触 DDS。硬件侧（``adapters/reach/lowstate.py``）负责产出
:class:`LowStateSample`；这里只做电机序号→关节名映射并驱动
:class:`control.floating_base.SupportKinematicFloatingBase`。

语义
----
* ``anchor()``：机器人双脚站定时调用，世界系 = 此刻骨盆位姿（yaw 归零、脚位固定）。
  每次任务开始（取点之前）调用一次；``h2_gait`` 走动之后必须重新调用。
* ``update()``：每个控制周期调用，返回最新 :class:`FloatingBaseState`。
* ``world_T_root()``：最近一次估计的 ``world_T_torso``；未锚定时抛错，绝不静默退回单位阵。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import numpy as np

from control.floating_base import (
    FloatingBaseConfig,
    FloatingBaseInput,
    FloatingBaseState,
    SupportKinematicFloatingBase,
)


@dataclass(frozen=True)
class LowStateSample:
    """一帧 rt/lowstate 的只读快照（时间戳为接收时刻的 ``time.monotonic()``）。"""

    timestamp_s: float
    sequence: int
    motor_q: np.ndarray            # 全身电机角 [N]（rt/lowstate 序号顺序）
    motor_dq: np.ndarray | None    # 全身电机角速度 [N]，旧 SDK 可能没有
    imu_quaternion_wxyz: np.ndarray
    imu_gyroscope_rad_s: np.ndarray
    tick: int | None = None

    def __post_init__(self) -> None:
        q = np.asarray(self.motor_q, dtype=np.float64).reshape(-1)
        if q.size == 0 or not np.all(np.isfinite(q)):
            raise ValueError("motor_q must be a non-empty finite vector")
        object.__setattr__(self, "motor_q", q.copy())
        if self.motor_dq is not None:
            dq = np.asarray(self.motor_dq, dtype=np.float64).reshape(-1)
            if dq.shape != q.shape or not np.all(np.isfinite(dq)):
                raise ValueError("motor_dq must match motor_q shape and be finite")
            object.__setattr__(self, "motor_dq", dq.copy())
        quaternion = np.asarray(self.imu_quaternion_wxyz, dtype=np.float64).reshape(-1)
        if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
            raise ValueError("imu_quaternion_wxyz must be one finite 4-vector")
        object.__setattr__(self, "imu_quaternion_wxyz", quaternion.copy())
        gyro = np.asarray(self.imu_gyroscope_rad_s, dtype=np.float64).reshape(-1)
        if gyro.shape != (3,) or not np.all(np.isfinite(gyro)):
            raise ValueError("imu_gyroscope_rad_s must be one finite 3-vector")
        object.__setattr__(self, "imu_gyroscope_rad_s", gyro.copy())
        if not np.isfinite(self.timestamp_s):
            raise ValueError("timestamp_s must be finite")

    def arm_q(self, motor_indices) -> np.ndarray:
        return self.motor_q[np.asarray(motor_indices, dtype=int)].copy()

    def arm_dq(self, motor_indices) -> np.ndarray | None:
        if self.motor_dq is None:
            return None
        return self.motor_dq[np.asarray(motor_indices, dtype=int)].copy()


class WorldFrameNotAnchored(RuntimeError):
    """尚未锚定世界系就请求 ``world_T_root``。"""


class WorldFrameEstimator:
    """``LowStateSample`` -> ``FloatingBaseState``（含 ``world_T_root``）。"""

    def __init__(self, config: FloatingBaseConfig) -> None:
        self.config = config
        self._estimator = SupportKinematicFloatingBase(config)
        self._last_sample: LowStateSample | None = None

    @classmethod
    def from_yaml(cls, path: str | Path, *, project_root: str | Path) -> "WorldFrameEstimator":
        return cls(FloatingBaseConfig.from_yaml(path, project_root=project_root))

    # ------------------------------------------------------------------ 输入转换
    def _to_input(self, sample: LowStateSample) -> FloatingBaseInput:
        joint_q = self.config.map_motor_q(
            {index: float(value) for index, value in enumerate(sample.motor_q)}
        )
        return FloatingBaseInput(
            timestamp_s=sample.timestamp_s,
            joint_q=joint_q,
            imu_quaternion_wxyz=sample.imu_quaternion_wxyz,
            imu_gyroscope_rad_s=sample.imu_gyroscope_rad_s,
            sequence=sample.sequence,
        )

    # ------------------------------------------------------------------ 状态
    @property
    def anchored(self) -> bool:
        return self._estimator.anchored

    @property
    def anchor_count(self) -> int:
        return self._estimator.anchor_count

    @property
    def last_state(self) -> FloatingBaseState | None:
        return self._estimator.last_state

    def anchor(self, sample: LowStateSample) -> FloatingBaseState:
        self._last_sample = sample
        return self._estimator.anchor(self._to_input(sample))

    def update(self, sample: LowStateSample) -> FloatingBaseState:
        if not self.anchored:
            raise WorldFrameNotAnchored("world frame is not anchored; call anchor() while standing still")
        self._last_sample = sample
        return self._estimator.update(self._to_input(sample))

    def world_T_root(self) -> np.ndarray:
        state = self._estimator.last_state
        if state is None:
            raise WorldFrameNotAnchored("world frame is not anchored")
        return state.world_T_root.copy()

    def snapshot(self) -> dict[str, object]:
        state = self._estimator.last_state
        return {
            "anchored": self.anchored,
            "anchor_count": self.anchor_count,
            "source": self.config.source,
            "support_mode": self.config.support_mode,
            "root_frame": self.config.root_frame,
            "anchor_warning_m": self.config.anchor_warning_m,
            "state": None if state is None else state.to_dict(),
        }


def transform_point(world_T_root: np.ndarray, p_root: Mapping | np.ndarray) -> list[float]:
    """把根系下的点换到世界系（取点时使用）。"""
    p = np.asarray(p_root, dtype=np.float64).reshape(3)
    return (np.asarray(world_T_root)[:3, :3] @ p + np.asarray(world_T_root)[:3, 3]).tolist()
