"""reference_builder + tracking_session 的离线闭环验证（无真机）。

用一个"理想执行器"（下一拍实测 q = 上一拍 q_target）模拟手臂，在轨迹中途
注入躯干后仰/平移，验证：
* 世界系 TCP 最终落在世界系目标上（躯干动了也对得准——本次迁移的核心目标）；
* 若不做世界系补偿（world_T_root 恒为单位阵），同样的躯干扰动会产生明显偏差；
* QP 连续失败 → RECOVERING_HOLD 保持最后目标 → 自动重规划恢复；
* 人工 HOLD 冻结轨迹时钟、RESUME 后继续；
* 时间缩放遵守关节速度上限。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pinocchio as pin
import yaml

from control.approach_tracker import ApproachTrackerConfig
from control.fault_supervisor import FaultSupervisor, SupervisorState
from control.pink_arm_controller import PinkArmController
from control.reference_builder import build_hold_plan, build_recovery_plan, build_reference_plan
from control.tool_config import ToolConfig
from control.tracking_session import TrackingSession

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config/robots/h2_pink_right.yaml"
Q_START = np.array([0.2, -0.25, 0.0, 0.9, 0.0, -0.1, 0.0])
Q_GOAL = np.array([-0.3, -0.35, 0.1, 1.9, 0.05, -0.2, 0.1])  # 肘部弯曲较大，倾斜后仍在工作空间内
DT = 0.02


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def make_controller() -> tuple[PinkArmController, dict]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    wrist_T_tcp = np.eye(4)
    wrist_T_tcp[0, 3] = 0.08
    tool = ToolConfig("right_tcp", "right_wrist_yaw_link", wrist_T_tcp)
    controller = PinkArmController(PROJECT_ROOT / config["model"]["urdf_path"], config, tool)
    return controller, config


def lean(pitch_rad: float, dx: float = 0.0, dz: float = 0.0) -> np.ndarray:
    return pin.SE3(pin.rpy.rpyToMatrix(0.0, pitch_rad, 0.0), np.array([dx, 0.0, dz])).homogeneous


EXECUTOR_MAX_QDOT = 0.4  # H2ArmController --arm-max-speed 默认天花板


def executor_follow(q_cmd: np.ndarray, q_target: np.ndarray, max_qdot: float = EXECUTOR_MAX_QDOT) -> np.ndarray:
    """H2ArmController._loop 的矢量同步限速：按最饱和关节整体等比缩放，方向不变。"""
    step = max_qdot * DT
    delta = q_target - q_cmd
    worst = float(np.max(np.abs(delta)))
    if worst > step:
        delta = delta * (step / worst)
    return q_cmd + delta


def run_ticks(session: TrackingSession, clock: FakeClock, q: np.ndarray, root_schedule, t0: float = 0.0,
              max_ticks: int = 3000, until_finished: bool = True):
    """执行器模型：每拍 q ← 限速跟随上一拍 q_target；root_schedule(t) -> world_T_root。"""
    steps = []
    t = t0
    root = root_schedule(t)
    for _ in range(max_ticks):
        clock.advance(DT)
        t += DT
        root = root_schedule(t)
        step = session.step(q, np.zeros(7), root, state_age_ms=5.0)
        steps.append(step)
        q = executor_follow(q, step.q_target)
        if until_finished and step.finished:
            break
    return q, root, steps


def simulate(session: TrackingSession, clock: FakeClock, root_schedule, q0: np.ndarray, max_ticks: int = 3000):
    q = q0.copy()
    session.start(q, np.zeros(7), root_schedule(0.0))
    return run_ticks(session, clock, q, root_schedule, max_ticks=max_ticks)


class ReferenceBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.controller, _ = make_controller()
        self.fk = lambda q: self.controller.root_T_tcp_actual(q).homogeneous

    def test_path_plan_lifts_to_world_and_scales_time(self) -> None:
        q_list = [Q_START + (Q_GOAL - Q_START) * s for s in np.linspace(0, 1, 6)]
        root_ref = lean(0.1, dx=0.05)
        plan = build_reference_plan(q_list, 0.5, fk=self.fk, world_T_root_ref=root_ref, max_qdot_rad_s=0.4)
        # 0.5 s 走 0.5 rad → 1.0 rad/s，超过 0.4*0.9 → 必须拉长
        self.assertGreater(plan.time_scale, 1.0)
        self.assertLessEqual(plan.max_qdot_rad_s, 0.4 * 0.9 + 1e-9)
        self.assertAlmostEqual(plan.duration_s, 0.5 * plan.time_scale)
        # 终点 = world_T_root_ref @ fk(q_goal)
        np.testing.assert_allclose(plan.goal_world_T_tcp, root_ref @ self.fk(Q_GOAL), atol=1e-12)
        np.testing.assert_allclose(plan.goal_q, Q_GOAL)
        np.testing.assert_allclose(plan.sample_posture(plan.duration_s / 2), (Q_START + Q_GOAL) / 2, atol=1e-9)

    def test_slow_path_is_not_scaled(self) -> None:
        q_list = [Q_START, Q_START + 0.01]
        plan = build_reference_plan(q_list, 2.0, fk=self.fk, world_T_root_ref=np.eye(4), max_qdot_rad_s=0.4)
        self.assertEqual(plan.time_scale, 1.0)
        self.assertAlmostEqual(plan.duration_s, 2.0)

    def test_duplicate_waypoints_do_not_break_timestamps(self) -> None:
        plan = build_reference_plan([Q_START, Q_START, Q_GOAL, Q_GOAL], 1.0, fk=self.fk,
                                    world_T_root_ref=np.eye(4), max_qdot_rad_s=5.0)
        self.assertTrue(np.all(np.diff(plan.time_s) > 0))

    def test_recovery_plan_respects_velocity_ceiling(self) -> None:
        plan = build_recovery_plan(Q_START, Q_GOAL, fk=self.fk, world_T_root_ref=np.eye(4), max_qdot_rad_s=0.4)
        self.assertEqual(plan.kind, "RECOVERY_QUINTIC")
        self.assertLessEqual(plan.max_qdot_rad_s, 0.4 * 0.9 + 1e-9)
        np.testing.assert_allclose(plan.q_nominal[0], Q_START)
        np.testing.assert_allclose(plan.q_nominal[-1], Q_GOAL)
        # 数值检查五次多项式的实际峰值速度
        dq = np.max(np.abs(np.diff(plan.q_nominal, axis=0)), axis=1) / np.diff(plan.time_s)
        self.assertLessEqual(dq.max(), 0.4 * 0.9 + 0.02)

    def test_hold_plan(self) -> None:
        goal = self.fk(Q_GOAL)
        plan = build_hold_plan(goal, Q_GOAL, world_T_root_ref=np.eye(4), duration_s=1.0)
        np.testing.assert_allclose(plan.reference.sample(0.5), goal)
        self.assertEqual(plan.max_qdot_rad_s, 0.0)

    def test_rejects_bad_inputs(self) -> None:
        with self.assertRaises(ValueError):
            build_reference_plan([Q_START], 1.0, fk=self.fk, world_T_root_ref=np.eye(4), max_qdot_rad_s=0.4)
        with self.assertRaises(ValueError):
            build_reference_plan([Q_START, Q_GOAL], -1.0, fk=self.fk, world_T_root_ref=np.eye(4), max_qdot_rad_s=0.4)
        with self.assertRaises(ValueError):
            build_reference_plan([Q_START, Q_GOAL], 1.0, fk=self.fk, world_T_root_ref=np.eye(3), max_qdot_rad_s=0.4)


class TrackingSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.controller, config = make_controller()
        self.tracker_cfg = ApproachTrackerConfig.from_mapping(config["tracker"])
        self.fk = lambda q: self.controller.root_T_tcp_actual(q).homogeneous
        self.clock = FakeClock()
        self.q_list = [Q_START + (Q_GOAL - Q_START) * s for s in np.linspace(0, 1, 12)]

    def make_session(self, plan, **kw) -> TrackingSession:
        supervisor = FaultSupervisor("test", "right", recovery_timeout_s=5.0, clock=self.clock)
        return TrackingSession(self.controller, self.tracker_cfg, plan, supervisor=supervisor,
                               executor_max_qdot_rad_s=EXECUTOR_MAX_QDOT, hold_s=0.5, clock=self.clock, **kw)

    def test_world_goal_reached_when_torso_leans_mid_motion(self) -> None:
        root_ref = np.eye(4)
        plan = build_reference_plan(self.q_list, 2.0, fk=self.fk, world_T_root_ref=root_ref, max_qdot_rad_s=0.4)
        session = self.make_session(plan)
        goal_world = plan.goal_world_T_tcp

        # 1 s 后躯干开始后仰到 6°、后退 2 cm，之后保持
        def schedule(t):
            a = float(np.clip((t - 1.0) / 1.0, 0.0, 1.0))
            return lean(-np.deg2rad(6) * a, dx=-0.02 * a, dz=-0.01 * a)

        q_end, root_end, steps = simulate(session, self.clock, schedule, Q_START)
        self.assertTrue(steps[-1].finished)
        tcp_world = root_end @ self.fk(q_end)
        err = np.linalg.norm(tcp_world[:3, 3] - goal_world[:3, 3])
        self.assertLess(err, 0.003, f"world TCP error {err*1e3:.1f} mm")
        self.assertLess(session.summary.final_position_error_m, 0.003)
        # 关节终值必须与原目标不同——因为它补偿了躯干
        self.assertGreater(np.max(np.abs(q_end - Q_GOAL)), 0.02)
        self.assertEqual(session.summary.qp_failures, 0)
        self.assertLessEqual(session.summary.max_qdot_rad_s, self.controller.max_joint_velocity_rad_s + 1e-6)

    def test_without_world_compensation_the_same_lean_misses_goal(self) -> None:
        """对照组：把 world_T_root 恒定当单位阵（= legacy 在 torso 系里干活），躯干一动就偏。"""
        plan = build_reference_plan(self.q_list, 2.0, fk=self.fk, world_T_root_ref=np.eye(4), max_qdot_rad_s=0.4)
        session = self.make_session(plan)
        q_end, _, _ = simulate(session, self.clock, lambda t: np.eye(4), Q_START)
        true_root_end = lean(-np.deg2rad(6), dx=-0.03, dz=0.02)
        tcp_world_actual = true_root_end @ self.fk(q_end)
        err = np.linalg.norm(tcp_world_actual[:3, 3] - plan.goal_world_T_tcp[:3, 3])
        self.assertGreater(err, 0.01, f"expected a visible miss, got {err*1e3:.1f} mm")

    def test_plan_lifted_with_pick_time_root_lands_on_world_target(self) -> None:
        """取点时躯干已倾斜 root_ref；执行中回正。目标在世界系不变，最终应对准。"""
        root_ref = lean(-np.deg2rad(4), dx=-0.01)
        plan = build_reference_plan(self.q_list, 2.0, fk=self.fk, world_T_root_ref=root_ref, max_qdot_rad_s=0.4)
        session = self.make_session(plan)

        def schedule(t):
            a = float(np.clip(t / 1.5, 0.0, 1.0))
            return lean(-np.deg2rad(4) * (1 - a), dx=-0.01 * (1 - a))

        q_end, root_end, steps = simulate(session, self.clock, schedule, Q_START)
        tcp_world = root_end @ self.fk(q_end)
        self.assertLess(np.linalg.norm(tcp_world[:3, 3] - plan.goal_world_T_tcp[:3, 3]), 0.003)

    def test_qp_failures_hold_last_target_then_recover(self) -> None:
        plan = build_reference_plan(self.q_list, 2.0, fk=self.fk, world_T_root_ref=np.eye(4), max_qdot_rad_s=0.4)
        session = self.make_session(plan, max_consecutive_qp_failures=3)
        q = Q_START.copy()
        session.start(q, np.zeros(7), np.eye(4))
        last_target = q
        for _ in range(10):
            self.clock.advance(DT)
            last_target = session.step(q, np.zeros(7), np.eye(4), state_age_ms=5.0).q_target
            q = executor_follow(q, last_target)

        fail = {"n": 0}

        def failing_compute(*a, **k):
            fail["n"] += 1
            raise RuntimeError("QP solver failed")

        # 注入 3 次失败 → 进入 RECOVERING_HOLD，q_target 冻结
        with mock.patch.object(self.controller, "compute", side_effect=failing_compute):
            held = []
            for _ in range(3):
                self.clock.advance(DT)
                held.append(session.step(q, np.zeros(7), np.eye(4), state_age_ms=5.0))
        self.assertEqual(session.supervisor.state, SupervisorState.RECOVERING_HOLD)
        # 冻结的是最后一个有效 q_target，而不是实测 q
        self.assertTrue(all(np.allclose(s.q_target, last_target) for s in held))
        self.assertTrue(all("holding_last_valid_target" in s.warnings for s in held))
        self.assertGreaterEqual(session.summary.qp_failures, 3)

        # 下一拍：自动恢复（fresh → reset → 五次多项式重规划 → RUNNING）
        self.clock.advance(DT)
        step = session.step(q, np.zeros(7), np.eye(4), state_age_ms=5.0)
        self.assertEqual(session.supervisor.state, SupervisorState.RUNNING)
        self.assertEqual(step.plan_kind, "RECOVERY_QUINTIC")
        self.assertEqual(session.summary.recoveries, 1)

        # 继续跑到结束，仍然到达原世界系目标
        q_end, _, steps = self._run_to_end(session, q)
        tcp = self.fk(q_end)
        self.assertLess(np.linalg.norm(tcp[:3, 3] - plan.goal_world_T_tcp[:3, 3]), 0.003)

    def _run_to_end(self, session: TrackingSession, q: np.ndarray):
        return run_ticks(session, self.clock, q, lambda t: np.eye(4))

    def test_manual_hold_freezes_clock_and_resume_continues(self) -> None:
        plan = build_reference_plan(self.q_list, 2.0, fk=self.fk, world_T_root_ref=np.eye(4), max_qdot_rad_s=0.4)
        session = self.make_session(plan)
        q = Q_START.copy()
        session.start(q, np.zeros(7), np.eye(4))
        for _ in range(20):
            self.clock.advance(DT)
            q = executor_follow(q, session.step(q, np.zeros(7), np.eye(4), state_age_ms=5.0).q_target)
        t_before = session.elapsed_s()
        session.manual_hold()
        for _ in range(25):
            self.clock.advance(DT)
            step = session.step(q, np.zeros(7), np.eye(4), state_age_ms=5.0)
            self.assertEqual(step.phase, "PAUSED")
            np.testing.assert_allclose(step.q_target, q)
        self.assertAlmostEqual(session.elapsed_s(), t_before, places=9)
        session.manual_resume()
        self.clock.advance(DT)
        step = session.step(q, np.zeros(7), np.eye(4), state_age_ms=5.0)
        self.assertEqual(session.supervisor.state, SupervisorState.RUNNING)
        q_end, _, steps = self._run_to_end(session, executor_follow(q, step.q_target))
        self.assertTrue(steps[-1].finished)
        self.assertLess(np.linalg.norm(self.fk(q_end)[:3, 3] - plan.goal_world_T_tcp[:3, 3]), 0.003)

    def test_stale_state_triggers_hold(self) -> None:
        plan = build_reference_plan(self.q_list, 2.0, fk=self.fk, world_T_root_ref=np.eye(4), max_qdot_rad_s=0.4)
        session = self.make_session(plan)
        session.start(Q_START, np.zeros(7), np.eye(4))
        self.clock.advance(DT)
        step = session.step(Q_START, np.zeros(7), np.eye(4), state_age_ms=400.0)
        self.assertIn(step.phase, ("RECOVERING", "TRACK"))
        self.assertTrue(any(f["code"] == "STATE_AGE_TRANSIENT" for f in session.summary.faults))

    def test_step_before_start_is_rejected(self) -> None:
        plan = build_reference_plan(self.q_list, 2.0, fk=self.fk, world_T_root_ref=np.eye(4), max_qdot_rad_s=0.4)
        session = self.make_session(plan)
        with self.assertRaises(RuntimeError):
            session.step(Q_START, None, np.eye(4))


if __name__ == "__main__":
    unittest.main()
