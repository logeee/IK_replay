from __future__ import annotations

import math
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from api import dispatch
from api.flow import ErrorCode, FlowError, SwitchFlow
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
        self.assertTrue(dispatch._kind_supported("lab", "remote_to_close"))
        self.assertFalse(dispatch._kind_supported("lab", "close_to_remote"))

    def test_capability_for_kind_maps_kind_to_direction(self):
        _install_registry(reg.seed_registry())
        # 方向由 kind 唯一决定：远方→就地 = 向左拨（rtl），任何现场一致
        cap = dispatch._capability_for_kind("factory", "remote_to_close")
        self.assertEqual(cap["task"]["direction"], "rtl")
        cap = dispatch._capability_for_kind("lab", "remote_to_close")
        self.assertEqual(cap["task"]["direction"], "rtl")
        # 就地→远方 = 向右拨（ltr）只在工厂柜验证过 → lab 查不到能力
        cap = dispatch._capability_for_kind("factory", "close_to_remote")
        self.assertEqual(cap["task"]["direction"], "ltr")
        self.assertIsNone(
            dispatch._capability_for_kind("lab", "close_to_remote"))


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
            capability_url="http://127.0.0.1:18000",
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
                # 拉起前的 18000 可达预检查（真实现会发 HTTP，测试里短路掉）
                mock.patch.object(dispatch, "fetch_snapshot",
                                  return_value={"ok": True}),
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

    def test_spawn_forwards_capability_url_to_reach(self):
        cmd, _task = self._spawn(reg.seed_registry(), calib_status="ready")
        self.assertEqual(cmd[cmd.index("--capability-url") + 1],
                         "http://127.0.0.1:18000")

    def test_spawn_fails_fast_when_capability_center_down(self):
        from core.capability_client import CapabilityUnavailable

        _install_registry(reg.seed_registry())
        args = Namespace(capability_url="http://127.0.0.1:18000")
        with (
            mock.patch.object(dispatch, "_args", args),
            mock.patch.object(
                dispatch, "fetch_snapshot",
                side_effect=CapabilityUnavailable("模拟 18000 挂掉")),
            mock.patch.object(dispatch.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(RuntimeError, "18000"):
                dispatch._spawn_reach({"log": []})
        popen.assert_not_called()


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


class SequenceClaimFilterTests(unittest.TestCase):
    """起手式认领（18000 配置）在选档时的严格过滤。"""

    @staticmethod
    def _client():
        client = mock.Mock()
        client.sequences.return_value = {"sequences": [
            {"name": "0.50-起手式新",
             "file": "0.50-起手式新_20260822_031632.json", "waypoints": []},
            {"name": "0.53-起手式新",
             "file": "0.53-起手式新_20260822_031632.json", "waypoints": []},
        ]}
        return client

    def test_unclaimed_pose_excluded_from_gear_choice(self):
        flow = SwitchFlow(client=self._client(), site="factory",
                          flip_kind="remote_to_close",
                          claimed_pose_names=["0.53-起手式新"])
        # 按距离本应选 0.50 档，但它未被认领 → 落到已认领的 0.53 档
        self.assertEqual(flow.choose_opening_pose(0.53)["name"],
                         "0.53-起手式新")

    def test_nothing_claimed_fails_with_claim_hint(self):
        flow = SwitchFlow(client=self._client(), site="factory",
                          flip_kind="remote_to_close",
                          claimed_pose_names=[])
        with self.assertRaises(FlowError) as ctx:
            flow.choose_opening_pose(0.53)
        self.assertEqual(ctx.exception.code, ErrorCode.POSE_UNAVAILABLE)
        self.assertIn("认领", str(ctx.exception))

    def test_none_claims_keeps_legacy_unfiltered_behavior(self):
        flow = SwitchFlow(client=self._client(), site="factory",
                          flip_kind="remote_to_close",
                          claimed_pose_names=None)
        self.assertEqual(flow.choose_opening_pose(0.53)["name"],
                         "0.50-起手式新")


class WaypointClaimGateTests(unittest.TestCase):
    """位点认领门禁：生效集合外的位点插值直接拒绝。"""

    def test_unclaimed_waypoint_rejected_with_hint(self):
        flow = SwitchFlow(client=mock.Mock(), site="factory",
                          flip_kind="remote_to_close",
                          claimed_waypoint_names=["录制点位1"])
        with self.assertRaises(FlowError) as ctx:
            flow._interp_to_waypoint("未认领位点", "测试")
        self.assertIn("生效位点", str(ctx.exception))

    def test_claimed_waypoint_passes_gate(self):
        client = mock.Mock()
        client.waypoints.return_value = {"waypoints": []}
        flow = SwitchFlow(client=client, site="factory",
                          flip_kind="remote_to_close",
                          claimed_waypoint_names=["录制点位1"])
        with self.assertRaises(FlowError) as ctx:
            flow._interp_to_waypoint("录制点位1", "测试")
        # 过了认领门禁，报的是"找不到路点"（18001 没录）而非认领错误
        self.assertIn("找不到路点", str(ctx.exception))

    def test_none_set_skips_gate(self):
        client = mock.Mock()
        client.waypoints.return_value = {"waypoints": []}
        flow = SwitchFlow(client=client, site="factory",
                          flip_kind="remote_to_close",
                          claimed_waypoint_names=None)
        with self.assertRaises(FlowError) as ctx:
            flow._interp_to_waypoint("任意位点", "测试")
        self.assertIn("找不到路点", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
