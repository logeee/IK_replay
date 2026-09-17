"""One arm_sdk publisher, two independently configured legacy arm channels.

No DDS publisher is created until an operator takes control. Unloading keeps
the original Kp=0 / hand_move_kd / grav_in_float semantics on each channel.
"""
from __future__ import annotations

import threading
import time
import numpy as np


class DualArmController:
    def __init__(self, network_interface=None, *, channel_factory=None, transport=None,
                 lowstate_reader=None):
        self.network_interface = network_interface
        self.options = {}
        self.channels = {}
        self.active = set()
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._transport = transport
        self._channel_factory = channel_factory
        self._lowstate_reader = lowstate_reader
        self.weight = 0.0
        self.error = None

    @property
    def engaged(self):
        return self._thread is not None and self._thread.is_alive()

    def configure(self, side, options):
        if side not in ("left", "right"):
            raise ValueError("arm must be left/right")
        with self.lock:
            if self.engaged:
                raise RuntimeError("修改补偿或手配置前请先释放双臂")
            self.options[side] = dict(options)
            self.channels.clear()

    def acquire(self, side):
        with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            if not self.channels:
                self._initialize()
            channel = self.channels[side]
            channel.stop()
            self.active.add(side)
            channel._engaged = True
            if not self.engaged:
                self._stop.clear()
                self.weight = 0.0
                self._thread = threading.Thread(target=self._loop, name="dual-arm-sdk", daemon=True)
                self._thread.start()
            return channel

    def _initialize(self):
        if self._transport is None:
            self._transport = DDSTransport(
                self.network_interface,
                lowstate_reader=self._lowstate_reader,
            )
        factory = self._channel_factory
        if factory is None:
            from calib_workstation.calib3d.arm import H2ArmController
            owner = self

            class ManagedArm(H2ArmController):
                def start(self):
                    # The owner's single loop already publishes both arms.
                    pass

                def shutdown(self):
                    owner.release(self.arm)

            factory = ManagedArm
        channels = {}
        for side in ("left", "right"):
            sequence = (self._transport.sequence()
                        if hasattr(self._transport, "sequence") else None)
            channels[side] = factory(
                arm=side, network_interface=self.network_interface,
                shared_transport=self._transport, **self.options.get(side, {}))
            # Robot-model / gravity initialization can hold the Python GIL for
            # slightly longer than the 500 ms safety window.  No command has
            # been published yet, so wait for the first frame received *after*
            # this initialization before starting the 50 Hz control loop.
            if hasattr(self._transport, "wait_for_fresh"):
                self._transport.wait_for_fresh(after_sequence=sequence)
        self.channels = channels

    def release(self, side):
        with self.lock:
            channel = self.channels.get(side)
            if channel is not None:
                channel.stop()
                channel._engaged = False
            self.active.discard(side)
            if self.active:
                # The shared weight must remain unchanged for the other arm.
                return
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(2.0)
            if thread.is_alive():
                raise RuntimeError("双臂发送线程尚未退出")
        with self.lock:
            self.channels.clear()
            self._thread = None

    def shutdown(self):
        for side in tuple(self.active):
            self.release(side)

    def tick(self, dt=0.02):
        """Advance both original arm laws, then atomically send one command."""
        with self.lock:
            self.weight = max(0.0, self.weight - dt) if self._stop.is_set() else min(1.0, self.weight + dt)
            outputs = {}
            for side, channel in self.channels.items():
                with channel._lock:
                    floating = channel._float
                    channel._weight = self.weight
                    if not floating:
                        delta = channel._desired_q - channel._cmd_q
                        largest = float(np.max(np.abs(delta)))
                        step = channel.max_speed * dt
                        if largest > step:
                            delta = delta * (step / largest)
                        channel._cmd_q = channel._cmd_q + delta
                    q = channel._cmd_q.copy()
                    push = channel._tau_push.copy()
                    kp = np.zeros(channel.n) if floating else channel.kp_vec.copy()
                    kd = np.full(channel.n, channel.hand_move_kd) if floating else channel.kd_vec.copy()
                # Read failures are not silently replaced by a stale float pose.
                q_gravity = channel.read_measured() if floating else q
                tau = channel._compute_tau(q_gravity, push, floating, self.weight)
                outputs[side] = {"indices": channel._jog_indices, "q": q, "kp": kp, "kd": kd, "tau": tau}
            self._transport.write(outputs, self.weight)
            now = time.monotonic()
            for side, output in outputs.items():
                channel = self.channels[side]
                with channel._lock:
                    channel._last_sent_q = output["q"].copy()
                    channel._last_sent_tau_ff = output["tau"].copy()
                    channel._last_sent_at = now
                    channel._last_sent_sequence += 1

    def _loop(self):
        deadline = time.monotonic()
        while True:
            try:
                self.tick()
            except Exception as exc:
                self.error = f"双臂控制数据/发送失败: {exc}"
                # Retain the last finite command while ramping control out.
                try:
                    self._transport.ramp_out(self.weight)
                finally:
                    self.weight = 0.0
                return
            if self._stop.is_set() and self.weight <= 0:
                return
            deadline += 0.02
            time.sleep(max(0.0, deadline - time.monotonic()))


