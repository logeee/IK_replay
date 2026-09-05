"""control/approach_tracker.py：预瞄 + 躯干位姿外推的几何验证。

移植自 arm-motion-middleware v1.0.3 tests/unit/test_approach_tracker.py（unittest 化）。
"""

from __future__ import annotations

import unittest

import numpy as np
import pinocchio as pin

from control.approach_tracker import ApproachTracker, ApproachTrackerConfig
from control.interfaces import ArmState
from control.trajectory_reference import WorldTCPReferenceTrajectory


class RecordingController:
    def __init__(self) -> None:
        self.target = None

    def reset(self, q_actual: np.ndarray) -> None:
        self.reset_q = np.asarray(q_actual).copy()

    def compute(self, q_actual, dq_actual, T_root_tcp_desired, dt):
        self.target = np.asarray(T_root_tcp_desired).copy()
        return np.zeros(7), np.asarray(q_actual).copy(), {"success": True}


def reference() -> WorldTCPReferenceTrajectory:
    poses = np.repeat(np.eye(4)[None, :, :], 2, axis=0)
    poses[0, :3, 3] = [0.2, 0.1, 1.0]
    poses[1, :3, 3] = [0.4, 0.1, 1.0]
    return WorldTCPReferenceTrajectory(np.array([0.0, 1.0]), poses)


class ApproachTrackerTest(unittest.TestCase):
    def test_preview_and_live_predicted_root_are_applied_only_to_control_target(self) -> None:
        controller = RecordingController()
        tracker = ApproachTracker(
            controller,
            reference(),
            ApproachTrackerConfig(trajectory_preview_s=0.1, root_prediction_s=0.1),
        )
        q = np.zeros(7)
        root_0 = np.eye(4)
        root_1 = pin.SE3(np.eye(3), np.array([0.01, 0.0, 0.0])).homogeneous
        tracker.reset(q, 0.0, root_0)
        tracker.observe_root(0.1, root_1)
        state = ArmState(q, q, root_1, np.eye(4))
        output = tracker.compute(0.1, state, 1.0 / 30.0)

        np.testing.assert_allclose(output.true_reference_world_T_tcp[:3, 3], [0.22, 0.1, 1.0])
        np.testing.assert_allclose(output.control_reference_world_T_tcp[:3, 3], [0.24, 0.1, 1.0])
        np.testing.assert_allclose(output.root_for_control_world_T_root[:3, 3], [0.02, 0.0, 0.0])
        expected = (
            np.linalg.inv(output.root_for_control_world_T_root)
            @ output.control_reference_world_T_tcp
        )
        np.testing.assert_allclose(controller.target, expected)

    def test_zero_preview_uses_current_world_reference_and_live_root(self) -> None:
        controller = RecordingController()
        tracker = ApproachTracker(
            controller,
            reference(),
            ApproachTrackerConfig(trajectory_preview_s=0.0, root_prediction_s=0.0),
        )
        q = np.zeros(7)
        root = pin.SE3(
            pin.rpy.rpyToMatrix(0.1, -0.2, 0.3), np.array([0.1, 0.2, 0.3])
        ).homogeneous
        tracker.reset(q, 0.25, root)
        output = tracker.compute(0.25, ArmState(q, q, root, np.eye(4)), 1.0 / 30.0)
        np.testing.assert_allclose(
            output.control_reference_world_T_tcp, output.true_reference_world_T_tcp
        )
        np.testing.assert_allclose(output.root_for_control_world_T_root, root)
        np.testing.assert_allclose(
            controller.target, np.linalg.inv(root) @ output.true_reference_world_T_tcp
        )
        np.testing.assert_allclose(output.feedforward_task_twist_tcp, 0.0, atol=1.0e-12)
        np.testing.assert_allclose(
            output.total_task_twist_tcp, output.feedback_task_twist_tcp, atol=1.0e-12
        )

    def test_predicted_root_motion_is_decomposed_as_task_feedforward(self) -> None:
        controller = RecordingController()
        tracker = ApproachTracker(
            controller,
            reference(),
            ApproachTrackerConfig(
                trajectory_preview_s=0.0,
                root_prediction_s=0.1,
                twist_filter_enabled=False,
            ),
        )
        q = np.zeros(7)
        root_0 = np.eye(4)
        root_1 = pin.SE3(np.eye(3), np.array([0.01, 0.0, 0.0])).homogeneous
        tracker.reset(q, 0.0, root_0)
        tracker.observe_root(0.1, root_1)
        actual_tcp = reference().sample(0.1)
        output = tracker.compute(0.1, ArmState(q, q, root_1, actual_tcp), 0.1)
        self.assertLess(output.feedforward_task_twist_tcp[0], 0.0)
        np.testing.assert_allclose(
            output.total_task_twist_tcp,
            output.feedback_task_twist_tcp + output.feedforward_task_twist_tcp,
            atol=1.0e-12,
        )

    def test_torso_lean_is_compensated_in_root_frame_target(self) -> None:
        """核心诉求：世界系目标不动，躯干后仰后，根系下的控制目标应随之改变。"""
        controller = RecordingController()
        tracker = ApproachTracker(
            controller,
            reference(),
            ApproachTrackerConfig(trajectory_preview_s=0.0, root_prediction_s=0.0),
        )
        q = np.zeros(7)
        upright = np.eye(4)
        tracker.reset(q, 0.0, upright)
        tracker.compute(0.0, ArmState(q, q, upright, np.eye(4)), 0.02)
        target_upright = controller.target.copy()

        leaned = pin.SE3(pin.rpy.rpyToMatrix(0.0, -0.1, 0.0), np.zeros(3)).homogeneous  # 后仰 ~5.7°
        tracker.observe_root(0.02, leaned)
        output = tracker.compute(0.02, ArmState(q, q, leaned, np.eye(4)), 0.02)
        target_leaned = controller.target.copy()

        # 世界系参考在 t≈0 处几乎不变
        np.testing.assert_allclose(
            output.true_reference_world_T_tcp[:3, 3], [0.204, 0.1, 1.0], atol=1e-9
        )
        # 根系目标必须变化，并且等于 inv(world_T_root) @ world_T_ref
        self.assertFalse(np.allclose(target_upright, target_leaned))
        np.testing.assert_allclose(
            target_leaned, np.linalg.inv(leaned) @ output.true_reference_world_T_tcp
        )

    def test_nested_filtered_root_config_is_parsed(self) -> None:
        config = ApproachTrackerConfig.from_mapping(
            {
                "trajectory_preview_s": 0.0667,
                "predictor_history": 6,
                "root_prediction": {
                    "mode": "FILTERED_PREDICTION",
                    "horizon_s": 0.05,
                    "twist_filter": {"enabled": True, "time_constant_s": 0.12},
                    "clamp": {"linear_velocity_m_s": 0.4, "angular_velocity_rad_s": 1.2},
                },
            }
        )
        self.assertEqual(config.root_prediction_mode, "FILTERED_PREDICTION")
        self.assertEqual(config.root_prediction_s, 0.05)
        self.assertEqual(config.predictor_history, 6)
        self.assertEqual(config.twist_filter_time_constant_s, 0.12)
        self.assertEqual(config.root_linear_velocity_clamp_m_s, 0.4)
        self.assertEqual(config.root_angular_velocity_clamp_rad_s, 1.2)


if __name__ == "__main__":
    unittest.main()
