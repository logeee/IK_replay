"""灵巧手姿态库（data/hand_poses）与 18001 手位执行端点。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import hand_poses
from core.hand_runtime import (
    HandRuntime,
    build_hand_runtime_config,
    configure_hand_runtime,
)


class HandPosesCrudTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_save_list_load_delete_roundtrip(self):
        item = hand_poses.save_pose(
            "旋钮-预抓取", [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
            device_id="brainco_revo2", side="right",
            combo={"arm": "right_arm", "hand_id": "qiangnao-1-right"},
            directory=self.dir)
        self.assertTrue(item["file"].startswith("旋钮-预抓取_"))
        listed = hand_poses.list_poses(self.dir)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "旋钮-预抓取")
        self.assertEqual(listed[0]["recorded_combo"]["hand_id"],
                         "qiangnao-1-right")
        loaded = hand_poses.load_pose(item["file"], self.dir)
        self.assertEqual(loaded["positions"], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        self.assertTrue(hand_poses.delete_pose(item["file"], self.dir))
        self.assertEqual(hand_poses.list_poses(self.dir), [])

    def test_positions_validation(self):
        with self.assertRaises(ValueError):
            hand_poses.validate_positions([0.1, 0.2])          # 数量不对
        with self.assertRaises(ValueError):
            hand_poses.validate_positions([0, 0, 0, 0, 0, 1.2])  # 超界
        with self.assertRaises(ValueError):
            hand_poses.validate_positions(
                [0, 0, 0, 0, 0, "x"])                          # 非数值

    def test_name_validation_and_traversal_guard(self):
        with self.assertRaises(ValueError):
            hand_poses.save_pose("", [0] * 6, device_id="d", side="right",
                                 directory=self.dir)
        with self.assertRaises(ValueError):
            hand_poses.save_pose("a/b", [0] * 6, device_id="d", side="right",
                                 directory=self.dir)
        self.assertIsNone(hand_poses.safe_pose_path("../x.json", self.dir))
        self.assertIsNone(hand_poses.safe_pose_path("x.txt", self.dir))


def _registry() -> dict:
    return {
        "active": {"arm": "right_arm", "hand_id": "qiangnao-1-right"},
        "hands": [{
            "id": "qiangnao-1-right",
            "name": "强脑-右-1",
            "design_side": "right",
            "hand_web_device_id": "brainco_revo2",
            "tcp_point_id": "",
        }],
    }


def _calibration() -> dict:
    return {
        "arm": "right_arm",
        "hand_id": "qiangnao-1-right",
        "hand_base_link": "base_link",
        "wrist_link": "right_wrist_yaw_link",
        "T_wrist2hand": [
            [1.0, 0.0, 0.0, 0.1],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        "p_tool_wrist_m": [0.2, 0.0, 0.0],
    }


class HandCommandTests(unittest.TestCase):
    def _runtime(self, root: Path, post) -> HandRuntime:
        config = build_hand_runtime_config(
            registry=_registry(),
            calibration=_calibration(),
            chain_id="right_arm",
            expected_wrist_link="right_wrist_yaw_link",
            service_url="https://127.0.0.1:18089",
            assets_root=root,
        )
        assert config is not None
        return HandRuntime(config, post_json=post)

    def test_command_posts_side_positions_duration(self):
        calls = []

        def post(url, payload, timeout, verify_tls):
            calls.append((url, payload))
            return {"ok": True}

        with tempfile.TemporaryDirectory() as temporary:
            result = self._runtime(Path(temporary), post).command(
                [0.1, 0.2, 0.3, 0.4, 0.5, 0.6], duration_ms=800)
        self.assertTrue(result["ok"])
        url, payload = calls[0]
        self.assertTrue(url.endswith("/api/command"))
        self.assertEqual(payload["side"], "right")
        self.assertEqual(payload["positions"], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        self.assertEqual(payload["duration_ms"], 800)

    def test_command_propagates_occupied_error(self):
        def post(url, payload, timeout, verify_tls):
            return {"ok": False, "error": "被视觉控制占用"}

        with tempfile.TemporaryDirectory() as temporary:
            result = self._runtime(Path(temporary), post).command([0] * 6)
        self.assertFalse(result["ok"])
        self.assertIn("被视觉控制占用", result["error"])


class ReachHandPoseEndpointTests(unittest.TestCase):
    """18001 POST /api/reach/hand/pose：按姿态文件或裸 positions 下发。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(hand_poses, "POSES_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(configure_hand_runtime, None)
        self.sent = []

        def post(url, payload, timeout, verify_tls):
            self.sent.append((url, payload))
            return {"ok": True}

        self.assets = tempfile.TemporaryDirectory()
        self.addCleanup(self.assets.cleanup)
        config = build_hand_runtime_config(
            registry=_registry(),
            calibration=_calibration(),
            chain_id="right_arm",
            expected_wrist_link="right_wrist_yaw_link",
            service_url="https://127.0.0.1:18089",
            assets_root=Path(self.assets.name),
        )
        assert config is not None
        configure_hand_runtime(HandRuntime(config, post_json=post))

    def test_pose_from_library_file(self):
        from adapters.reach import hand as reach_hand

        item = hand_poses.save_pose(
            "旋钮-预抓取", [0.3, 0.86, 0.24, 0.82, 0.82, 0.82],
            device_id="brainco_revo2", side="right")
        result = reach_hand.dexterous_hand_pose({"file": item["file"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "旋钮-预抓取")
        _, payload = self.sent[0]
        self.assertEqual(payload["positions"],
                         [0.3, 0.86, 0.24, 0.82, 0.82, 0.82])

    def test_pose_with_raw_positions(self):
        from adapters.reach import hand as reach_hand

        result = reach_hand.dexterous_hand_pose(
            {"positions": [0, 0, 0, 0, 0, 0], "duration_ms": 300})
        self.assertTrue(result["ok"])
        _, payload = self.sent[0]
        self.assertEqual(payload["duration_ms"], 300)

    def test_pose_missing_file_is_404(self):
        from adapters.reach import hand as reach_hand

        response = reach_hand.dexterous_hand_pose({"file": "不存在.json"})
        self.assertEqual(response.status_code, 404)

    def test_pose_without_runtime_is_error(self):
        from adapters.reach import hand as reach_hand

        configure_hand_runtime(None)
        response = reach_hand.dexterous_hand_pose(
            {"positions": [0, 0, 0, 0, 0, 0]})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
