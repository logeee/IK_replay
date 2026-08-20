from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.responses import FileResponse

from api import gravity_calibration as gravity


class GravityCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.patches = [
            patch.object(gravity, "DATA_ROOT", root),
            patch.object(gravity, "WAYPOINTS_DIR", root / "waypoints"),
            patch.object(gravity, "RUNS_DIR", root / "runs"),
            patch.object(gravity, "BATCHES_DIR", root / "batches"),
        ]
        for item in self.patches:
            item.start()
        with gravity._lock:
            gravity._plan = None
            gravity._batch = None
            gravity._operation.update(
                phase="idle",
                message="等待选择位点",
                point_id=None,
                plan_id=None,
                run_id=None,
                progress=0.0,
                error=None,
            )

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_root_serves_gravity_dashboard(self):
        response = gravity.page()
        self.assertIsInstance(response, FileResponse)
        self.assertTrue(str(response.path).endswith("web/gravity.html"))
        html = gravity.WEB_DIR.joinpath("gravity.html").read_text(encoding="utf-8")
        self.assertIn("重力补偿标定实验台", html)
        self.assertIn("/api/gravity/execute/", html)
        self.assertIn("PORT 18002", html)

    def test_dashboard_inline_javascript_is_valid(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        html = gravity.WEB_DIR.joinpath("gravity.html").read_text(encoding="utf-8")
        match = re.search(r"<script>(.*)</script>", html, flags=re.DOTALL)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "gravity.js"
            script.write_text(match.group(1), encoding="utf-8")
            result = subprocess.run(
                [node, "--check", str(script)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_waypoint_storage_is_separate_and_records_measured_joints(self):
        def fake_reach(method, path, **_kwargs):
            if path.endswith("/status"):
                return {"robot": "h2", "chain_id": "right_arm"}
            if path.endswith("/joints"):
                return {"ok": True, "named_joints": {"j1": 0.25, "j2": -0.5}}
            raise AssertionError(path)

        with patch.object(gravity, "_request_reach", side_effect=fake_reach):
            result = gravity.save_waypoint({"name": "高位", "note": "静态手型"})

        self.assertTrue(result["ok"])
        point = result["point"]
        self.assertEqual(point["named_joints"]["j2"], -0.5)
        self.assertTrue((gravity.WAYPOINTS_DIR / f"{point['id']}.json").is_file())
        self.assertEqual(gravity._list_points()[0]["name"], "高位")

    def test_aggregate_reports_static_joint_and_torque_error(self):
        samples = [
            {
                "arm": {
                    "cmd_rad": [1.0, 2.0],
                    "measured_rad": [0.9, 1.8],
                    "measured_dq_rad_s": [0.01, -0.02],
                    "tau_est_nm": [4.0, 5.0],
                    "tau_grav_nm": [3.0, 4.0],
                    "estimated_pd_support_nm": [1.0, 2.0],
                    "command_snapshot": {"tau_ff_nm": [3.5, 4.5]},
                }
            },
            {
                "arm": {
                    "cmd_rad": [1.0, 2.0],
                    "measured_rad": [0.8, 1.9],
                    "measured_dq_rad_s": [0.03, -0.04],
                    "tau_est_nm": [4.4, 4.6],
                    "tau_grav_nm": [3.2, 3.8],
                    "estimated_pd_support_nm": [2.0, 1.0],
                    "command_snapshot": {"tau_ff_nm": [3.7, 4.3]},
                }
            },
        ]
        aggregate = gravity._aggregate_samples(samples)
        self.assertEqual(aggregate["measured_rad"]["count"], 2)
        self.assertAlmostEqual(aggregate["gravity_torque_nm"]["mean"][0], 3.1)
        self.assertAlmostEqual(aggregate["measured_velocity_rad_s"]["mean"][0], 0.02)
        self.assertAlmostEqual(aggregate["estimated_joint_torque_nm"]["mean"][0], 4.2)
        self.assertAlmostEqual(aggregate["command_minus_measured_rad"][0], 0.15)

    def test_batch_preserves_selected_order_and_can_skip(self):
        for point_id, name in (("111111111111", "低位"), ("222222222222", "高位")):
            gravity._atomic_json(
                gravity._point_path(point_id),
                {
                    "id": point_id,
                    "name": name,
                    "named_joints": {"j1": 0.0},
                    "order": 1,
                },
            )
        created = gravity.create_batch(
            {
                "point_ids": ["222222222222", "111111111111"],
                "duration_s": 5,
                "settle_s": 2,
                "sample_s": 1,
                "sample_hz": 10,
            }
        )
        self.assertTrue(created["ok"])
        batch = created["batch"]
        self.assertEqual(
            [item["point_name"] for item in batch["items"]], ["高位", "低位"]
        )
        skipped = gravity.skip_batch_item(batch["id"])
        self.assertTrue(skipped["ok"])
        self.assertEqual(skipped["batch"]["items"][0]["status"], "skipped")
        self.assertTrue((gravity.BATCHES_DIR / f"{batch['id']}.json").is_file())

    def test_manual_batch_runs_exactly_one_point_then_waits(self):
        for point_id, name in (("111111111111", "一"), ("222222222222", "二")):
            gravity._atomic_json(
                gravity._point_path(point_id),
                {"id": point_id, "name": name, "named_joints": {"j1": 0.0}},
            )
        batch = gravity.create_batch(
            {"point_ids": ["111111111111", "222222222222"]}
        )["batch"]

        def fake_execute(point_id, _body):
            gravity._set_operation(
                phase="completed",
                point_id=point_id,
                run_id="abababababab",
                error=None,
            )
            return {"ok": True, "run_id": "abababababab"}

        with (
            patch.object(
                gravity,
                "plan_waypoint",
                return_value={"ok": True, "plan": {"id": "cdcdcdcdcdcd"}},
            ),
            patch.object(gravity, "execute_waypoint", side_effect=fake_execute),
        ):
            gravity._run_batch(batch["id"], automatic=False)

        self.assertEqual(gravity._batch["items"][0]["status"], "completed")
        self.assertEqual(gravity._batch["items"][1]["status"], "pending")
        self.assertEqual(gravity._batch["state"], "ready")

    def test_monitor_marks_point_complete_and_persists_samples(self):
        point = {
            "schema_version": 1,
            "id": "a1b2c3d4e5f6",
            "name": "测试点",
            "order": 1,
            "named_joints": {"j1": 0.2},
            "completed_runs": 0,
        }
        gravity._atomic_json(gravity._point_path(point["id"]), point)
        plan = {
            "id": "111122223333",
            "point_id": point["id"],
            "point_name": point["name"],
            "duration_s": 0.0,
        }
        run = {"id": "abcdabcdabcd", "point_id": point["id"], "status": "running"}

        def fake_reach(_method, path, **_kwargs):
            if path.endswith("/exec_status"):
                return {"running": False, "progress": 1.0, "message": "完成（刚性保持）"}
            if path.endswith("/diagnostics"):
                return {
                    "arm": {
                        "armed": True,
                        "cmd_rad": [0.2],
                        "measured_rad": [0.19],
                        "tau_grav_nm": [1.5],
                    }
                }
            raise AssertionError(path)

        with patch.object(gravity, "_request_reach", side_effect=fake_reach):
            gravity._monitor_and_sample(
                plan, run, settle_s=0.0, sample_s=0.06, sample_hz=100.0
            )

        persisted = json.loads(
            (gravity.RUNS_DIR / f"{run['id']}.json").read_text(encoding="utf-8")
        )
        updated = gravity._load_point(point["id"])
        self.assertEqual(persisted["status"], "completed")
        self.assertGreaterEqual(persisted["sample_count"], 3)
        self.assertEqual(updated["completed_runs"], 1)
        self.assertEqual(gravity._operation["phase"], "completed")


if __name__ == "__main__":
    unittest.main()
