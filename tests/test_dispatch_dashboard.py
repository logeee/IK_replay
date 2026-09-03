from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from fastapi.responses import FileResponse

from api import dispatch
from api.flow import SwitchFlow


class DispatchDashboardTests(unittest.TestCase):
    def test_root_serves_workflow_dashboard(self):
        response = dispatch.index()
        self.assertIsInstance(response, FileResponse)
        self.assertTrue(str(response.path).endswith("web/dispatch.html"))
        html = dispatch.WEB_DIR.joinpath("dispatch.html").read_text(encoding="utf-8")
        self.assertIn("拨闸任务流程监控", html)
        self.assertIn('fetch("/task/status"', html)
        self.assertIn("就地 → 远方（向右拨）", html)
        self.assertIn('id="defaultPresetRemoteToClose"', html)
        self.assertIn('id="defaultPresetCloseToRemote"', html)
        self.assertIn('id="firstOffsetY"', html)
        self.assertIn('id="learnFirstRoundDefault"', html)
        self.assertIn('id="pushForce"', html)
        self.assertIn('id="defaultPushForceRemoteToClose"', html)
        self.assertIn('id="defaultPushForceCloseToRemote"', html)
        self.assertIn("first_round_offset_wall_mm_by_kind", html)
        self.assertIn("push_force_n_by_kind", html)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertIn('cache: "no-store"', html)

    def test_defaults_get_keeps_stale_single_preset_page_compatible(self):
        config = {
            "schema_version": 2,
            "defaults": {
                "site": "factory",
                "offset_preset_by_kind": {
                    "remote_to_close": "左拨",
                    "close_to_remote": "右拨",
                },
                "lift_mm": {"base": 10, "step": 10, "max": 30},
            },
            "offset_presets": [],
        }
        with patch.object(dispatch, "_current_defaults", return_value=config):
            response = dispatch.config_defaults_get()

        body = json.loads(response.body)
        self.assertEqual(body["defaults"]["offset_preset"], "左拨")
        self.assertEqual(
            body["defaults"]["offset_preset_by_kind"]["close_to_remote"],
            "右拨",
        )
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")

    def test_stale_single_preset_save_preserves_other_factory_direction(self):
        config = {
            "schema_version": 2,
            "defaults": {
                "site": "factory",
                "offset_preset_by_kind": {
                    "remote_to_close": "旧左拨",
                    "close_to_remote": "保留右拨",
                },
                "lift_mm": {"base": 10, "step": 10, "max": 30},
            },
            "offset_presets": [
                {"name": "新左拨", "offset_mm": {"x": 1, "y": 2, "z": 3}},
                {"name": "保留右拨", "offset_mm": {"x": 4, "y": 5, "z": 6}},
            ],
        }
        with (
            patch.object(dispatch, "_current_defaults", return_value=config),
            patch.object(
                dispatch,
                "save_dispatch_defaults",
                side_effect=lambda payload: payload,
            ) as save,
        ):
            response = dispatch.config_defaults_set({
                "site": "factory",
                "offset_preset": "新左拨",
            })

        self.assertTrue(response["ok"])
        saved = save.call_args.args[0]["defaults"]["offset_preset_by_kind"]
        self.assertEqual(saved["remote_to_close"], "新左拨")
        self.assertEqual(saved["close_to_remote"], "保留右拨")

    def test_dashboard_inline_javascript_is_valid(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        html = dispatch.WEB_DIR.joinpath("dispatch.html").read_text(encoding="utf-8")
        match = re.search(r"<script>(.*)</script>", html, flags=re.DOTALL)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "dispatch.js"
            script.write_text(match.group(1), encoding="utf-8")
            result = subprocess.run(
                [node, "--check", str(script)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_arm_stop_interrupts_flow_then_half_stiffness_resets_and_releases(self):
        flow = SwitchFlow(client=mock.Mock())
        flow._current_pose = {
            "name": "0.50-左-起手式",
            "endpoint_name": "0.50-左-终点",
        }
        task = {
            "state": "running",
            "flow": flow,
            "prompt": {"id": "waiting"},
            "log": [],
        }
        client = mock.Mock()
        client.stop.return_value = {"ok": True}
        client.exec_status.return_value = {
            "ok": True,
            "running": False,
            "message": "完成（刚性保持）",
        }
        client.waypoints.return_value = {
            "waypoints": [
                {
                    "name": "0.50-左-终点",
                    "named_joints": {"joint": 0.5},
                },
                {
                    "name": "起手点测试",
                    "named_joints": {"joint": 0.0},
                },
            ],
        }
        client.joints.return_value = {
            "ok": True,
            "named_joints": {"joint": 0.25},
        }
        client.execute.return_value = {"ok": True}
        client.disarm.return_value = {"ok": True}
        with dispatch._lock:
            original_task = dispatch._task
            dispatch._task = task
        try:
            with (
                patch.object(dispatch, "_reach_alive", return_value=True),
                patch.object(dispatch, "ReachClient", return_value=client),
                patch.object(dispatch._http, "post"),
            ):
                result = dispatch.arm_stop()
        finally:
            with dispatch._lock:
                dispatch._task = original_task

        self.assertTrue(result["ok"])
        self.assertTrue(result["arm_released"])
        self.assertTrue(flow.reset_and_release.is_set())
        self.assertTrue(flow.reset_complete.is_set())
        self.assertIsNone(task["prompt"])
        self.assertEqual(
            result["route"], ["0.50-左-终点", "起手点测试"]
        )
        self.assertEqual(client.execute.call_count, 2)
        for call in client.execute.call_args_list:
            self.assertEqual(call.kwargs["stiffness_scale"], 0.5)
            self.assertEqual(call.kwargs["max_speed_rad_s"], 0.15)
        client.disarm.assert_called_once_with()

    def test_finished_task_is_counted_exactly_once(self):
        with dispatch._lock:
            original = dict(dispatch._task_stats)
            try:
                dispatch._task_stats.update(
                    accepted=1, succeeded=0, failed=0, rejected_busy=0
                )
                task = {"result": {"ok": True}, "stats_counted": False}
                dispatch._count_finished_task_locked(task)
                dispatch._count_finished_task_locked(task)
                self.assertEqual(dispatch._task_stats["succeeded"], 1)
                self.assertEqual(dispatch._task_stats["failed"], 0)
            finally:
                dispatch._task_stats.clear()
                dispatch._task_stats.update(original)

    def test_idle_status_exposes_service_metrics_for_dashboard(self):
        with dispatch._lock:
            original_task = dispatch._task
            original_check = dispatch._check
            dispatch._task = None
            dispatch._check = None
        try:
            with patch.object(dispatch, "_reach_alive", return_value=False):
                status = dispatch.task_status()
        finally:
            with dispatch._lock:
                dispatch._task = original_task
                dispatch._check = original_check
        self.assertEqual(status["state"], "idle")
        self.assertIn("service", status)
        self.assertIn("succeeded", status["service"])
        self.assertEqual(status["log"], [])


if __name__ == "__main__":
    unittest.main()
