from __future__ import annotations

import math
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from api import dispatch
from api.flow import SwitchFlow
from core import capability_registry as reg


def _install_registry(registry):
    dispatch._capability_registry_cache = registry
    dispatch._capability_registry_loaded = True


class _RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = (dispatch._capability_registry_cache,
                       dispatch._capability_registry_loaded)

    def tearDown(self):
        (dispatch._capability_registry_cache,
         dispatch._capability_registry_loaded) = self._saved


class KindSupportTests(_RegistryTestCase):
    def test_seed_registry_reproduces_legacy_support_table(self):
        _install_registry(reg.seed_registry())
        for site, kinds in dispatch.SITE_SUPPORTED_KINDS.items():
            for kind in ("close_to_remote", "remote_to_close"):
                self.assertEqual(
                    dispatch._kind_supported(site, kind),
                    kind in kinds,
                    f"{site}/{kind} 与旧硬编码表不一致",
                )

    def test_disabling_capability_removes_support(self):
        registry = reg.seed_registry()
        for cap in registry["capabilities"]:
            if cap["task"]["direction"] == "ltr":
                cap["enabled"] = False
        _install_registry(registry)
        # 工厂柜 就地→远方 = 向右拨（ltr）→ 停用后不再支持
        self.assertFalse(
            dispatch._kind_supported("factory", "close_to_remote"))
        self.assertTrue(
            dispatch._kind_supported("factory", "remote_to_close"))

    def test_unimplemented_method_is_not_supported(self):
        registry = reg.seed_registry()
        for cap in registry["capabilities"]:
            cap["method"] = "twist"
            cap["method_params"] = {}
        _install_registry(reg.validate_registry(registry))
        self.assertFalse(
            dispatch._kind_supported("factory", "remote_to_close"))

    def test_registry_unavailable_falls_back_to_legacy_table(self):
        _install_registry(None)
        self.assertTrue(dispatch._kind_supported("lab", "close_to_remote"))
        self.assertFalse(dispatch._kind_supported("lab", "remote_to_close"))

    def test_capability_for_kind_maps_site_semantics_to_direction(self):
        _install_registry(reg.seed_registry())
        # 工厂柜：远方→就地 = 向左拨（rtl）
        cap = dispatch._capability_for_kind("factory", "remote_to_close")
        self.assertEqual(cap["task"]["direction"], "rtl")
        # 实验室柜：就地→远方 = 向左拨（rtl）
        cap = dispatch._capability_for_kind("lab", "close_to_remote")
        self.assertEqual(cap["task"]["direction"], "rtl")


class SpawnReachTests(_RegistryTestCase):
    def _spawn(self, registry, calib_status="pending"):
        _install_registry(registry)
        task = {"log": []}
        args = Namespace(
            reach_port=18001, camera_host="127.0.0.1",
            camera_request_port=60000, camera_name="head",
            camera_rgbd_calib="/tmp/rgbd.json", network_interface="lo",
            calib="/legacy/handeye3d_result.json", tool_out_mm=15.0,
            yolo="http://127.0.0.1:7004", camera_port=None,
        )
        info = {"status": calib_status,
                "path": reg.calib_rel_path("right_arm", "yinshi-1-right"),
                "source_path": "", "registered_at": "",
                "solved_at": None, "residual_mm": None, "num_samples": None,
                "arm": "right_arm", "hand_id": "yinshi-1-right"}
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(dispatch, "_args", args),
                mock.patch.object(dispatch, "ROOT", Path(temporary)),
                mock.patch.object(dispatch, "calibration_info",
                                  return_value=info),
                mock.patch.object(dispatch.subprocess, "Popen") as popen,
            ):
                dispatch._spawn_reach(task)
            cmd = popen.call_args.args[0]
        return cmd, task

    def test_spawn_uses_active_combo_chain_and_archived_calib(self):
        cmd, task = self._spawn(reg.seed_registry(), calib_status="ready")
        self.assertIn("--chain", cmd)
        self.assertEqual(cmd[cmd.index("--chain") + 1], "right_arm")
        calib = cmd[cmd.index("--calib") + 1]
        self.assertTrue(calib.endswith(
            "config/hand_eye/right_arm__yinshi-1-right/handeye3d_result.json"))
        # tool_out_mm 取手型号登记值
        self.assertEqual(cmd[cmd.index("--tool-out-mm") + 1], "15.0")
        self.assertTrue(any("激活组合" in line for line in task["log"]))

    def test_spawn_falls_back_to_cli_calib_when_pending(self):
        cmd, task = self._spawn(reg.seed_registry(), calib_status="pending")
        self.assertEqual(cmd[cmd.index("--calib") + 1],
                         "/legacy/handeye3d_result.json")
        self.assertTrue(any("待补" in line for line in task["log"]))

    def test_spawn_without_registry_keeps_legacy_behavior(self):
        cmd, task = self._spawn(None)
        self.assertEqual(cmd[cmd.index("--chain") + 1], "right_arm")
        self.assertEqual(cmd[cmd.index("--calib") + 1],
                         "/legacy/handeye3d_result.json")