class DDSTransport:
    MAX_LOWSTATE_AGE_S = 0.5

    def __init__(self, network_interface, *, lowstate_reader=None):
        from calib_workstation.calib3d.dds import ensure_dds_initialized
        from calib_workstation.calib3d.arm import _make_crc
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.utils.crc import CRC
        ensure_dds_initialized(network_interface)
        self._lock = threading.Lock()
        self._sample = None
        self._received_at = 0.0
        self._sequence = 0
        self._lowstate_reader = lowstate_reader
        self._subscriber = None
        if lowstate_reader is None:
            # 仅保留给独立使用者的兼容回退。17001/18001 生产路径会传入
            # H2PoseProvider.read_low_state_snapshot，不再建立重复订阅。
            self._subscriber = ChannelSubscriber("rt/lowstate", LowState_)
            self._subscriber.Init(self._receive, 10)
            deadline = time.monotonic() + 5.0
            while self._sample is None:
                if time.monotonic() > deadline:
                    raise RuntimeError("5 秒内未收到全身关节状态")
                time.sleep(0.02)
        # 在创建发送端之前先验证共享源可读且新鲜。
        self.snapshot()
        self._publisher = ChannelPublisher("rt/arm_sdk", LowCmd_)
        self._publisher.Init()
        self._command = unitree_hg_msg_dds__LowCmd_()
        self._crc = _make_crc(CRC)
        self._last_outputs = None

    def _receive(self, sample):
        with self._lock:
            self._sample, self._received_at = sample, time.monotonic()
            self._sequence += 1

    def snapshot(self):
        sample, received_at, sequence = self._raw_snapshot()
        self._validate_fresh(sample, received_at, sequence)
        return sample

    def _raw_snapshot(self):
        if self._lowstate_reader is not None:
            return self._lowstate_reader()
        with self._lock:
            return self._sample, self._received_at, self._sequence

    def _validate_fresh(self, sample, received_at, sequence):
        age_s = time.monotonic() - float(received_at or 0.0)
        if sample is None or age_s > self.MAX_LOWSTATE_AGE_S:
            age_ms = max(0.0, age_s * 1000.0)
            source = "共享只读订阅" if self._lowstate_reader is not None else "独立订阅"
            raise RuntimeError(
                f"lowstate 已过期（{age_ms:.0f} ms，序号 {sequence}，{source}）"
            )

    def sequence(self):
        return self._raw_snapshot()[2]

    def wait_for_fresh(self, *, after_sequence=None, timeout_s=1.0):
        """初始化阶段等待一张新帧；不放宽执行阶段的 500 ms 安全门限。"""
        deadline = time.monotonic() + float(timeout_s)
        last = None
        while time.monotonic() < deadline:
            sample, received_at, sequence = self._raw_snapshot()
            last = (sample, received_at, sequence)
            is_new = after_sequence is None or sequence > after_sequence
            if is_new:
                try:
                    self._validate_fresh(sample, received_at, sequence)
                    return sample
                except RuntimeError:
                    pass
            time.sleep(0.005)
        sample, received_at, sequence = last or (None, 0.0, 0)
        age_ms = max(0.0, (time.monotonic() - float(received_at or 0.0)) * 1000.0)
        raise RuntimeError(
            "控制通道初始化后未等到新鲜 lowstate"
            f"（{age_ms:.0f} ms，序号 {sequence}，初始化前序号 {after_sequence}）"
        )

    def write(self, outputs, weight):
        self.snapshot()
        self._publish(outputs, weight)
        self._last_outputs = outputs

    def _publish(self, outputs, weight):
        command = self._command
        command.motor_cmd[31].q = float(weight)
        for data in outputs.values():
            if not all(np.all(np.isfinite(data[key])) for key in ("q", "tau", "kp", "kd")):
                raise RuntimeError("双臂指令包含非有限值")
            for i, index in enumerate(data["indices"]):
                motor = command.motor_cmd[index]
                motor.q, motor.dq = float(data["q"][i]), 0.0
                motor.tau = float(data["tau"][i])
                motor.kp, motor.kd = float(data["kp"][i]), float(data["kd"][i])
        command.crc = self._crc.Crc(command)
        self._publisher.Write(command)

    def ramp_out(self, weight):
        if self._last_outputs is None:
            return
        original = max(weight, 1e-9)
        while weight > 0:
            weight = max(0.0, weight - 0.02)
            outputs = {side: {**data, "tau": data["tau"] * (weight / original)}
                       for side, data in self._last_outputs.items()}
            self._publish(outputs, weight)
            time.sleep(0.02)
