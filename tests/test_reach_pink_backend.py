"""pink 运动后端的离线闭环验证（走真实的 adapters/reach 执行入口，无真机）。

* 假 H2ArmController：复刻 set_target / 矢量同步限速 / status() 语义；
* MockLowStateSampler：站立中性位，执行中途注入躯干后仰 + 腰俯仰；
* 走 ``execution._exec_loop`` → 按 ``state.motion_backend`` 分发到 ``exec_loop_pink``。

验收：
1. 世界系终态 TCP 误差 < 3 mm（躯干动了仍对准取点时刻冻结的世界系目标）；
2. 最终关节角 ≠ 规划终点（说明确实做了补偿）；
3. 执行记录写入 execution_history 且带 pink 摘要；
4. legacy 后端路径不受影响（同一假控制器跑一遍原逻辑）。
"""

from __future__ import annotations

import threading
import time
import unittest

import numpy as np

from adapters.reach import execution
from adapters.reach.execution_pink import PinkRuntime, normalize_motion_backend
from adapters.reach.lowstate import MockLowStateSampler
from adapters.reach.state import state

RIGHT_ARM_MOTORS = (22, 23, 24, 25, 26, 27, 28)
JOINT_NAMES = [
    "right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_shoulder_yaw_joint",
    "right_elbow_joint", "right_wrist_roll_joint", "right_wrist_pitch_joint", "right_wrist_yaw_joint",
]
Q_START = np.array([0.2, -0.25, 0.0, 0.9, 0.0, -0.1, 0.0])
Q_GOAL = np.array([-0.3, -0.35, 0.1, 1.9, 0.05, -0.2, 0.1])
DT = 0.02


class FakeArmController:
    """H2ArmController 的执行语义子集：50 Hz 线程把 cmd 按矢量同步限速滑向 desired。"""

    def __init__(self, q0: np.ndarray, max_speed: float = 0.4) -> None:
        self.max_speed = float(max_speed)
        self._speed_ceiling = self.max_speed
        self._lock = threading.Lock()
        self._cmd = q0.copy()
        self._desired = q0.copy()
        self._jog = False
        self._tau = np.zeros(7)
        self.targets: list[np.ndarray] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                step = self.max_speed * DT
                delta = self._desired - self._cmd
                worst = float(np.max(np.abs(delta)))
                if worst > step:
                    delta = delta * (step / worst)
                self._cmd = self._cmd + delta
            time.sleep(DT)

    def shutdown(self) -> None:
        self._stop.set()
        self._thread.join(1.0)

    # ---- H2ArmController API 子集 ----
    def enable_jog(self) -> None:
        with self._lock:
            self._jog = True
            self._desired = self._cmd.copy()

    def disable_jog(self) -> None:
        with self._lock:
            self._jog = False
            self._desired = self._cmd.copy()
            self._tau[:] = 0.0

    def stop(self) -> None:
        self.disable_jog()

    def set_max_speed(self, v: float) -> None:
        with self._lock:
            self.max_speed = float(np.clip(v, 0.05, self._speed_ceiling))

    def set_target(self, q) -> bool:
        with self._lock:
            if not self._jog:
                return False
            self._desired = np.asarray(q, dtype=float).copy()
            self.targets.append(self._desired.copy())
            return True

    def set_tau_ff(self, tau) -> bool:
        with self._lock:
            self._tau = np.asarray(tau, dtype=float)
            return True

    def read_measured(self) -> np.ndarray:
        with self._lock:
            return self._cmd.copy()

    def status(self) -> dict:
        with self._lock:
            return {
                "cmd_rad": self._cmd.tolist(),
                "desired_rad": self._desired.tolist(),
                "measured_rad": self._cmd.tolist(),
                "jog_enabled": self._jog,
                "float": False,
                "kp": 140.0,
                "kp_wrist": 60.0,
            }


class PinkBackendOfflineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ctl = FakeArmController(Q_START)
        self.sampler = MockLowStateSampler(arm_motor_indices=RIGHT_ARM_MOTORS,
                                           arm_q_reader=self.ctl.read_measured)
        self.rt = PinkRuntime(arm_side="right", sampler=self.sampler, wrist_link="right_wrist_yaw_link")
        self._saved = {k: getattr(state, k) for k in (
            "controller", "pink_runtime", "motion_backend", "p_tool", "joint_names",
            "log_dir", "pick_history_dir", "settle_trim", "exec_running", "exec_phase")}
        state.controller = self.ctl
        state.pink_runtime = self.rt
        state.motion_backend = "pink"
        state.p_tool = [0.08, 0.0, 0.0]
        state.joint_names = list(JOINT_NAMES)
        state.log_dir = None
        state.pick_history_dir = None
        state.settle_trim = "off"
        state.exec_cancel.clear()
        state.exec_running = True
        with state.execution_history_lock:
            state.execution_history.clear()

    def tearDown(self) -> None:
        self.ctl.shutdown()
        for k, v in self._saved.items():
            setattr(state, k, v)
        state.exec_cancel.clear()

    def _q_list(self, n: int = 12) -> list[np.ndarray]:
        return [Q_START + (Q_GOAL - Q_START) * s for s in np.linspace(0, 1, n)]

    def test_execute_compensates_torso_disturbance_in_world_frame(self) -> None:
        # 1) 站定锚定 → 2) 取点（记录 world_T_root）→ 3) 执行中整机绕脚后仰 2°（躯干后移约 3.6 cm）+ 腰俯仰 0.04 rad
        self.rt.anchor()
        self.assertIsNotNone(self.rt.capture_pick_frame())
        controller = self.rt.controller_for_tool(state.p_tool)
        fk = lambda q: controller.root_T_tcp_actual(q).homogeneous  # noqa: E731
        goal_world = self.rt.pick_world_T_root @ fk(Q_GOAL)

        def disturb() -> None:
            time.sleep(0.8)
            for a in np.linspace(0.0, 1.0, 20):
                self.sampler.set_disturbance(pitch_rad=-np.deg2rad(2) * a, waist_pitch_rad=0.04 * a)
                time.sleep(0.03)

        threading.Thread(target=disturb, daemon=True).start()
        t0 = time.monotonic()
        execution._exec_loop(self._q_list(), 2.0, speed=0.4, label="pink-offline",
                             command_start_q=Q_START.copy())
        elapsed = time.monotonic() - t0

        self.assertIn("完成", state.exec_message, state.exec_message)
        self.assertFalse(state.exec_running)
        self.assertEqual(state.exec_phase, "idle")
        summary = self.rt.last_summary
        self.assertIsNotNone(summary)
        self.assertEqual(summary["qp_failures"], 0)
        self.assertEqual(summary["faults"], [])
        self.assertLess(summary["final_position_error_m"], 0.003, summary)

        # 用扰动后的真实 world_T_root 独立复算世界系 TCP
        _, fb = self.rt.update_world()
        q_final = self.ctl.read_measured()
        tcp_world = fb.world_T_root @ fk(q_final)
        err_mm = np.linalg.norm(tcp_world[:3, 3] - goal_world[:3, 3]) * 1e3
        self.assertLess(err_mm, 3.0, f"world TCP error {err_mm:.1f} mm")
        # 补偿确实发生：关节终值偏离规划终点
        self.assertGreater(np.max(np.abs(q_final - Q_GOAL)), 0.02)
        self.assertGreater(len(self.ctl.targets), 50)
        self.assertLess(elapsed, 12.0)

        with state.execution_history_lock:
            records = list(state.execution_history)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["motion_backend"], "pink")
        self.assertEqual(rec["pink"]["world_T_root_ref_source"], "pick")
        self.assertIn("plan", rec["pink"])
        self.assertGreater(len(rec["pink"]["steps"]), 5)

    def test_execute_requires_anchor(self) -> None:
        execution._exec_loop(self._q_list(), 1.0, speed=0.4, label="pink-noanchor",
                             command_start_q=Q_START.copy())
        self.assertIn("世界系未锚定", state.exec_message)
        self.assertFalse(state.exec_running)
        np.testing.assert_allclose(self.ctl.read_measured(), Q_START, atol=1e-9)

    def test_reanchor_invalidates_pick_frame_and_falls_back_to_execution_start(self) -> None:
        self.rt.anchor()
        self.rt.capture_pick_frame()
        self.rt.anchor()  # 走动后重新锚定 → 取点世界系作废
        self.assertIsNone(self.rt.pick_world_T_root)
        execution._exec_loop(self._q_list(6), 1.0, speed=0.4, label="pink-fallback",
                             command_start_q=Q_START.copy())
        self.assertIn("完成", state.exec_message)
        with state.execution_history_lock:
            rec = list(state.execution_history)[-1]
        self.assertEqual(rec["pink"]["world_T_root_ref_source"], "execution_start")

    def test_cancel_holds_current_position(self) -> None:
        self.rt.anchor()
        threading.Timer(0.5, state.exec_cancel.set).start()
        execution._exec_loop(self._q_list(), 3.0, speed=0.4, label="pink-cancel",
                             command_start_q=Q_START.copy())
        self.assertIn("已中止", state.exec_message)
        q = self.ctl.read_measured()
        self.assertGreater(np.max(np.abs(q - Q_START)), 0.01)   # 走了一段
        self.assertGreater(np.max(np.abs(q - Q_GOAL)), 0.2)     # 没走完

    def test_legacy_backend_path_is_unchanged(self) -> None:
        state.motion_backend = "legacy"
        state.pink_runtime = None
        execution._exec_loop(self._q_list(6), 1.0, speed=0.4, label="legacy",
                             command_start_q=Q_START.copy())
        self.assertIn("完成", state.exec_message)
        np.testing.assert_allclose(self.ctl.read_measured(), Q_GOAL, atol=1e-6)
        with state.execution_history_lock:
            rec = list(state.execution_history)[-1]
        self.assertEqual(rec["motion_backend"], "legacy")
        self.assertNotIn("pink", rec)

    def test_normalize_motion_backend(self) -> None:
        self.assertEqual(normalize_motion_backend(None), "legacy")
        self.assertEqual(normalize_motion_backend(" Pink "), "pink")
        with self.assertRaises(ValueError):
            normalize_motion_backend("curobo")


if __name__ == "__main__":
    unittest.main()
