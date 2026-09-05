"""control/pink_arm_controller.py 的离线测试（无真机，需要 pinocchio + pin-pink）。

移植自 arm-motion-middleware v1.0.3 tests/unit/test_pink_arm_controller.py，
改为 unittest；TCP 不再来自 tool.yml，而是像运行时一样由代码构造。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import yaml

from control.pink_arm_controller import PinkArmController
from control.tool_config import ToolConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_CONFIG = PROJECT_ROOT / "config/robots/h2_pink_left.yaml"
Q_HOME = np.array([0.0, 0.25, 0.0, 0.8, 0.0, 0.0, 0.0])


def make_controller(tcp_xyz=(0.08, 0.0, 0.0)) -> PinkArmController:
    config = yaml.safe_load(CONTROLLER_CONFIG.read_text(encoding="utf-8"))
    wrist_T_tcp = np.eye(4)
    wrist_T_tcp[:3, 3] = tcp_xyz
    tool = ToolConfig("left_tcp", config["model"]["wrist_frame"], wrist_T_tcp)
    return PinkArmController(
        PROJECT_ROOT / config["model"]["urdf_path"], config, tool
    )


class PinkArmControllerTest(unittest.TestCase):
    def test_controller_tracks_small_tcp_step(self) -> None:
        controller = make_controller()
        q = Q_HOME.copy()
        controller.reset(q)
        target = controller.root_T_tcp_actual(q)
        target.translation[2] += 0.01
        qdot, q_target, diagnostics = controller.compute(q, np.zeros(7), target, 1.0 / 30.0)
        self.assertTrue(diagnostics.qp_success)
        self.assertEqual(qdot.shape, (7,))
        self.assertEqual(q_target.shape, (7,))
        self.assertTrue(np.all(np.isfinite(q_target)))
        ceiling = controller.max_joint_velocity_rad_s
        self.assertLessEqual(np.max(np.abs(q_target - q)), ceiling / 30.0 + 1.0e-9)

    def test_tcp_offset_comes_from_runtime_tool_config(self) -> None:
        controller = make_controller(tcp_xyz=(0.123, -0.01, 0.02))
        np.testing.assert_allclose(controller.wrist_T_tcp.translation, [0.123, -0.01, 0.02])
        self.assertEqual(controller.tool_config.parent_link, "left_wrist_yaw_link")
        # TCP frame 确实被加进模型并随偏移平移
        q = Q_HOME.copy()
        controller.reset(q)
        tcp = controller.root_T_tcp_actual(q)
        wrist = controller.configuration.get_transform_frame_to_world("left_wrist_yaw_link")
        expected = wrist * controller.wrist_T_tcp
        np.testing.assert_allclose(tcp.homogeneous, expected.homogeneous, atol=1e-12)

    def test_tcp_parent_must_match_wrist_frame(self) -> None:
        config = yaml.safe_load(CONTROLLER_CONFIG.read_text(encoding="utf-8"))
        tool = ToolConfig("bad", "left_elbow_link", np.eye(4))
        with self.assertRaisesRegex(RuntimeError, "must match wrist frame"):
            PinkArmController(PROJECT_ROOT / config["model"]["urdf_path"], config, tool)

    def test_phase_local_posture_reference_and_velocity_ceiling(self) -> None:
        controller = make_controller()
        q = Q_HOME.copy()
        controller.reset(q)
        controller.set_posture_reference(q + 0.01)
        controller.set_joint_velocity_ceiling(0.4)
        self.assertAlmostEqual(controller.max_joint_velocity_rad_s, 0.4)
        self.assertLessEqual(np.max(controller.model.velocityLimit), 0.4 + 1.0e-12)
        target = controller.root_T_tcp_actual(q)
        target.translation[0] += 0.05  # 足够大的一步，逼近速度上限
        qdot, q_target, diagnostics = controller.compute(q, np.zeros(7), target, 0.02)
        self.assertTrue(diagnostics.qp_success)
        self.assertLessEqual(np.max(np.abs(qdot)), 0.4 + 1.0e-9)
        self.assertTrue(np.all(np.isfinite(q_target)))

    def test_phase_local_controller_overrides_reject_invalid_values(self) -> None:
        controller = make_controller()
        with self.assertRaisesRegex(ValueError, "q_reference"):
            controller.set_posture_reference(np.zeros(6))
        with self.assertRaisesRegex(ValueError, "positive and finite"):
            controller.set_joint_velocity_ceiling(0.0)
        with self.assertRaisesRegex(ValueError, "finite and non-negative"):
            controller.set_qdot_continuity_regularization(-0.1)

    def test_joint_limit_policy_accepts_only_strict_or_warn(self) -> None:
        self.assertEqual(PinkArmController._normalize_joint_limit_policy("warn"), "WARN")
        self.assertEqual(PinkArmController._normalize_joint_limit_policy("STRICT"), "STRICT")
        with self.assertRaisesRegex(ValueError, "joint_limit_policy"):
            PinkArmController._normalize_joint_limit_policy("ignore")

    def test_execution_position_limits_are_intersected_with_urdf_limits(self) -> None:
        controller = object.__new__(PinkArmController)
        controller.model = SimpleNamespace(
            nq=2,
            lowerPositionLimit=np.array([-1.0, -2.0]),
            upperPositionLimit=np.array([1.0, 2.0]),
        )
        controller._urdf_lower_position_limit = np.array([-1.0, -2.0])
        controller._urdf_upper_position_limit = np.array([1.0, 2.0])

        controller.set_position_limits(np.array([-0.9, -1.9]), np.array([0.9, 1.9]))
        np.testing.assert_allclose(controller.model.lowerPositionLimit, [-0.9, -1.9])
        np.testing.assert_allclose(controller.model.upperPositionLimit, [0.9, 1.9])

        controller.set_position_limits(np.array([-1.01, -1.9]), np.array([0.9, 2.1]))
        np.testing.assert_allclose(controller.model.lowerPositionLimit, [-1.0, -1.9])
        np.testing.assert_allclose(controller.model.upperPositionLimit, [0.9, 2.0])

    def test_warn_policy_turns_pink_limit_exception_into_diagnostic(self) -> None:
        controller = object.__new__(PinkArmController)
        controller._is_reset = True
        controller.joint_limit_policy = "WARN"
        controller.model = SimpleNamespace(
            nq=2,
            nv=2,
            lowerPositionLimit=np.array([0.0, -1.0]),
            upperPositionLimit=np.array([1.0, 1.0]),
        )
        controller.configuration = SimpleNamespace(
            update=lambda _q: None,
            integrate=lambda qdot, _dt: np.array([1.2, 0.0]) + qdot * 0.0,
        )
        controller.frame_task = SimpleNamespace(
            set_target=lambda _target: None,
            compute_error=lambda _configuration: np.zeros(6),
        )
        controller.low_acceleration_task = None
        controller.tasks = []
        controller._previous_qdot = np.zeros(2)
        controller.qp_solver = "fake"
        controller.pink_cfg = {"qp_damping": 1.0e-8}
        controller._max_joint_velocity = 1.5

        solver_kwargs: dict = {}

        def fail_with_limit(*_args, **kwargs):
            solver_kwargs.update(kwargs)
            raise RuntimeError("joint position limit reached")

        with mock.patch("control.pink_arm_controller.pink.solve_ik", fail_with_limit):
            qdot, q_target, diagnostics = controller.compute(
                np.array([0.2, 0.0]), np.zeros(2), np.eye(4), 0.02
            )

        np.testing.assert_array_equal(qdot, np.zeros(2))
        np.testing.assert_array_equal(q_target, np.array([0.2, 0.0]))
        self.assertFalse(diagnostics.qp_success)
        self.assertTrue(diagnostics.joint_limit_warning)
        self.assertEqual(diagnostics.joint_limit_warning_joints, ())
        self.assertTrue(diagnostics.message.startswith("joint-limit warning:"))
        self.assertIs(solver_kwargs["safety_break"], False)

    def test_qdot_continuity_regularization_is_secondary_and_resets_history(self) -> None:
        controller = make_controller()
        original_tasks = tuple(controller.tasks)
        controller.set_qdot_continuity_regularization(0.002)
        self.assertIsNotNone(controller.low_acceleration_task)
        self.assertIs(controller.frame_task, controller.tasks[0])
        self.assertIs(controller.posture_task, controller.tasks[1])
        self.assertIs(controller.low_acceleration_task, controller.tasks[2])
        self.assertIs(controller.damping_task, controller.tasks[3])
        q = Q_HOME.copy()
        controller.reset(q)
        target = controller.root_T_tcp_actual(q)
        qdot, _, diagnostics = controller.compute(q, np.zeros(7), target, 0.02)
        self.assertTrue(diagnostics.qp_success)
        np.testing.assert_allclose(controller._previous_qdot, qdot)
        controller.reset(q)
        np.testing.assert_array_equal(controller._previous_qdot, np.zeros(7))
        controller.set_qdot_continuity_regularization(0.0)
        self.assertIsNone(controller.low_acceleration_task)
        self.assertEqual(tuple(controller.tasks), original_tasks)

    def test_right_arm_config_loads_and_solves(self) -> None:
        config = yaml.safe_load(
            (PROJECT_ROOT / "config/robots/h2_pink_right.yaml").read_text(encoding="utf-8")
        )
        tool = ToolConfig("right_tcp", "right_wrist_yaw_link", np.eye(4))
        controller = PinkArmController(PROJECT_ROOT / config["model"]["urdf_path"], config, tool)
        q = np.array([0.2, -0.25, 0.0, 0.9, 0.0, -0.1, 0.0])
        controller.reset(q)
        target = controller.root_T_tcp_actual(q)
        target.translation[2] += 0.01
        _, _, diagnostics = controller.compute(q, np.zeros(7), target, 0.02)
        self.assertTrue(diagnostics.qp_success)
        self.assertLess(diagnostics.solve_time_s, 0.02)


if __name__ == "__main__":
    unittest.main()
