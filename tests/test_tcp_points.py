"""TCP 工作点：core 库 CRUD/默认点/坐标换算 + 18001 选择端点热替换。"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import tcp_points  # noqa: E402

# 平移 (0.1, 0.2, 0.3) + 绕 z 转 90°：x_hand -> y_wrist
T_DEMO = [
    [0.0, -1.0, 0.0, 0.1],
    [1.0, 0.0, 0.0, 0.2],
    [0.0, 0.0, 1.0, 0.3],
    [0.0, 0.0, 0.0, 1.0],
]


class TcpPointsCrudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_list_filter_by_hand(self):
        tcp_points.save_point("捏合点", [0.01, -0.02, 0.03],
                              hand_id="qiangnao-1-right",
                              combo={"arm": "right_arm",
                                     "hand_id": "qiangnao-1-right"},
                              directory=self.dir)
        tcp_points.save_point("指尖", [0.0, 0.0, 0.1],
                              hand_id="yinshi-right", directory=self.dir)
        mine = tcp_points.list_points("qiangnao-1-right", directory=self.dir)
        self.assertEqual([p["name"] for p in mine], ["捏合点"])
        self.assertEqual(mine[0]["recorded_combo"]["arm"], "right_arm")
        everyone = tcp_points.list_points(directory=self.dir)
        self.assertEqual(len(everyone), 2)

    def test_update_keeps_file_and_created_at(self):
        item = tcp_points.save_point("旧名", [0, 0, 0.05],
                                     hand_id="h", directory=self.dir)
        updated = tcp_points.update_point(
            item["file"], name="新名", xyz_hand=[0.01, 0.02, 0.03],
            directory=self.dir)
        self.assertEqual(updated["file"], item["file"])
        self.assertEqual(updated["name"], "新名")
        self.assertEqual(updated["created_at"], item["created_at"])
        self.assertIn("updated_at", updated)
        loaded = tcp_points.load_point(item["file"], directory=self.dir)
        self.assertEqual(loaded["xyz_hand"], [0.01, 0.02, 0.03])

    def test_validate_rejects_bad_xyz(self):
        for bad in ([0, 0], "abc", [0, 0, float("nan")], [0, 0, 2.0]):
            with self.assertRaises(ValueError):
                tcp_points.save_point("x", bad, hand_id="h",
                                      directory=self.dir)

    def test_default_lifecycle_and_delete_cleanup(self):
        item = tcp_points.save_point("p", [0, 0, 0.01],
                                     hand_id="h", directory=self.dir)
        self.assertIsNone(tcp_points.get_default("h", directory=self.dir))
        tcp_points.set_default("h", "custom", item["file"],
                               directory=self.dir)
        self.assertEqual(
            tcp_points.get_default("h", directory=self.dir),
            {"kind": "custom", "key": item["file"]})
        # 删除点时顺带清默认
        tcp_points.delete_point(item["file"], directory=self.dir)
        self.assertIsNone(tcp_points.get_default("h", directory=self.dir))
        # 标定点默认不受删除影响
        tcp_points.set_default("h", "calib", "tip:x", directory=self.dir)
        self.assertEqual(
            tcp_points.get_default("h", directory=self.dir)["kind"], "calib")
        tcp_points.clear_default("h", directory=self.dir)
        self.assertIsNone(tcp_points.get_default("h", directory=self.dir))

    def test_path_traversal_blocked(self):
        self.assertIsNone(tcp_points.safe_point_path("../x.json"))
        self.assertIsNone(tcp_points.safe_point_path("a/b.json"))
        self.assertIsNone(tcp_points.safe_point_path("_default.json"))


class TransformTest(unittest.TestCase):
    def test_hand_to_wrist_roundtrip(self):
        p_hand = [0.05, -0.02, 0.11]
        p_wrist = tcp_points.hand_to_wrist(T_DEMO, p_hand)
        # 绕 z 转 90°: (x,y)->(-y,x)，再加平移
        self.assertAlmostEqual(p_wrist[0], 0.1 - (-0.02))
        self.assertAlmostEqual(p_wrist[1], 0.2 + 0.05)
        self.assertAlmostEqual(p_wrist[2], 0.3 + 0.11)
        back = tcp_points.wrist_to_hand(T_DEMO, p_wrist)
        for a, b in zip(back, p_hand):
            self.assertAlmostEqual(a, b)

    def test_calib_tcp_points_filters_garbage(self):
        calib = {"tcp_points_wrist_m": [
            {"id": "tip:a", "label": "A", "link": "la",
             "p_wrist_m": [0.1, 0.2, 0.3]},
            {"id": "", "p_wrist_m": [0, 0, 0]},          # 缺 id
            {"id": "tip:b", "p_wrist_m": [0, 0]},        # 维度不对
            {"id": "tip:c", "p_wrist_m": [0, 0, math.inf]},  # 非有限
            "not-a-dict",
        ]}
        points = tcp_points.calib_tcp_points(calib)
        self.assertEqual([p["id"] for p in points], ["tip:a"])
        self.assertEqual(points[0]["label"], "A")


class ReachTcpSelectTest(unittest.TestCase):
    """18001 /tcp/select 的热替换逻辑（直接驱动 apply_selection）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # 把 tcp_store 的默认目录指到临时目录
        import adapters.reach.tcp as reach_tcp
        self.reach_tcp = reach_tcp
        self.state = reach_tcp.state
        self._saved = {
            "handeye_ready": self.state.handeye_ready,
            "p_tool": self.state.p_tool,
            "p_tool_startup": self.state.p_tool_startup,
            "tcp_selection": self.state.tcp_selection,
            "T_wrist2hand": self.state.T_wrist2hand,
            "calib_tcp_points": self.state.calib_tcp_points,
            "active_combo": self.state.active_combo,
        }
        self._patch_dir()

        self.state.handeye_ready = True
        self.state.p_tool = [0.2, 0.0, 0.0]
        self.state.p_tool_startup = [0.2, 0.0, 0.0]
        self.state.tcp_selection = None
        self.state.T_wrist2hand = T_DEMO
        self.state.calib_tcp_points = [
            {"id": "tip:index", "label": "食指指尖", "link": "l",
             "p_wrist_m": [0.23, 0.02, 0.03]},
        ]
        self.state.active_combo = {"arm": "right_arm",
                                   "hand_id": "qiangnao-1-right"}

    def _patch_dir(self):
        self._orig_points_dir = tcp_points.POINTS_DIR
        tcp_points.POINTS_DIR = self.dir

    def tearDown(self):
        tcp_points.POINTS_DIR = self._orig_points_dir
        for key, value in self._saved.items():
            setattr(self.state, key, value)
        self.tmp.cleanup()

    def test_select_custom_transforms_to_wrist(self):
        item = tcp_points.save_point("捏合点", [0.05, -0.02, 0.11],
                                     hand_id="qiangnao-1-right")
        info = self.reach_tcp.apply_selection("custom", item["file"])
        self.assertAlmostEqual(self.state.p_tool[0], 0.12)
        self.assertAlmostEqual(self.state.p_tool[1], 0.25)
        self.assertAlmostEqual(self.state.p_tool[2], 0.41)
        self.assertEqual(info["selection"]["label"], "捏合点")

    def test_select_calib_and_restore(self):
        self.reach_tcp.apply_selection("calib", "tip:index")
        self.assertEqual(self.state.p_tool, [0.23, 0.02, 0.03])
        self.assertEqual(self.state.tcp_selection["kind"], "calib")
        info = self.reach_tcp.apply_selection(None, None)
        self.assertEqual(self.state.p_tool, [0.2, 0.0, 0.0])
        self.assertIsNone(info["selection"])

    def test_reject_other_hands_point(self):
        item = tcp_points.save_point("别人的", [0, 0, 0.01],
                                     hand_id="yinshi-right")
        with self.assertRaises(ValueError):
            self.reach_tcp.apply_selection("custom", item["file"])
        self.assertEqual(self.state.p_tool, [0.2, 0.0, 0.0])  # 没被污染

    def test_reject_when_camera_only(self):
        self.state.handeye_ready = False
        with self.assertRaises(ValueError):
            self.reach_tcp.apply_selection("calib", "tip:index")

    def test_startup_default_applied(self):
        item = tcp_points.save_point("默认点", [0.0, 0.0, 0.1],
                                     hand_id="qiangnao-1-right")
        tcp_points.set_default("qiangnao-1-right", "custom", item["file"])
        note = self.reach_tcp.apply_startup_default()
        self.assertIn("默认 TCP 点已应用", note)
        self.assertAlmostEqual(self.state.p_tool[0], 0.1)
        self.assertAlmostEqual(self.state.p_tool[1], 0.2)
        self.assertAlmostEqual(self.state.p_tool[2], 0.4)

    def test_startup_default_missing_file_falls_back(self):
        tcp_points.set_default("qiangnao-1-right", "custom", "ghost.json")
        note = self.reach_tcp.apply_startup_default()
        self.assertIn("失败", note)
        self.assertEqual(self.state.p_tool, [0.2, 0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
