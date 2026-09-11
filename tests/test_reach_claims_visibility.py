"""18001 列表接口的认领可见性 + 左右臂归属过滤。

默认只回「激活组合已启用能力认领的」+「本组合自己录的」（来源戳豁免，
新录的不能刚存完就消失）；?scope=all 放开认领过滤；无组合（camera-only）
不做认领过滤。臂归属（core/arm_assets.py）是另一道关：异臂 / 无 arm 标记
的文件任何 scope 都不显示。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.reach import recordings
from adapters.reach.state import state


class _StateSandbox(unittest.TestCase):
    """备份/还原全局 state 的相关字段，避免污染其他用例。"""

    _FIELDS = ("sequences_dir", "waypoints_dir", "active_combo",
               "visible_sequences", "visible_waypoints",
               "joint_names", "chain_id")

    def setUp(self):
        self._backup = {f: getattr(state, f) for f in self._FIELDS}
        self.addCleanup(self._restore)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        state.sequences_dir = root / "sequences"
        state.waypoints_dir = root / "waypoints"
        state.sequences_dir.mkdir()
        state.waypoints_dir.mkdir()
        state.chain_id = "right_arm"
        state.active_combo = {"arm": "right_arm",
                              "hand_id": "qiangnao-1-right"}

    def _restore(self):
        for field, value in self._backup.items():
            setattr(state, field, value)

    @staticmethod
    def _write(directory: Path, filename: str, payload: dict):
        (directory / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class SequenceVisibilityTests(_StateSandbox):
    def setUp(self):
        super().setUp()
        state.visible_sequences = {"R-0.50-扭-起手式"}
        self._write(state.sequences_dir, "R-a_20260101_000000.json",
                    {"name": "R-0.50-扭-起手式",
                     "arm": "right_arm"})                  # 已认领 → 可见
        self._write(state.sequences_dir, "R-b_20260101_000000.json",
                    {"name": "R-0.50-起手式新",
                     "arm": "right_arm"})                  # 别家的 → 隐藏
        self._write(state.sequences_dir, "R-c_20260101_000000.json", {
            "name": "R-新录未认领",                        # 自己录的 → 可见
            "arm": "right_arm",
            "recorded_combo": {"arm": "right_arm",
                               "hand_id": "qiangnao-1-right"},
        })

    def test_default_filters_to_claims_plus_own_recordings(self):
        res = recordings.reach_sequences()
        self.assertEqual({s["name"] for s in res["sequences"]},
                         {"R-0.50-扭-起手式", "R-新录未认领"})
        self.assertEqual(res["hidden"], 1)
        self.assertTrue(res["filtered"])
        self.assertEqual(res["arm"], "right_arm")
        self.assertEqual(res["arm_hidden"],
                         {"foreign_arm": 0, "unmarked": 0})

    def test_scope_all_returns_whole_own_arm_pool(self):
        res = recordings.reach_sequences(scope="all")
        self.assertEqual(len(res["sequences"]), 3)
        self.assertEqual(res["hidden"], 0)

    def test_no_combo_context_skips_filtering(self):
        state.visible_sequences = None
        res = recordings.reach_sequences()
        self.assertEqual(len(res["sequences"]), 3)
        self.assertFalse(res["filtered"])

    def test_other_arm_and_unmarked_hidden_even_with_scope_all(self):
        # 左臂的（即便被认领 / 自己组合录的）→ 任何 scope 都不显示
        state.visible_sequences = {"R-0.50-扭-起手式", "L-0.50-扭-起手式"}
        self._write(state.sequences_dir, "L-d_20260101_000000.json", {
            "name": "L-0.50-扭-起手式", "arm": "left_arm",
            "recorded_combo": {"arm": "right_arm",
                               "hand_id": "qiangnao-1-right"},
        })
        # 无 arm 字段的旧文件 → 不显示
        self._write(state.sequences_dir, "e_20260101_000000.json",
                    {"name": "旧序列",
                     "recorded_combo": {"arm": "right_arm",
                                        "hand_id": "qiangnao-1-right"}})
        # arm 与名字前缀矛盾 → 视为无归属，不显示
        self._write(state.sequences_dir, "f_20260101_000000.json",
                    {"name": "L-矛盾", "arm": "right_arm"})
        for scope in ("", "all"):
            res = recordings.reach_sequences(scope=scope)
            names = {s["name"] for s in res["sequences"]}
            self.assertNotIn("L-0.50-扭-起手式", names)
            self.assertNotIn("旧序列", names)
            self.assertNotIn("L-矛盾", names)
            self.assertEqual(res["total"], 3)
            self.assertEqual(res["arm_hidden"],
                             {"foreign_arm": 1, "unmarked": 2})
        state.visible_sequences = None
        res = recordings.reach_sequences()
        self.assertEqual(len(res["sequences"]), 3)


class WaypointVisibilityTests(_StateSandbox):
    def setUp(self):
        super().setUp()
        state.visible_waypoints = {"R-0.50-扭-终点"}
        self._write(state.waypoints_dir, "R-a_20260101_000000.json",
                    {"name": "R-0.50-扭-终点",
                     "arm": "right_arm"})                  # 生效位点 → 可见
        self._write(state.waypoints_dir, "R-b_20260101_000000.json",
                    {"name": "R-0.50-起手式新终点",
                     "arm": "right_arm"})                  # 别家的 → 隐藏
        self._write(state.waypoints_dir, "R-c_20260101_000000.json", {
            "name": "R-新录位点",                          # 自己录的 → 可见
            "arm": "right_arm",
            "recorded_combo": {"arm": "right_arm",
                               "hand_id": "qiangnao-1-right"},
        })

    def test_default_filters_to_claims_plus_own_recordings(self):
        res = recordings.reach_waypoints()
        self.assertEqual({w["name"] for w in res["waypoints"]},
                         {"R-0.50-扭-终点", "R-新录位点"})
        self.assertEqual(res["hidden"], 1)
        self.assertTrue(res["filtered"])
        self.assertEqual(res["arm"], "right_arm")

    def test_scope_all_returns_whole_own_arm_pool(self):
        res = recordings.reach_waypoints(scope="all")
        self.assertEqual(len(res["waypoints"]), 3)

    def test_other_arm_and_unmarked_hidden_even_with_scope_all(self):
        self._write(state.waypoints_dir, "L-d_20260101_000000.json",
                    {"name": "L-左臂位点", "arm": "left_arm"})
        self._write(state.waypoints_dir, "e_20260101_000000.json",
                    {"name": "旧位点"})
        for scope in ("", "all"):
            res = recordings.reach_waypoints(scope=scope)
            names = {w["name"] for w in res["waypoints"]}
            self.assertNotIn("L-左臂位点", names)
            self.assertNotIn("旧位点", names)
            self.assertEqual(res["total"], 3)
            self.assertEqual(res["arm_hidden"],
                             {"foreign_arm": 1, "unmarked": 1})

    def test_left_arm_service_sees_only_left_files(self):
        state.chain_id = "left_arm"
        state.visible_waypoints = None
        self._write(state.waypoints_dir, "L-d_20260101_000000.json",
                    {"name": "L-左臂位点", "arm": "left_arm"})
        res = recordings.reach_waypoints(scope="all")
        self.assertEqual([w["name"] for w in res["waypoints"]],
                         ["L-左臂位点"])
        self.assertEqual(res["arm"], "left_arm")
        self.assertEqual(res["arm_hidden"]["foreign_arm"], 3)


class RecordStampTests(_StateSandbox):
    def test_new_waypoint_carries_recorded_combo(self):
        state.joint_names = ["j1"]
        state.chain_id = "right_arm"
        with mock.patch.object(recordings, "_read_joints",
                               return_value=[0.0]):
            res = recordings.reach_record_waypoint({"name": "新位点"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["waypoint"]["recorded_combo"],
                         {"arm": "right_arm",
                          "hand_id": "qiangnao-1-right"})

    def test_new_waypoint_gets_arm_prefix_and_field(self):
        state.joint_names = ["j1"]
        state.chain_id = "right_arm"
        with mock.patch.object(recordings, "_read_joints",
                               return_value=[0.0]):
            res = recordings.reach_record_waypoint({"name": "新位点"})
        self.assertTrue(res["ok"])
        wp = res["waypoint"]
        self.assertEqual(wp["name"], "R-新位点")
        self.assertEqual(wp["arm"], "right_arm")
        self.assertTrue(wp["file"].startswith("R-新位点_"))
        on_disk = json.loads(
            (state.waypoints_dir / wp["file"]).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["arm"], "right_arm")
        self.assertEqual(on_disk["name"], "R-新位点")
        # 刚录的即刻出现在本臂列表里
        listed = recordings.reach_waypoints()
        self.assertEqual([w["name"] for w in listed["waypoints"]],
                         ["R-新位点"])

    def test_already_prefixed_name_not_doubled(self):
        state.joint_names = ["j1"]
        state.chain_id = "right_arm"
        with mock.patch.object(recordings, "_read_joints",
                               return_value=[0.0]):
            res = recordings.reach_record_waypoint({"name": "R-新位点"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["waypoint"]["name"], "R-新位点")

    def test_other_arm_prefix_rejected_400(self):
        state.joint_names = ["j1"]
        state.chain_id = "right_arm"
        with mock.patch.object(recordings, "_read_joints",
                               return_value=[0.0]) as read:
            res = recordings.reach_record_waypoint({"name": "L-新位点"})
            read.assert_not_called()
        self.assertEqual(res.status_code, 400)
        self.assertEqual(list(state.waypoints_dir.glob("*.json")), [])

    def test_left_arm_service_prefixes_L(self):
        state.joint_names = ["j1"]
        state.chain_id = "left_arm"
        with mock.patch.object(recordings, "_read_joints",
                               return_value=[0.0]):
            res = recordings.reach_record_waypoint({"name": "新位点"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["waypoint"]["name"], "L-新位点")
        self.assertEqual(res["waypoint"]["arm"], "left_arm")

    def test_camera_only_no_combo_no_stamp(self):
        state.active_combo = None
        state.joint_names = ["j1"]
        state.chain_id = "right_arm"
        with mock.patch.object(recordings, "_read_joints",
                               return_value=[0.0]):
            res = recordings.reach_record_waypoint({"name": "新位点"})
        self.assertTrue(res["ok"])
        self.assertNotIn("recorded_combo", res["waypoint"])


class SaveSequenceArmTests(_StateSandbox):
    def setUp(self):
        super().setUp()
        state.active_combo = None   # 不触发 18000 自动认领
        self._write(state.waypoints_dir, "R-p_20260101_000000.json",
                    {"name": "R-p", "arm": "right_arm",
                     "named_joints": {"j1": 0.0}})
        self._write(state.waypoints_dir, "L-q_20260101_000000.json",
                    {"name": "L-q", "arm": "left_arm",
                     "named_joints": {"j1": 0.0}})
        self._write(state.waypoints_dir, "r_20260101_000000.json",
                    {"name": "r", "named_joints": {"j1": 0.0}})

    def test_save_sequence_prefixes_and_stamps_arm(self):
        res = recordings.reach_save_sequence(
            {"name": "新序列", "waypoints": ["R-p_20260101_000000.json"]})
        self.assertTrue(res["ok"])
        seq = res["sequence"]
        self.assertEqual(seq["name"], "R-新序列")
        self.assertEqual(seq["arm"], "right_arm")
        self.assertTrue(seq["file"].startswith("R-新序列_"))

    def test_save_sequence_rejects_other_arm_or_unmarked_waypoint(self):
        for wp in ("L-q_20260101_000000.json", "r_20260101_000000.json"):
            res = recordings.reach_save_sequence(
                {"name": "新序列", "waypoints": [wp]})
            self.assertEqual(res.status_code, 409, wp)
        self.assertEqual(list(state.sequences_dir.glob("*.json")), [])

    def test_save_sequence_rejects_other_arm_prefix_name(self):
        res = recordings.reach_save_sequence(
            {"name": "L-新序列", "waypoints": ["R-p_20260101_000000.json"]})
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
