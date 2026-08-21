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
            patch.object(gravity, "IK_VALIDATIONS_DIR", root / "ik_validation"),
            patch.object(gravity, "REGULAR_WAYPOINTS_DIR", root / "regular_waypoints"),
            patch.object(gravity, "GRAVITY_PROFILES_PATH", root / "gravity.json"),
        ]
        for item in self.patches:
            item.start()
        with gravity._lock:
            gravity._plan = None
            gravity._batch = None
            gravity._run_cancel.clear()
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
        self.assertIn("理论 / 实测完整机器人姿态对比", html)
        self.assertIn("gravity-viewer.js", html)
        self.assertIn("完整机器人轨迹回放预览", html)
        self.assertIn("gravity-plan-viewer.js", html)
        self.assertIn("planShowCollisions", html)
        self.assertIn("点云IK落点验证", html)
        self.assertIn("/api/gravity/ik_validation", html)

    def test_profile_api_saves_immutable_version_and_activates_it(self):
        result = gravity.save_gravity_profile(
            {
                "version": "0.1.0",
                "label": "第一次标定",
                "description": "测试版本",
                "activate": True,
                "parameters": {
                    "grav_alpha": 0.9,
                    "payload_kg": 0.2,
                    "grav_in_float": True,
                    "use_imu_gravity": False,
                },
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["active_version"], "0.1.0")
        duplicate = gravity.save_gravity_profile(
            {
                "version": "0.1.0",
                "label": "覆盖",
                "parameters": {
                    "grav_alpha": 1.0,
                    "payload_kg": 0.0,
                    "grav_in_float": True,
                    "use_imu_gravity": False,
                },
            }
        )
        self.assertEqual(duplicate.status_code, 400)
        rollback = gravity.activate_gravity_profile("0.0.0")
        self.assertTrue(rollback["ok"])
        self.assertTrue(rollback["applies_on_next_18001_start"])

    def test_runs_are_partitioned_by_version_and_never_moved_on_rollback(self):
        baseline_run = {
            "id": "aaaaaaaaaaaa",
            "status": "completed",
            "started_at": "2026-08-21T09:45:00+08:00",
            "gravity_profile": {"version": "0.0.0"},
        }
        calibrated_run = {
            "id": "bbbbbbbbbbbb",
            "status": "completed",
            "started_at": "2026-08-21T09:46:00+08:00",
            "gravity_profile": {"version": "0.1.0"},
        }
        gravity._save_run(baseline_run)
        gravity._save_run(calibrated_run)
        baseline_path = gravity.RUNS_DIR / "0.0.0" / "aaaaaaaaaaaa.json"
        calibrated_path = gravity.RUNS_DIR / "0.1.0" / "bbbbbbbbbbbb.json"
        self.assertTrue(baseline_path.is_file())
        self.assertTrue(calibrated_path.is_file())

        gravity.activate_gravity_profile("0.0.0")

        self.assertTrue(baseline_path.is_file())
        self.assertTrue(calibrated_path.is_file())
        self.assertEqual(
            {run["storage_version"] for run in gravity._list_runs()},
            {"0.0.0", "0.1.0"},
        )

    def test_ik_metrics_separate_solver_tracking_and_total_error(self):
        execution = {
            "tcp": {
                "pick_target_root": [0.4, -0.2, 0.8],
                "planned_root": [0.401, -0.198, 0.8],
            },
            "pick_context": {"p_root": [0.4, -0.2, 0.8]},
        }
        aggregate = {
            "tcp_measured_root_m": {
                "mean": [0.398, -0.197, 0.796],
            }
        }

        metrics = gravity._ik_error_metrics(execution, aggregate)

        self.assertAlmostEqual(metrics["ik"]["norm_mm"], (5.0 ** 0.5))
        self.assertAlmostEqual(metrics["tracking"]["norm_mm"], (26.0 ** 0.5))
        self.assertAlmostEqual(metrics["total"]["norm_mm"], (29.0 ** 0.5))
        for actual, expected in zip(
            metrics["tracking"]["delta_mm"], [-3.0, 1.0, -4.0]
        ):
            self.assertAlmostEqual(actual, expected)

    def test_ik_validation_is_partitioned_by_gravity_version(self):
        record = {
            "id": "abababababab",
            "execution_id": "cdcdcdcdcdcd",
            "started_at": "2026-08-21T16:00:00",
            "gravity_profile": {"version": "0.1.0"},
        }

        gravity._save_ik_validation(record)

        path = gravity.IK_VALIDATIONS_DIR / "0.1.0" / "abababababab.json"
        self.assertTrue(path.is_file())
        listed = gravity._list_ik_validations()
        self.assertEqual(listed[0]["storage_version"], "0.1.0")
        self.assertEqual(
            gravity._load_ik_validation("abababababab")["execution_id"],
            "cdcdcdcdcdcd",
        )

    def test_capture_ik_validation_checks_pointcloud_execution_and_saves_samples(self):
        names = [
            "right_shoulder_pitch_joint",
            "right_shoulder_roll_joint",
            "right_shoulder_yaw_joint",
            "right_elbow_joint",
            "right_wrist_roll_joint",
            "right_wrist_pitch_joint",
            "right_wrist_yaw_joint",
        ]
        target = [0.2, -0.25, 0.0, 0.9, 0.0, -0.1, 0.0]
        execution = {
            "id": "edededededed",
            "result": "done",
            "segment": "主轨迹",
            "robot": "h2",
            "chain_id": "right_arm",
            "joint_names": names,
            "target_rad": target,
            "gravity_profile": {"version": "0.1.0"},
            "pick_context": {
                "selection_mode": "frozen_rgbd_pointcloud",
                "p_root": [0.4, -0.2, 0.8],
                "pixel": [300, 200],
            },
            "tcp": {
                "pick_target_root": [0.4, -0.2, 0.8],
                "planned_root": [0.401, -0.2, 0.8],
            },
        }
        samples = [
            {
                "arm": {
                    "armed": True,
                    "joint_names": names,
                    "cmd_rad": target,
                    "measured_rad": [value - 0.01 for value in target],
                    "tcp_cmd_root_m": [0.401, -0.2, 0.8],
                    "tcp_measured_root_m": [0.399, -0.2, 0.796],
                }
            }
            for _ in range(5)
        ]
        aggregate = gravity._aggregate_samples(samples)

        def request(method, path, **kwargs):
            if path == "/api/reach/executions/edededededed":
                return {"ok": True, "execution": execution}
            raise AssertionError(path)

        with (
            patch.object(gravity, "_request_reach", side_effect=request),
            patch.object(
                gravity,
                "_sample_ik_execution",
                return_value=(samples, aggregate),
            ),
            patch.object(
                gravity,
                "_reach_status",
                return_value={
                    "gravity_profile": {"version": "0.1.0"},
                    "p_tool": [0.3, 0.0, 0.0],
                },
            ),
        ):
            response = gravity.capture_ik_validation(
                "edededededed",
                {
                    "start_label": "0.5以上",
                    "sample_s": 2.0,
                    "sample_hz": 10.0,
                },
            )

        self.assertTrue(response["ok"])
        saved = gravity._load_ik_validation(response["validation"]["id"])
        self.assertEqual(saved["start_label"], "0.5以上")
        self.assertEqual(saved["sample_count"], 5)
        self.assertAlmostEqual(saved["metrics"]["ik"]["norm_mm"], 1.0)
        self.assertGreater(saved["metrics"]["tracking"]["norm_mm"], 4.0)
        comparison_response = gravity.ik_validation_comparison(saved["id"])
        self.assertTrue(comparison_response["ok"])
        comparison = comparison_response["comparison"]
        self.assertEqual(comparison["validation_kind"], "pointcloud_ik")
        self.assertEqual(len(comparison["joint_error_deg"]), 7)
        self.assertAlmostEqual(
            comparison["error_breakdown"]["total"]["norm_mm"],
            saved["metrics"]["total"]["norm_mm"],
        )
        duplicate = gravity.capture_ik_validation("edededededed", {})
        self.assertEqual(duplicate.status_code, 400)

    def test_ik_sampling_rejects_when_current_command_left_theoretical_goal(self):
        execution = {"target_rad": [0.0, 0.0]}

        def request(method, path, **kwargs):
            if path == "/api/reach/exec_status":
                return {"running": False}
            if path == "/api/reach/diagnostics":
                return {
                    "arm": {
                        "armed": True,
                        "cmd_rad": [0.2, 0.0],
                    }
                }
            raise AssertionError(path)

        with patch.object(gravity, "_request_reach", side_effect=request):
            with self.assertRaisesRegex(gravity.GravityServiceError, "已不是"):
                gravity._sample_ik_execution(
                    execution,
                    sample_s=1.0,
                    sample_hz=10.0,
                    command_tolerance_rad=0.05,
                )

    def test_pose_comparison_returns_dual_fk_links_and_tcp_error(self):
        names = [
            "right_shoulder_pitch_joint",
            "right_shoulder_roll_joint",
            "right_shoulder_yaw_joint",
            "right_elbow_joint",
            "right_wrist_roll_joint",
            "right_wrist_pitch_joint",
            "right_wrist_yaw_joint",
        ]
        command = [0.2, -0.25, 0.0, 0.9, 0.0, -0.1, 0.0]
        measured = [0.18, -0.24, 0.01, 0.87, 0.0, -0.09, 0.0]
        run = {
            "id": "cccccccccccc",
            "point_name": "对比点",
            "robot": "h2",
            "chain_id": "right_arm",
            "gravity_profile": {"version": "0.0.0"},
            "tool_visualization": {
                "tcp_offset": [0.3, 0.01, 0.04],
                "markers": {"red": [0.28, 0.0, 0.04]},
                "wrist_link": "right_wrist_yaw_link",
            },
            "sample_points": [
                {
                    "index": 1,
                    "type": "final",
                    "trajectory_fraction": 1.0,
                    "sample_count": 10,
                    "planned_named_joints": dict(zip(names, command)),
                    "samples": [{"arm": {"joint_names": names}}],
                    "aggregate": {
                        "command_rad": {"mean": command},
                        "measured_rad": {"mean": measured},
                        "tcp_command_root_m": {"mean": [0.4, -0.2, 0.8]},
                        "tcp_measured_root_m": {"mean": [0.39, -0.2, 0.78]},
                    },
                }
            ],
        }
        gravity._save_run(run)

        response = gravity.run_comparison(run["id"], sample_index=1)

        self.assertTrue(response["ok"])
        comparison = response["comparison"]
        self.assertEqual(len(comparison["joint_error_deg"]), 7)
        self.assertGreaterEqual(len(comparison["theoretical"]["links"]), 8)
        for actual, expected in zip(
            comparison["tcp_delta_mm"], [-10.0, 0.0, -20.0]
        ):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(comparison["tcp_error_mm"], 22.3606798)
        self.assertEqual(
            comparison["tool_visualization"]["wrist_link"],
            "right_wrist_yaw_link",
        )

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

    def test_comparison_viewer_javascript_is_valid(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, "--check", str(gravity.WEB_DIR / "gravity-viewer.js")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        viewer_source = (gravity.WEB_DIR / "gravity-viewer.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("STLLoader", viewer_source)
        self.assertIn("comparePrevSample", viewer_source)
        self.assertIn("compareNextSample", viewer_source)
        plan_result = subprocess.run(
            [node, "--check", str(gravity.WEB_DIR / "gravity-plan-viewer.js")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(plan_result.returncode, 0, plan_result.stderr)

    def test_plan_preview_exposes_robot_and_all_joint_frames(self):
        with gravity._lock:
            gravity._plan = {
                "id": "dddddddddddd",
                "point_id": "eeeeeeeeeeee",
                "point_name": "预览点",
                "robot": "h2",
                "chain_id": "right_arm",
                "duration_s": 5.0,
                "planner": "linear",
                "collision": {"status": "free"},
                "waypoints": [{"j1": 0.0}, {"j1": 0.2}, {"j1": 0.4}],
                "preview": {"sample_fractions": [0.5, 1.0]},
                "tool_visualization": {
                    "tcp_offset": [0.3, 0.01, 0.04],
                    "markers": {
                        "red": [0.28, 0.0, 0.04],
                        "blue": [0.28, 0.02, 0.04],
                    },
                    "wrist_link": "right_wrist_yaw_link",
                },
            }
        response = gravity.gravity_plan_preview("dddddddddddd")
        self.assertTrue(response["ok"])
        self.assertEqual(response["plan"]["robot"], "h2")
        self.assertEqual(len(response["plan"]["frames"]), 3)
        self.assertEqual(response["plan"]["sample_fractions"], [0.5, 1.0])
        self.assertIn("red", response["plan"]["tool_visualization"]["markers"])
        self.assertEqual(
            response["plan"]["tool_visualization"]["tcp_offset"],
            [0.3, 0.01, 0.04],
        )

    def test_collision_blocked_plan_is_retained_for_preview(self):
        point_id = "ffffffffffff"
        gravity._atomic_json(
            gravity._point_path(point_id),
            {
                "id": point_id,
                "name": "碰撞测试点",
                "robot": "h2",
                "chain_id": "right_arm",
                "named_joints": {"j1": 0.4},
            },
        )
        collision = {
            "status": "collision",
            "rrt_error": "终点姿态本身撞障",
            "checks": [
                {
                    "index": 1,
                    "status": "collision",
                    "min_distance_m": -0.01,
                    "min_distance_mm": -10.0,
                    "pair": {"a": "arm", "b": "torso"},
                    "shapes": {},
                }
            ],
        }

        def fake_reach(_method, path, **_kwargs):
            if path.endswith("/status"):
                return {
                    "robot": "h2",
                    "chain_id": "right_arm",
                    "p_tool": [0.3, 0.0, 0.0],
                }
            if path.endswith("/joints"):
                return {"named_joints": {"j1": 0.0}}
            return {
                "planner": "linear",
                "collision": collision,
                "waypoints": [
                    {"named_joints": {"j1": 0.0}},
                    {"named_joints": {"j1": 0.4}},
                ],
            }

        with patch.object(gravity, "_request_reach", side_effect=fake_reach):
            response = gravity.plan_waypoint(point_id, {"steps": 40})

        self.assertEqual(response.status_code, 409)
        self.assertTrue(gravity._plan["blocked"])
        preview = gravity.gravity_plan_preview(gravity._plan["id"])
        self.assertTrue(preview["plan"]["blocked"])
        self.assertEqual(
            preview["plan"]["collision"]["checks"][0]["min_distance_mm"],
            -10.0,
        )

    def test_robot_metadata_serves_urdf_for_full_preview(self):
        response = gravity.gravity_robot_metadata("h2")
        self.assertTrue(response["ok"])
        self.assertEqual(response["metadata"]["active_robot"], "h2")
        self.assertTrue(response["metadata"]["robot"]["urdf_url"].endswith("robot.urdf"))

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

    def test_regular_waypoints_can_be_imported_once_without_modifying_source(self):
        gravity.REGULAR_WAYPOINTS_DIR.mkdir(parents=True)
        source_path = gravity.REGULAR_WAYPOINTS_DIR / "起手点_20260821.json"
        source_payload = {
            "name": "起手点",
            "chain_id": "right_arm",
            "named_joints": {"j1": 0.25, "j2": -0.5},
            "created_at": "2026-08-21 09:00:00",
        }
        source_path.write_text(
            json.dumps(source_payload, ensure_ascii=False), encoding="utf-8"
        )

        available = gravity.importable_waypoints()
        self.assertEqual(available["available_count"], 1)
        imported = gravity.import_waypoints(
            {"files": [source_path.name], "name_prefix": "旧库-"}
        )

        self.assertTrue(imported["ok"])
        self.assertEqual(imported["imported_count"], 1)
        point = imported["imported"][0]
        self.assertEqual(point["name"], "旧库-起手点")
        self.assertEqual(point["source_waypoint_file"], source_path.name)
        self.assertEqual(point["named_joints"]["j2"], -0.5)
        self.assertEqual(
            json.loads(source_path.read_text(encoding="utf-8")), source_payload
        )

        duplicate = gravity.import_waypoints({"files": [source_path.name]})
        self.assertEqual(duplicate["imported_count"], 0)
        self.assertEqual(duplicate["skipped_count"], 1)
        self.assertTrue(
            gravity.importable_waypoints()["waypoints"][0]["already_imported"]
        )

    def test_waypoint_import_rejects_path_traversal(self):
        response = gravity.import_waypoints({"files": ["../secret.json"]})
        self.assertEqual(response.status_code, 400)

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

    def test_plan_is_split_into_overlapping_static_sample_segments(self):
        waypoints = [{"j1": float(index)} for index in range(9)]
        segments = gravity._split_plan_waypoints(waypoints, 3)
        self.assertEqual(len(segments), 4)
        self.assertEqual(
            [segment["end_index"] for segment in segments], [2, 4, 6, 8]
        )
        self.assertEqual(segments[1]["waypoints"][0], segments[0]["waypoints"][-1])
        self.assertTrue(segments[-1]["final"])

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
            "max_speed_rad_s": 0.2,
            "waypoints": [{"j1": 0.0}, {"j1": 0.2}],
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
                plan,
                run,
                settle_s=0.0,
                sample_s=0.06,
                sample_hz=100.0,
                cancel_event=gravity._run_cancel,
            )

        persisted = json.loads(
            gravity._run_path(run).read_text(encoding="utf-8")
        )
        updated = gravity._load_point(point["id"])
        self.assertEqual(persisted["status"], "completed")
        self.assertGreaterEqual(persisted["sample_count"], 3)
        self.assertEqual(len(persisted["sample_points"]), 1)
        self.assertEqual(persisted["sample_points"][0]["type"], "final")
        self.assertEqual(updated["completed_runs"], 1)
        self.assertEqual(gravity._operation["phase"], "completed")

    def test_intermediate_samples_restart_from_each_held_segment(self):
        point = {
            "id": "123456789abc",
            "name": "分段点",
            "named_joints": {"j1": 0.3},
            "completed_runs": 0,
        }
        gravity._atomic_json(gravity._point_path(point["id"]), point)
        plan = {
            "id": "111122223333",
            "point_id": point["id"],
            "point_name": point["name"],
            "duration_s": 3.0,
            "max_speed_rad_s": 0.2,
            "waypoints": [{"j1": 0.0}, {"j1": 0.1}, {"j1": 0.2}, {"j1": 0.3}],
        }
        run = {"id": "987654321abc", "point_id": point["id"], "status": "running"}
        started_segments = []

        def fake_reach(method, path, **kwargs):
            if path.endswith("/exec_status"):
                return {"running": False, "progress": 1.0, "message": "完成（刚性保持）"}
            if path.endswith("/diagnostics"):
                return {
                    "arm": {
                        "armed": True,
                        "cmd_rad": [0.1],
                        "measured_rad": [0.1],
                        "tau_grav_nm": [1.0],
                    }
                }
            if method == "POST" and path.endswith("/execute"):
                started_segments.append(kwargs["body"]["waypoints"])
                return {"ok": True, "running": True, "message": "执行中"}
            raise AssertionError((method, path))

        with patch.object(gravity, "_request_reach", side_effect=fake_reach):
            gravity._monitor_and_sample(
                plan,
                run,
                settle_s=0.0,
                sample_s=0.04,
                sample_hz=100.0,
                intermediate_stops=2,
                cancel_event=gravity._run_cancel,
            )

        persisted = json.loads(
            gravity._run_path(run).read_text(encoding="utf-8")
        )
        self.assertEqual(len(persisted["sample_points"]), 3)
        self.assertEqual(len(started_segments), 2)
        self.assertEqual(started_segments[0][0], {"j1": 0.1})
        self.assertEqual(started_segments[1][-1], {"j1": 0.3})

    def test_new_execution_replaces_cancelled_event(self):
        point_id = "123456789abc"
        with gravity._lock:
            gravity._plan = {
                "id": "111122223333",
                "point_id": point_id,
                "point_name": "停止后重试",
                "duration_s": 2.0,
                "max_speed_rad_s": 0.2,
                "intermediate_stops": 0,
                "planner": "linear",
                "waypoints": [{"j1": 0.0}, {"j1": 0.2}],
            }
            gravity._operation["phase"] = "error"
            previous_event = gravity._run_cancel
            previous_event.set()

        thread_call = {}

        class FakeThread:
            def __init__(self, **kwargs):
                thread_call.update(kwargs)

            def start(self):
                thread_call["started"] = True

        with (
            patch.object(
                gravity,
                "_reach_status",
                return_value={
                    "armed": True,
                    "hand_move": False,
                    "exec": {"running": False},
                    "gravity_profile": {},
                },
            ),
            patch.object(
                gravity,
                "_request_reach",
                return_value={"running": True, "message": "执行中"},
            ),
            patch.object(gravity.threading, "Thread", FakeThread),
        ):
            result = gravity.execute_waypoint(
                point_id,
                {
                    "confirm": True,
                    "plan_id": "111122223333",
                    "intermediate_stops": 0,
                },
            )

        self.assertTrue(result["ok"])
        self.assertIsNot(gravity._run_cancel, previous_event)
        self.assertFalse(gravity._run_cancel.is_set())
        self.assertIs(thread_call["kwargs"]["cancel_event"], gravity._run_cancel)
        self.assertTrue(thread_call["started"])


if __name__ == "__main__":
    unittest.main()
