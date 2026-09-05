"""control/floating_base.py：支撑腿运动学里程计的几何不变量（无真机）。

验证要点：
* 站直、IMU 单位姿态：骨盆位置 = 配置初始高度，world_T_root 平移只差腰以上 FK；
* 躯干后仰（IMU 变化、腿不动）：双脚锚点保持不动，骨盆位置随之改变；
* 脚锚点误差在双脚站定时应≈0（quality GOOD）；
* 重新锚定后 yaw 归零、计数递增。
"""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from control.floating_base import (
    FloatingBaseConfig,
    FloatingBaseInput,
    SupportKinematicFloatingBase,
    quaternion_wxyz_to_rotation,
    rotation_to_quaternion_wxyz,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config/robots/h2_floating_base.yaml"


def rpy_quaternion_wxyz(roll: float, pitch: float, yaw: float) -> np.ndarray:
    cr, sr = np.cos(roll / 2), np.sin(roll / 2)
    cp, sp = np.cos(pitch / 2), np.sin(pitch / 2)
    cy, sy = np.cos(yaw / 2), np.sin(yaw / 2)
    return np.array(
        [
            cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
        ]
    )


def sample(t: float, joint_q: dict, quat: np.ndarray, seq: int | None = None) -> FloatingBaseInput:
    return FloatingBaseInput(t, joint_q, quat, np.zeros(3), sequence=seq)


class FloatingBaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = FloatingBaseConfig.from_yaml(CONFIG, project_root=PROJECT_ROOT)

    def test_config_maps_all_31_motors(self) -> None:
        self.assertEqual(len(self.config.motor_index_to_joint), 31)
        joints = self.config.map_motor_q({12: 0.1, 13: -0.2, 25: 0.9})
        self.assertAlmostEqual(joints["waist_yaw_joint"], 0.1)
        self.assertAlmostEqual(joints["waist_roll_joint"], -0.2)
        self.assertAlmostEqual(joints["right_elbow_joint"], 0.9)

    def test_upright_neutral_pose_anchors_at_initial_height(self) -> None:
        estimator = SupportKinematicFloatingBase(self.config)
        state = estimator.anchor(sample(0.0, {}, np.array([1.0, 0, 0, 0]), seq=1))
        np.testing.assert_allclose(
            state.position_world, self.config.initial_pelvis_position_world_m, atol=1e-12
        )
        np.testing.assert_allclose(state.quaternion_world_wxyz, [1, 0, 0, 0], atol=1e-12)
        self.assertEqual(state.quality, "DOUBLE_SUPPORT_GOOD")
        self.assertAlmostEqual(state.left_anchor_error_m, 0.0)
        self.assertTrue(estimator.anchored)
        # 躯干在骨盆上方：world_T_root 的 z 高于骨盆
        self.assertGreater(state.world_T_root[2, 3], state.position_world[2])
        # 中性位下 torso 与 pelvis 姿态一致（腰关节为 0）
        np.testing.assert_allclose(state.world_T_root[:3, :3], np.eye(3), atol=1e-12)

    def test_torso_lean_moves_pelvis_but_keeps_feet_anchored(self) -> None:
        estimator = SupportKinematicFloatingBase(self.config)
        upright = estimator.anchor(sample(0.0, {}, np.array([1.0, 0, 0, 0]), seq=1))
        left_anchor = upright.left_foot_position_world.copy()
        right_anchor = upright.right_foot_position_world.copy()

        # 整体绕 y 后仰 5°，腿关节不动（模拟本体控制器整体倾斜）
        leaned = estimator.update(sample(0.02, {}, rpy_quaternion_wxyz(0.0, -np.deg2rad(5), 0.0), seq=2))
        # 脚必须还在锚点上（这是估计器的定义）
        np.testing.assert_allclose(leaned.left_foot_position_world, left_anchor, atol=1e-9)
        np.testing.assert_allclose(leaned.right_foot_position_world, right_anchor, atol=1e-9)
        self.assertEqual(leaned.quality, "DOUBLE_SUPPORT_GOOD")
        # 骨盆位置应改变（绕脚旋转），且 torso 姿态跟随 IMU
        self.assertGreater(np.linalg.norm(leaned.position_world - upright.position_world), 0.01)
        expected_R = quaternion_wxyz_to_rotation(rpy_quaternion_wxyz(0.0, -np.deg2rad(5), 0.0))
        np.testing.assert_allclose(leaned.world_T_root[:3, :3], expected_R, atol=1e-9)
        self.assertAlmostEqual(leaned.base_pose_jump_rad, np.deg2rad(5), places=9)

    def test_waist_pitch_changes_root_but_not_pelvis(self) -> None:
        estimator = SupportKinematicFloatingBase(self.config)
        neutral = estimator.anchor(sample(0.0, {}, np.array([1.0, 0, 0, 0]), seq=1))
        bent = estimator.update(
            sample(0.02, {"waist_pitch_joint": 0.3}, np.array([1.0, 0, 0, 0]), seq=2)
        )
        np.testing.assert_allclose(bent.position_world, neutral.position_world, atol=1e-12)
        self.assertFalse(np.allclose(bent.world_T_root, neutral.world_T_root))

    def test_startup_yaw_is_zeroed_and_reanchor_resets(self) -> None:
        estimator = SupportKinematicFloatingBase(self.config)
        yawed = rpy_quaternion_wxyz(0.0, 0.0, np.deg2rad(40))
        first = estimator.anchor(sample(0.0, {}, yawed, seq=1))
        np.testing.assert_allclose(first.quaternion_world_wxyz, [1, 0, 0, 0], atol=1e-12)
        self.assertEqual(estimator.anchor_count, 1)

        # 再转 10°，应表现为 +10° yaw
        more = estimator.update(sample(0.02, {}, rpy_quaternion_wxyz(0, 0, np.deg2rad(50)), seq=2))
        R = quaternion_wxyz_to_rotation(more.quaternion_world_wxyz)
        self.assertAlmostEqual(np.arctan2(R[1, 0], R[0, 0]), np.deg2rad(10), places=9)

        # 重新锚定：yaw 再次归零、计数递增
        again = estimator.anchor(sample(1.0, {}, rpy_quaternion_wxyz(0, 0, np.deg2rad(50)), seq=3))
        np.testing.assert_allclose(again.quaternion_world_wxyz, [1, 0, 0, 0], atol=1e-12)
        self.assertEqual(estimator.anchor_count, 2)
        self.assertIn("ANCHOR_2", again.active_anchor)

    def test_same_sequence_returns_cached_state(self) -> None:
        estimator = SupportKinematicFloatingBase(self.config)
        a = estimator.anchor(sample(0.0, {}, np.array([1.0, 0, 0, 0]), seq=7))
        b = estimator.update(sample(0.5, {}, rpy_quaternion_wxyz(0, 0, 0.5), seq=7))
        self.assertIs(a, b)

    def test_quaternion_roundtrip(self) -> None:
        q = rpy_quaternion_wxyz(0.3, -0.2, 1.1)
        np.testing.assert_allclose(
            rotation_to_quaternion_wxyz(quaternion_wxyz_to_rotation(q)), q, atol=1e-12
        )

    def test_rejects_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            FloatingBaseInput(0.0, {}, np.zeros(4), np.zeros(3))
        with self.assertRaises(ValueError):
            FloatingBaseInput(float("nan"), {}, np.array([1.0, 0, 0, 0]), np.zeros(3))


if __name__ == "__main__":
    unittest.main()
