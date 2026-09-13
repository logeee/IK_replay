from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.hand_runtime import (
    HandRuntime,
    apply_mount_profile,
    build_hand_runtime_config,
    configure_hand_runtime,
    hand_connect,
)
from reach_server import _camera_stream_name, _validate_camera_identity


def _registry() -> dict:
    return {
        "active": {"arm": "right_arm", "hand_id": "qiangnao-1-right"},
        "hands": [{
            "id": "qiangnao-1-right",
            "name": "强脑-右-1",
            "design_side": "right",
            "hand_web_device_id": "brainco_revo2",
            "tcp_point_id": "tip:right_index_tip",
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
        "tcp_points_wrist_m": [{
            "id": "tip:right_index_tip",
            "p_wrist_m": [0.23, 0.02, 0.03],
        }],
    }


class HandRuntimeConfigTests(unittest.TestCase):
    def test_builds_only_from_consistent_active_combo(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = build_hand_runtime_config(
                registry=_registry(),
                calibration=_calibration(),
                chain_id="right_arm",
                expected_wrist_link="right_wrist_yaw_link",
                service_url="https://127.0.0.1:18089",
                assets_root=Path(temporary),
            )
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.device_id, "brainco_revo2")
        self.assertEqual(config.p_tool_wrist_m, [0.23, 0.02, 0.03])

    def test_rejects_calibration_for_another_hand(self):
        calibration = _calibration()
        calibration["hand_id"] = "yinshi-1-right"
        with self.assertRaisesRegex(ValueError, "hand_id"):
            build_hand_runtime_config(
                registry=_registry(),
                calibration=calibration,
                chain_id="right_arm",
                expected_wrist_link="right_wrist_yaw_link",
                service_url="https://127.0.0.1:18089",
                assets_root=Path("/tmp"),
            )

    def test_rejects_wrong_wrist_link(self):
        with self.assertRaisesRegex(ValueError, "wrist_link"):
            build_hand_runtime_config(
                registry=_registry(),
                calibration=_calibration(),
                chain_id="right_arm",
                expected_wrist_link="left_wrist_yaw_link",
                service_url="https://127.0.0.1:18089",
                assets_root=Path("/tmp"),
            )

    def test_rejects_mount_calibrated_against_another_model_root(self):
        calibration = _calibration()
        calibration["hand_base_link"] = "R_hand_base_link"
        with self.assertRaisesRegex(ValueError, "hand_base_link"):
            build_hand_runtime_config(
                registry=_registry(),
                calibration=calibration,
                chain_id="right_arm",
                expected_wrist_link="right_wrist_yaw_link",
                service_url="https://127.0.0.1:18089",
                assets_root=Path("/tmp"),
            )

    def test_fixed_cad_profile_replaces_mount_and_reprojects_tcp(self):
        registry = _registry()
        registry["active"]["mount_profile_id"] = "cad_nominal"
        registry["hands"][0]["mount_profiles"] = [{
            "id": "measured_3d",
            "name": "3D 标定实测",
            "source": "calibration",
            "model": {"source": "device_default", "root": "", "urdf": ""},
        }, {
            "id": "cad_nominal",
            "name": "CAD 名义装配",
            "source": "fixed",
            "hand_base_link": "base_link",
            "model": {
                "source": "project",
                "root": "hands/revo2_left_cad",
                "urdf": "revo2_left_cad.urdf",
            },
            "T_wrist2hand": [
                [1, 0, 0, 0.2], [0, 1, 0, 0],
                [0, 0, 1, 0], [0, 0, 0, 1],
            ],
        }]
        calibration, profile = apply_mount_profile(registry, _calibration())
        self.assertEqual(profile["id"], "cad_nominal")
        self.assertEqual(calibration["T_wrist2hand"][0][3], 0.2)
        # 原 TCP 比原手根多 0.13m；换安装后仍是同一个手系点。
        self.assertAlmostEqual(
            calibration["tcp_points_wrist_m"][0]["p_wrist_m"][0], 0.33)

        with tempfile.TemporaryDirectory() as temporary:
            config = build_hand_runtime_config(
                registry=registry,
                calibration=calibration,
                chain_id="right_arm",
                expected_wrist_link="right_wrist_yaw_link",
                service_url="https://127.0.0.1:18089",
                assets_root=Path(temporary),
            )
            assert config is not None
            runtime = HandRuntime(
                config,
                fetch_json=lambda *_: {"devices": []},
            )
            snapshot = runtime.snapshot()
        self.assertEqual(snapshot["mount_profile"]["id"], "cad_nominal")
        self.assertEqual(
            snapshot["model"]["urdf_url"],
            "/assets/hands/revo2_left_cad/revo2_left_cad.urdf",
        )


class CameraIdentityTests(unittest.TestCase):
    def test_capability_camera_roles_map_to_teleimager_streams(self):
        self.assertEqual(_camera_stream_name("head"), "head_rgbd_camera")
        self.assertEqual(_camera_stream_name("waist"), "torso_rgbd_camera")

    def test_explicit_teleimager_stream_name_is_preserved(self):
        self.assertEqual(
            _camera_stream_name("head_rgbd_camera"), "head_rgbd_camera")

    def test_accepts_matching_gemini_serial_and_profile(self):
        _validate_camera_identity(
            {"camera": {
                "serial": "CP0T263000BE",
                "width": 1920,
                "height": 1080,
            }},
            {
                "source": "zmq",
                "serial": "CP0T263000BE",
                "width": 1920,
                "height": 1080,
            },
        )

    def test_rejects_extrinsic_from_another_camera(self):
        with self.assertRaisesRegex(ValueError, "序列号"):
            _validate_camera_identity(
                {"camera": {"serial": "CP0T263000BE"}},
                {"source": "zmq", "serial": "ANOTHER_CAMERA"},
            )


class HandRuntimeSnapshotTests(unittest.TestCase):
    def _runtime(self, root: Path, status: dict) -> HandRuntime:
        config = build_hand_runtime_config(
            registry=_registry(),
            calibration=_calibration(),
            chain_id="right_arm",
            expected_wrist_link="right_wrist_yaw_link",
            service_url="https://127.0.0.1:18089",
            assets_root=root,
        )
        assert config is not None

        def fetch(url: str, timeout: float, verify_tls: bool):
            self.assertEqual(timeout, 0.8)
            self.assertFalse(verify_tls)
            if url.endswith("/api/devices"):
                return {"devices": []}
            if url.endswith("/api/status"):
                return status
            raise AssertionError(url)

        return HandRuntime(config, fetch_json=fetch)

    def test_snapshot_exposes_model_mount_and_live_positions(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(Path(temporary), {
                "connected": True,
                "device_id": "brainco_revo2",
                "transport": "dds",
                "hands": {"right": {"positions": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]}},
            })
            snapshot = runtime.snapshot()
        self.assertTrue(snapshot["service"]["connected"])
        self.assertEqual(snapshot["positions"], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        self.assertEqual(snapshot["wrist_link"], "right_wrist_yaw_link")
        self.assertEqual(
            snapshot["model"]["urdf_url"],
            "/api/reach/hand/assets/brainco_hand/brainco_right.urdf",
        )

    def test_snapshot_rejects_state_from_another_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._runtime(Path(temporary), {
                "connected": True,
                "device_id": "inspire_dfx",
                "hands": {"right": {"positions": [0.5] * 6}},
            })
            snapshot = runtime.snapshot()
        self.assertFalse(snapshot["service"]["compatible"])
        self.assertIsNone(snapshot["positions"])
        self.assertIn("期望设备 brainco_revo2", snapshot["service"]["error"])

    def test_asset_path_cannot_escape_configured_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / "brainco_hand" / "brainco_right.urdf"
            asset.parent.mkdir()
            asset.write_text("<robot/>", encoding="utf-8")
            runtime = self._runtime(root, {})
            self.assertEqual(
                runtime.asset_path("brainco_hand/brainco_right.urdf"),
                asset.resolve(),
            )
            with self.assertRaises(FileNotFoundError):
                runtime.asset_path("../secret")


class HandConnectTests(unittest.TestCase):
    """接管手臂时顺带连接 18089：POST /api/connect 按激活组合的设备。"""

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

    def test_connect_posts_device_and_reports_ok(self):
        calls = []

        def post(url, payload, timeout, verify_tls):
            calls.append((url, payload, timeout, verify_tls))
            return {"ok": True, "connected": True}

        with tempfile.TemporaryDirectory() as temporary:
            result = self._runtime(Path(temporary), post).connect()
        self.assertTrue(result["ok"])
        self.assertEqual(result["device_id"], "brainco_revo2")
        self.assertEqual(result["side"], "right")
        self.assertEqual(result["hand_name"], "强脑-右-1")
        url, payload, timeout, verify_tls = calls[0]
        self.assertTrue(url.endswith("/api/connect"))
        self.assertEqual(payload, {"device_id": "brainco_revo2"})
        self.assertEqual(timeout, 5.0)
        self.assertFalse(verify_tls)

    def test_connect_propagates_service_refusal(self):
        def post(url, payload, timeout, verify_tls):
            return {"ok": False, "error": "被视觉控制占用"}

        with tempfile.TemporaryDirectory() as temporary:
            result = self._runtime(Path(temporary), post).connect()
        self.assertFalse(result["ok"])
        self.assertIn("被视觉控制占用", result["error"])

    def test_connect_unreachable_reports_error(self):
        def post(url, payload, timeout, verify_tls):
            raise OSError("connection refused")

        with tempfile.TemporaryDirectory() as temporary:
            result = self._runtime(Path(temporary), post).connect()
        self.assertFalse(result["ok"])
        self.assertIn("18089 不可达", result["error"])


class HandConnectModuleTests(unittest.TestCase):
    """模块级 hand_connect()：无 runtime 时 enabled=False（不算错误）。"""

    def tearDown(self):
        configure_hand_runtime(None)

    def test_without_runtime_reports_disabled(self):
        configure_hand_runtime(None)
        result = hand_connect()
        self.assertFalse(result["ok"])
        self.assertFalse(result["enabled"])

    def test_with_runtime_reports_enabled(self):
        def post(url, payload, timeout, verify_tls):
            return {"ok": True}

        with tempfile.TemporaryDirectory() as temporary:
            config = build_hand_runtime_config(
                registry=_registry(),
                calibration=_calibration(),
                chain_id="right_arm",
                expected_wrist_link="right_wrist_yaw_link",
                service_url="https://127.0.0.1:18089",
                assets_root=Path(temporary),
            )
            assert config is not None
            configure_hand_runtime(HandRuntime(config, post_json=post))
            result = hand_connect()
        self.assertTrue(result["ok"])
        self.assertTrue(result["enabled"])

    def test_arm_takeover_note_wording(self):
        from adapters.reach.execution import _hand_takeover_note

        # 组合没绑 18089 设备：不啰嗦
        configure_hand_runtime(None)
        _, note = _hand_takeover_note()
        self.assertEqual(note, "")

        # 绑了且连上：消息报手型号与侧
        def post(url, payload, timeout, verify_tls):
            return {"ok": True}

        with tempfile.TemporaryDirectory() as temporary:
            config = build_hand_runtime_config(
                registry=_registry(),
                calibration=_calibration(),
                chain_id="right_arm",
                expected_wrist_link="right_wrist_yaw_link",
                service_url="https://127.0.0.1:18089",
                assets_root=Path(temporary),
            )
            assert config is not None
            configure_hand_runtime(HandRuntime(config, post_json=post))
            _, note = _hand_takeover_note()
        self.assertIn("灵巧手已连接", note)
        self.assertIn("强脑-右-1", note)


if __name__ == "__main__":
    unittest.main()