class FlowCapabilityParamTests(unittest.TestCase):
    _PLANE = {
        "left_root": [-1.0, 0.0, 0.0],
        "right_root": [1.0, 0.0, 0.0],
        "wall_up_root": [0.0, 0.0, 1.0],
    }

    def test_push_hold_is_forwarded_to_execute_body(self):
        client = mock.Mock()
        client.joints.return_value = {"ok": True, "named_joints": {}}
        client.plan_cartesian.return_value = {
            "ok": True, "waypoints": [{"named_joints": {}}]}
        client.execute.return_value = {"ok": True}
        flow = SwitchFlow(client=client, site="factory",
                          flip_kind="remote_to_close", push_hold_s=0.5)
        flow._wait_exec = mock.Mock()
        flow._log = mock.Mock()

        flow._sidestep_flick({"plane": self._PLANE}, "测试")

        body = client.execute.call_args.kwargs
        self.assertEqual(body["push_hold_s"], 0.5)
        self.assertEqual(body["push"]["force_n"], 15.0)

    def test_push_hold_omitted_when_not_configured(self):
        client = mock.Mock()
        client.joints.return_value = {"ok": True, "named_joints": {}}
        client.plan_cartesian.return_value = {
            "ok": True, "waypoints": [{"named_joints": {}}]}
        client.execute.return_value = {"ok": True}
        flow = SwitchFlow(client=client, site="factory",
                          flip_kind="remote_to_close")
        flow._wait_exec = mock.Mock()
        flow._log = mock.Mock()

        flow._sidestep_flick({"plane": self._PLANE}, "测试")

        self.assertNotIn("push_hold_s", client.execute.call_args.kwargs)

    def test_sidestep_down_deg_overrides_class_default(self):
        flow = SwitchFlow(client=mock.Mock(), site="factory",
                          flip_kind="remote_to_close", sidestep_down_deg=30.0)
        direction = flow._sidestep_direction(self._PLANE)
        self.assertAlmostEqual(direction[0], -math.cos(math.radians(30)))
        self.assertAlmostEqual(direction[2], -math.sin(math.radians(30)))

    def test_default_down_deg_unchanged_without_injection(self):
        flow = SwitchFlow(client=mock.Mock(), site="factory",
                          flip_kind="remote_to_close")
        direction = flow._sidestep_direction(self._PLANE)
        self.assertAlmostEqual(direction[2], -math.sin(math.radians(15)))

    def test_injected_pose_pattern_overrides_builtin_family(self):
        client = mock.Mock()
        client.sequences.return_value = {"sequences": [
            {"name": "0.50-起手式新",
             "file": "0.50-起手式新_20260822_031632.json", "waypoints": []},
            {"name": "0.50-新手型-起手式",
             "file": "0.50-新手型-起手式_20260901_000000.json",
             "waypoints": []},
        ]}
        flow = SwitchFlow(
            client=client, site="factory", flip_kind="remote_to_close",
            pose_pattern=r"^\s*(\d+(?:\.\d+)?)-新手型-起手式\s*$")
        pose = flow.choose_opening_pose(0.53)
        self.assertEqual(pose["name"], "0.50-新手型-起手式")

    def test_without_pose_pattern_builtin_family_still_used(self):
        client = mock.Mock()
        client.sequences.return_value = {"sequences": [
            {"name": "0.50-起手式新",
             "file": "0.50-起手式新_20260822_031632.json", "waypoints": []},
        ]}
        flow = SwitchFlow(client=client, site="factory",
                          flip_kind="remote_to_close")
        self.assertEqual(flow.choose_opening_pose(0.53)["name"],
                         "0.50-起手式新")


if __name__ == "__main__":
    unittest.main()
