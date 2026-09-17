"""Cabinet X force feedforward, bound to an A/B endpoint plan.

No hardware thread: the owning execution loop calls update at its control cadence.
The force is expressed at the configured TCP and mapped at measured joint angles.
"""
from __future__ import annotations

import time
import numpy as np

from .state import state


def parse_assist(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"direction", "force_n", "ramp_s", "hold_s", "release_s"}:
        raise ValueError("柜面助力参数不完整")
    if value["direction"] not in ("a_to_b", "b_to_a"):
        raise ValueError("柜面助力方向必须为 A→B 或 B→A")
    result = {"direction": value["direction"]}
    for key, low, high in (("force_n", 0., 40.), ("ramp_s", .1, 5.),
                           ("hold_s", 0., 5.), ("release_s", .1, 5.)):
        v = value[key]
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not np.isfinite(v) or not low <= v <= high:
            raise ValueError(f"柜面助力 {key} 必须为 {low:g}～{high:g} 内的数字")
        result[key] = float(v)
    return result


def validate_execution_assist(value, proof, push=None):
    spec = parse_assist(value)
    expected = (proof or {}).get("cabinet_assist")
    if spec != expected:
        raise ValueError("柜面助力与规划不一致，请重新规划")
    if spec is not None:
        if proof.get("reference_kind") != "cabinet_waypoint" or push is not None:
            raise ValueError("柜面助力仅用于 A/B 点位，不能同时叠加旧横移推力")
    return spec


def smooth_fraction(value):
    u = float(np.clip(value, 0., 1.))
    return u * u * (3. - 2. * u)


class CabinetAssist:
    def __init__(self, ctl, proof, jacobian):
        self.ctl, self.proof, self.jacobian = ctl, proof, jacobian
        self.spec = parse_assist(proof["cabinet_assist"])
        self.axis = np.asarray(proof["cabinet_x_axis_root"], dtype=float)
        if self.axis.shape != (3,) or not np.isfinite(self.axis).all() or not np.isclose(np.linalg.norm(self.axis), 1.):
            raise ValueError("柜面助力轴无效，请重新定位")
        self.world_ref = proof.get("world_T_root_ref")
        self.started = None
        self.gain = 0.

    def clear(self):
        self.gain = 0.
        try:
            self.ctl.set_tau_ff(np.zeros(len(state.joint_names)))
        except Exception:
            self.ctl.stop()

    def update(self, world_R_root=None, *, gain=None, active=True):
        if state.exec_cancel.is_set() or not active:
            self.clear()
            self.started = None
            return
        now = time.monotonic()
        if self.started is None:
            self.started = now
        self.gain = (smooth_fraction((now - self.started) / self.spec["ramp_s"])
                     if gain is None else float(gain))
        direction = self.axis
        if world_R_root is not None:
            # Keep the cabinet force fixed in the anchored world as the base moves.
            direction = np.asarray(world_R_root).T @ np.asarray(self.world_ref)[:3, :3] @ direction
        force = direction * self.spec["force_n"] * self.gain
        if self.spec["direction"] == "b_to_a":
            force = -force
        q = np.asarray(self.ctl.read_measured(), dtype=float)
        if q.shape != (len(state.joint_names),) or not np.isfinite(q).all():
            raise RuntimeError("助力实测关节角无效")
        J = np.asarray(self.jacobian(dict(zip(state.joint_names, q))))
        if J.shape != (3, len(q)) or not np.isfinite(J).all():
            raise RuntimeError("助力雅可比无效")
        if self.ctl.set_tau_ff(J.T @ force) is False and not state.exec_cancel.is_set():
            raise RuntimeError("助力下发失败：手臂已停止或退出接管")

    def finish(self, world_rotation=lambda: None):
        # Preserve the real endpoint target throughout hold and release. Never
        # substitute measured position or bypass the owning loop's goal check.
        try:
            if self.spec["hold_s"] > 0:
                state.exec_phase, state.exec_message = "push_hold", "柜面助力保持中"
                end = time.monotonic() + self.spec["hold_s"]
                while time.monotonic() < end and not state.exec_cancel.is_set():
                    self.update(world_rotation())
                    time.sleep(.02)
            state.exec_phase, state.exec_message = "release", "柜面助力撤除中"
            initial, start = self.gain, time.monotonic()
            while not state.exec_cancel.is_set():
                fraction = (time.monotonic() - start) / self.spec["release_s"]
                self.update(world_rotation(), gain=initial * (1. - smooth_fraction(fraction)))
                if fraction >= 1.:
                    break
                time.sleep(.02)
        finally:
            self.clear()


def make_assist(ctl, proof, jacobian):
    spec = (proof or {}).get("cabinet_assist")
    return CabinetAssist(ctl, proof, jacobian) if spec and spec["force_n"] > 0 else None
