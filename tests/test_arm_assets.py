"""core/arm_assets：左右臂归属（arm 字段 + R-/L- 名字前缀）的规则。"""

from __future__ import annotations

import unittest

from core import arm_assets as aa


class NamingTests(unittest.TestCase):
    def test_prefix_per_arm(self):
        self.assertEqual(aa.arm_prefix("right_arm"), "R-")
        self.assertEqual(aa.arm_prefix("left_arm"), "L-")
        with self.assertRaises(ValueError):
            aa.arm_prefix("both_arms")

    def test_prefixed_name_adds_once_and_keeps_same_arm_prefix(self):
        self.assertEqual(aa.prefixed_name("right_arm", "起手点测试"), "R-起手点测试")
        self.assertEqual(aa.prefixed_name("right_arm", "R-起手点测试"), "R-起手点测试")
        self.assertEqual(aa.prefixed_name("left_arm", " 录制点位1 "), "L-录制点位1")

    def test_prefixed_name_rejects_foreign_prefix(self):
        with self.assertRaises(aa.ArmMismatch):
            aa.prefixed_name("left_arm", "R-起手点测试")

    def test_arm_from_name_and_strip(self):
        self.assertEqual(aa.arm_from_name("R-0.43-左-起手式"), "right_arm")
        self.assertEqual(aa.arm_from_name("L-x"), "left_arm")
        self.assertIsNone(aa.arm_from_name("0.43-左-起手式"))   # 「左」不是前缀
        self.assertIsNone(aa.arm_from_name("r-x"))             # 必须大写
        self.assertEqual(aa.strip_arm_prefix("R-0.43-起手式新"), "0.43-起手式新")
        self.assertEqual(aa.strip_arm_prefix("0.43-起手式新"), "0.43-起手式新")

    def test_arm_asset_name(self):
        self.assertEqual(aa.arm_asset_name("left_arm", "起手点测试"), "L-起手点测试")


class OwnershipTests(unittest.TestCase):
    def test_asset_arm_requires_valid_field(self):
        self.assertEqual(aa.asset_arm({"arm": "right_arm", "name": "R-a"}), "right_arm")
        self.assertIsNone(aa.asset_arm({"name": "R-a"}))            # 缺 arm
        self.assertIsNone(aa.asset_arm({"arm": "arm", "name": "a"}))  # 非法取值
        self.assertIsNone(aa.asset_arm({"arm": "right_arm", "name": "L-a"}))  # 矛盾
        self.assertIsNone(aa.asset_arm("not a dict"))

    def test_belongs_to_is_strict(self):
        right = {"arm": "right_arm", "name": "R-a"}
        self.assertTrue(aa.belongs_to(right, "right_arm"))
        self.assertFalse(aa.belongs_to(right, "left_arm"))
        self.assertFalse(aa.belongs_to({"name": "a"}, "right_arm"))
        self.assertFalse(aa.belongs_to(right, "nonsense"))

    def test_filter_by_arm_drops_unmarked_and_foreign(self):
        items = [
            {"arm": "right_arm", "name": "R-1"},
            {"arm": "left_arm", "name": "L-2"},
            {"name": "3"},
        ]
        self.assertEqual([i["name"] for i in aa.filter_by_arm(items, "right_arm")],
                         ["R-1"])
        self.assertEqual([i["name"] for i in aa.filter_by_arm(items, "left_arm")],
                         ["L-2"])

    def test_check_asset_arm_messages(self):
        with self.assertRaisesRegex(aa.ArmMismatch, "没有 arm 归属字段"):
            aa.check_asset_arm({"name": "x"}, "right_arm", "位点")
        with self.assertRaisesRegex(aa.ArmMismatch, "属于左臂"):
            aa.check_asset_arm({"arm": "left_arm", "name": "L-x"}, "right_arm", "位点")
        with self.assertRaisesRegex(aa.ArmMismatch, "矛盾"):
            aa.check_asset_arm({"arm": "right_arm", "name": "L-x"}, "right_arm")
        self.assertEqual(
            aa.check_asset_arm({"arm": "right_arm", "name": "R-x"}, "right_arm"),
            "right_arm")

    def test_check_asset_arm_cross_checks_joint_names(self):
        # 即便 arm 字段被手改成 right_arm，左臂关节名照样拒绝
        forged = {
            "arm": "right_arm", "name": "R-x",
            "named_joints": {"left_shoulder_pitch_joint": 0.0},
        }
        with self.assertRaisesRegex(aa.ArmMismatch, "关节名是左臂"):
            aa.check_asset_arm(forged, "right_arm")
        sequence = {
            "arm": "right_arm", "name": "R-seq",
            "trajectory": {"joint_names": ["left_elbow_joint"]},
        }
        with self.assertRaises(aa.ArmMismatch):
            aa.check_asset_arm(sequence, "right_arm")
        sidestep = {
            "arm": "left_arm", "name": "L-sidestep_L6cm",
            "waypoints": [{"named_joints": {"right_elbow_joint": 0.0}}],
        }
        with self.assertRaises(aa.ArmMismatch):
            aa.check_asset_arm(sidestep, "left_arm")
        # 通用关节名（测试夹具 j1/j2）不参与交叉校验
        aa.check_asset_arm({"arm": "right_arm", "named_joints": {"j1": 0.0}},
                           "right_arm")

    def test_joints_arm(self):
        self.assertEqual(aa.joints_arm(["right_a", "right_b"]), "right_arm")
        self.assertIsNone(aa.joints_arm(["right_a", "left_b"]))
        self.assertIsNone(aa.joints_arm(["j1"]))
        self.assertIsNone(aa.joints_arm([]))

    def test_stamp_writes_arm_and_prefix(self):
        item = aa.stamp({"name": "完全捏住", "positions": []}, "right_arm")
        self.assertEqual(item["arm"], "right_arm")
        self.assertEqual(item["name"], "R-完全捏住")
        with self.assertRaises(ValueError):
            aa.stamp({"name": "x"}, "")


if __name__ == "__main__":
    unittest.main()
