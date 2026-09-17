"""One arm_sdk publisher, two independently configured legacy arm channels.

No DDS publisher is created until an operator takes control. Unloading keeps
the original Kp=0 / hand_move_kd / grav_in_float semantics on each channel.
"""
from __future__ import annotations

import threading
import time
import numpy as np


class DualArmController:
    def __init__(self, network_interface=None, *, channel_factory=None, transport=None):
        self.network_interface = network_interface
        self.options = {}
        self.channels = {}
        self.active = set()
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._transport = transport
        self._channel_factory = channel_factory
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
            self._transport = DDSTransport(self.network_interface)
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
            channels[side] = factory(
                arm=side, network_interface=self.network_interface,
                shared_transport=self._transport, **self.options.get(side, {}))
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
    def __init__(self, network_interface):
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
        self._subscriber = ChannelSubscriber("rt/lowstate", LowState_)
        self._subscriber.Init(self._receive, 10)
        deadline = time.monotonic() + 5.0
        while self._sample is None:
            if time.monotonic() > deadline:
                raise RuntimeError("5 秒内未收到全身关节状态")
            time.sleep(0.02)
        self._publisher = ChannelPublisher("rt/arm_sdk", LowCmd_)
        self._publisher.Init()
        self._command = unitree_hg_msg_dds__LowCmd_()
        self._crc = _make_crc(CRC)
        self._last_outputs = None

    def _receive(self, sample):
        with self._lock:
            self._sample, self._received_at = sample, time.monotonic()

    def snapshot(self):
        with self._lock:
            if self._sample is None or time.monotonic() - self._received_at > 0.25:
                raise RuntimeError("lowstate 已过期")
            return self._sample

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
