"""rt/lowstate 全身采样器（pink 后端用）：电机角/角速度 + IMU 四元数/陀螺仪 + 新鲜度。

现有 ``H2PoseProvider``/``H2ArmController`` 只暴露手臂 7 关节与腰/IMU 姿态，
不含陀螺仪、``tick`` 与全身 ``dq``，所以浮动基座估计需要自己的只读订阅。
DDS ChannelFactory 在进程内只初始化一次（``backend.dds.ensure_dds_initialized``），
多一个 subscriber 没有副作用，且**不创建任何 publisher**。

``MockLowStateSampler`` 用于 ``--no-robot`` 与离线回放：站立中性位 + 可注入的
躯干俯仰/位移扰动。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

from control.world_frame import LowStateSample

H2_MOTOR_COUNT = 31


class LowStateSampler:
    """协议：``sample()`` 返回最新帧；``age_ms()`` 返回该帧距现在的毫秒数。"""

    def sample(self) -> LowStateSample:  # pragma: no cover - protocol
        raise NotImplementedError

    def age_ms(self) -> float:  # pragma: no cover - protocol
        raise NotImplementedError

    def close(self) -> None:
        return None


class H2LowStateSampler(LowStateSampler):
    """独立只读订阅 rt/lowstate。"""

    def __init__(self, network_interface: str | None = None, lowstate_timeout: float = 5.0) -> None:
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

        from backend.dds import ensure_dds_initialized  # hand_eye_3D

        ensure_dds_initialized(network_interface)
        self._lock = threading.Lock()
        self._msg = None
        self._received_at: float | None = None
        self._sequence = 0
        self._subscriber = ChannelSubscriber("rt/lowstate", LowState_)
        self._subscriber.Init(self._on_low_state, 10)
        deadline = time.monotonic() + lowstate_timeout
        while self._msg is None:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{lowstate_timeout:.0f}s 内没收到 rt/lowstate（pink 采样器）")
            time.sleep(0.05)

    def _on_low_state(self, msg) -> None:
        now = time.monotonic()
        with self._lock:
            self._msg = msg
            self._received_at = now
            self._sequence += 1

    def sample(self) -> LowStateSample:
        with self._lock:
            msg, received_at, sequence = self._msg, self._received_at, self._sequence
        if msg is None or received_at is None:
            raise RuntimeError("HardwareStateUnavailable: no H2 lowstate sample")
        motors = list(msg.motor_state)[:H2_MOTOR_COUNT]
        q = np.asarray([float(m.q) for m in motors], dtype=np.float64)
        dq = None
        if motors and all(hasattr(m, "dq") for m in motors):
            dq_values = np.asarray([float(m.dq) for m in motors], dtype=np.float64)
            dq = dq_values if np.all(np.isfinite(dq_values)) else None
        imu = msg.imu_state
        return LowStateSample(
            timestamp_s=received_at,
            sequence=sequence,
            motor_q=q,
            motor_dq=dq,
            imu_quaternion_wxyz=np.asarray(imu.quaternion, dtype=np.float64),
            imu_gyroscope_rad_s=np.asarray(imu.gyroscope, dtype=np.float64),
            tick=int(getattr(msg, "tick", 0)) or None,
        )

    def age_ms(self) -> float:
        with self._lock:
            received_at = self._received_at
        if received_at is None:
            return float("inf")
        return (time.monotonic() - received_at) * 1e3


class MockLowStateSampler(LowStateSampler):
    """无真机：站立中性位；``set_disturbance`` 注入躯干俯仰/位移用于离线验证。

    ``arm_q_reader``（可选）：返回被控手臂 7 关节角的无参函数——``--no-robot``
    时 reach 的模拟控制器持有指令角，把它接进来，PINK 闭环才能“看到”手臂在动。
    """

    def __init__(
        self,
        arm_motor_indices=None,
        arm_q_reader: Callable[[], np.ndarray] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._sequence = 0
        self._arm_indices = None if arm_motor_indices is None else np.asarray(arm_motor_indices, dtype=int)
        self._arm_q_reader = arm_q_reader
        self._pitch_rad = 0.0
        self._yaw_rad = 0.0
        self._gyro = np.zeros(3)
        self._motor_q = np.zeros(H2_MOTOR_COUNT)
        self._lock = threading.Lock()

    def set_disturbance(self, *, pitch_rad: float = 0.0, yaw_rad: float = 0.0,
                        waist_pitch_rad: float | None = None) -> None:
        """模拟躯干扰动。pitch/yaw 作用在 IMU（整体倾斜）；waist_pitch 作用在腰关节。"""
        with self._lock:
            self._pitch_rad = float(pitch_rad)
            self._yaw_rad = float(yaw_rad)
            if waist_pitch_rad is not None:
                self._motor_q[14] = float(waist_pitch_rad)

    def set_motor_q(self, index: int, value: float) -> None:
        with self._lock:
            self._motor_q[int(index)] = float(value)

    def sample(self) -> LowStateSample:
        with self._lock:
            q = self._motor_q.copy()
            pitch, yaw = self._pitch_rad, self._yaw_rad
            self._sequence += 1
            sequence = self._sequence
        if self._arm_indices is not None and self._arm_q_reader is not None:
            try:
                q[self._arm_indices] = np.asarray(self._arm_q_reader(), dtype=np.float64).reshape(-1)
            except Exception:
                pass
        cp, sp = np.cos(pitch / 2), np.sin(pitch / 2)
        cy, sy = np.cos(yaw / 2), np.sin(yaw / 2)
        quaternion = np.array([cp * cy, -sp * sy, sp * cy, cp * sy])  # roll=0 的 RPY→WXYZ
        return LowStateSample(
            timestamp_s=float(self._clock()),
            sequence=sequence,
            motor_q=q,
            motor_dq=np.zeros(H2_MOTOR_COUNT),
            imu_quaternion_wxyz=quaternion,
            imu_gyroscope_rad_s=self._gyro,
            tick=None,
        )

    def age_ms(self) -> float:
        return 0.0
