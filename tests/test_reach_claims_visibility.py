"""18001 列表接口的认领可见性。

默认只回「激活组合已启用能力认领的」+「本组合自己录的」（来源戳豁免，
新录的不能刚存完就消失）；?scope=all 看全池；无组合（camera-only）不过滤。
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
        state.visible_sequences = {"0.50-扭-起手式"}
        self._write(state.sequences_dir, "a_20260101_000000.json",
                    {"name": "0.50-扭-起手式"})          # 已认领 → 可见
        self._write(state.sequences_dir, "b_20260101_000000.json",
                    {"name": "0.50-起手式新"})            # 别家的 → 隐藏
        self._write(state.sequences_dir, "c_20260101_000000.json", {
            "name": "新录未认领",                          # 自己录的 → 可见
            "recorded_combo": {"arm": "right_arm",
                               "hand_id": "qiangnao-1-right"},
        })

    def test_default_filters_to_claims_plus_own_recordings(self):
        res = recordings.reach_sequences()
        self.assertEqual({s["name"] for s in res["sequences"]},
                         {"0.50-扭-起手式", "新录未认领"})
        self.assertEqual(res["hidden"], 1)
        self.assertTrue(res["filtered"])

    def test_scope_all_returns_everything(self):
        res = recordings.reach_sequences(scope="all")
        self.assertEqual(len(res["sequences"]), 3)
        self.assertEqual(res["hidden"], 0)

    def test_no_combo_context_skips_filtering(self):
        state.visible_sequences = None
        res = recordings.reach_sequences()
        self.assertEqual(len(res["sequences"]), 3)
        self.assertFalse(res["filtered"])


class WaypointVisibilityTests(_StateSandbox):
    def setUp(self):
        super().setUp()
        state.visible_waypoints = {"0.50-扭-终点"}
        self._write(state.waypoints_dir, "a_20260101_000000.json",
                    {"name": "0.50-扭-终点"})             # 生效位点 → 可见
        self._write(state.waypoints_dir, "b_20260101_000000.json",
                    {"name": "0.50-起手式新终点"})         # 别家的 → 隐藏
        self._write(state.waypoints_dir, "c_20260101_000000.json", {
            "name": "新录位点",                            # 自己录的 → 可见
            "recorded_combo": {"arm": "right_arm",
                               "hand_id": "qiangnao-1-right"},
        })

    def test_default_filters_to_claims_plus_own_recordings(self):
        res = recordings.reach_waypoints()
        self.assertEqual({w["name"] for w in res["waypoints"]},
                         {"0.50-扭-终点", "新录位点"})
        self.assertEqual(res["hidden"], 1)
        self.assertTrue(res["filtered"])

    def test_scope_all_returns_everything(self):
        res = recordings.reach_waypoints(scope="all")
        self.assertEqual(len(res["waypoints"]), 3)


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

    def test_camera_only_no_combo_no_stamp(self):
        state.active_combo = None
        state.joint_names = ["j1"]
        state.chain_id = "right_arm"
        with mock.patch.object(recordings, "_read_joints",
                               return_value=[0.0]):
            res = recordings.reach_record_waypoint({"name": "新位点"})
        self.assertTrue(res["ok"])
        self.assertNotIn("recorded_combo", res["waypoint"])


if __name__ == "__main__":
    unittest.main()
