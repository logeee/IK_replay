from __future__ import annotations

import math
import unittest
from unittest import mock

from adapters.reach import execution as reach_execution
from api import dispatch
from api.flow import SwitchFlow, resolve_flip_intent


class _YoloSequence:
    def __init__(self, *scenes: str):
        self._scenes = list(scenes)

    def scene(self, **_kwargs):
        scene = self._scenes.pop(0)
        return {"ok": True, "scene": scene, "conf": 0.9, "boxes": []}


class FlipIntentTests(unittest.TestCase):
    def test_factory_states_and_physical_directions_are_symmetric(self):
        left = resolve_flip_intent("factory", "remote_to_close")
        right = resolve_flip_intent("factory", "close_to_remote")

        self.assertEqual(
            (left["flip_from"], left["flip_to"], left["direction"]),
            ("远方", "就地", "rtl"),
        )
        self.assertEqual(
            (right["flip_from"], right["flip_to"], right["direction"]),
            ("就地", "远方", "ltr"),
        )

    def test_factory_constructor_applies_direction_to_distance_sign(self):
        left = SwitchFlow(
            client=mock.Mock(),
            site="factory",
            flip_kind="remote_to_close",
            sidestep_cm=10,
        )
        right = SwitchFlow(
            client=mock.Mock(),
            site="factory",
            flip_kind="close_to_remote",
            sidestep_cm=10,
        )

        self.assertEqual(left.sidestep_cm, 10.0)
        self.assertEqual(right.sidestep_cm, -10.0)
        self.assertEqual(left.push_force_n, 15.0)
        self.assertEqual(right.push_force_n, 15.0)

    def test_rightward_flip_selects_left_prefixed_opening_pose(self):
        client = mock.Mock()
        client.sequences.return_value = {
            "sequences": [
                {
                    "name": "0.50-起手式新",
                    "file": "0.50-起手式新_20260822_031632.json",
                    "waypoints": [
                        "录制点位1_20260726_151627.json",
                        "0.50-起手式新终点_20260822_031632.json",
                    ],
                },
                {
                    "name": "0.50-左-起手式",
                    "file": "0.50-左-起手式_20260826_134700.json",
                    "waypoints": [
                        "录制点位1_20260726_151627.json",
                        "0.50-左-终点_20260826_134700.json",
                    ],
                },
            ],
        }
        rightward = SwitchFlow(
            client=client,
            site="factory",
            flip_kind="close_to_remote",
        )
        leftward = SwitchFlow(
            client=client,
            site="factory",
            flip_kind="remote_to_close",
        )

        right_pose = rightward.choose_opening_pose(0.53)
        left_pose = leftward.choose_opening_pose(0.53)

        self.assertEqual(right_pose["name"], "0.50-左-起手式")
        self.assertEqual(right_pose["endpoint_name"], "0.50-左-终点")
        self.assertEqual(left_pose["name"], "0.50-起手式新")
        self.assertEqual(left_pose["endpoint_name"], "0.50-起手式新终点")

    def test_opening_pose_distance_is_rounded_to_nearest_higher_tie(self):
        client = mock.Mock()
        client.sequences.return_value = {
            "sequences": [
                {
                    "name": f"{distance:.2f}-左-起手式",
                    "file": f"{distance:.2f}-左-起手式_20260826_144000.json",
                    "waypoints": [],
                }
                for distance in (0.45, 0.46)
            ],
        }
        flow = SwitchFlow(
            client=client,
            site="factory",
            flip_kind="close_to_remote",
        )

        self.assertEqual(
            flow.choose_opening_pose(0.486)["name"],
            "0.46-左-起手式",
        )
        self.assertEqual(
            flow.choose_opening_pose(0.485)["name"],
            "0.46-左-起手式",
        )

    def test_retry_uses_selected_left_pose_endpoint(self):
        flow = SwitchFlow(
            client=mock.Mock(),
            site="factory",
            flip_kind="close_to_remote",
        )
        flow._current_pose = {
            "name": "0.50-左-起手式",
        }
        flow._interp_to_waypoint = mock.Mock()

        flow._goto_endpoint("重试回位")

        flow._interp_to_waypoint.assert_called_once_with(
            "0.50-左-终点",
            "重试回位",
        )

    def test_left_start_pose_descends_via_endpoint_before_release(self):
        client = mock.Mock()
        client.disarm.return_value = {"ok": True}
        flow = SwitchFlow(client=client)
        pose = {
            "name": "0.50-左-起手式",
            "endpoint_name": "0.50-左-终点",
        }
        flow._current_pose = pose
        flow._interp_to_waypoint = mock.Mock()
        flow._log = mock.Mock()

        flow.descend_fast(pose)

        self.assertEqual(
            flow._interp_to_waypoint.call_args_list,
            [
                mock.call("0.50-左-终点", "收尾第一段"),
                mock.call(
                    flow.DESCEND_WAYPOINT,
                    "收尾",
                    speed_rad_s=flow.DESCEND_SPEED_RAD_S,
                ),
            ],
        )
        client.disarm.assert_called_once_with()

    def test_failed_left_pose_cleanup_uses_same_safe_route(self):
        client = mock.Mock()
        client.disarm.return_value = {"ok": True}
        flow = SwitchFlow(client=client)
        flow._current_pose = {
            "name": "0.50-左-起手式",
            "endpoint_name": "0.50-左-终点",
        }
        flow._arm_moved = True
        flow._armed_by_flow = True
        flow._interp_to_waypoint = mock.Mock()
        flow._log = mock.Mock()

        flow._descend_on_failure()

        self.assertEqual(
            flow._interp_to_waypoint.call_args_list,
            [
                mock.call("0.50-左-终点", "失败收尾第一段"),
                mock.call(
                    flow.DESCEND_WAYPOINT,
                    "失败收尾",
                    speed_rad_s=flow.DESCEND_SPEED_RAD_S,
                ),
            ],
        )
        client.disarm.assert_called_once_with()

    def test_left_cleanup_does_not_release_if_endpoint_return_fails(self):
        client = mock.Mock()
        flow = SwitchFlow(client=client)
        flow._current_pose = {
            "name": "0.50-左-起手式",
            "endpoint_name": "0.50-左-终点",
        }
        flow._arm_moved = True
        flow._armed_by_flow = True
        flow._interp_to_waypoint = mock.Mock(
            side_effect=RuntimeError("终点不可达")
        )
        flow._log = mock.Mock()

        flow._descend_on_failure()

        flow._interp_to_waypoint.assert_called_once_with(
            "0.50-左-终点", "失败收尾第一段"
        )
        client.disarm.assert_not_called()

    def test_pick_stability_uses_only_waist_and_imu_not_wall(self):
        client = mock.Mock()
        client.torso.return_value = {
            "ok": True,
            "waist_names": ["waist_yaw", "waist_roll", "waist_pitch"],
            "waist_rad": [0.1, -0.05, 0.02],
            "imu_rpy": [0.01, -0.02, 0.03],
        }
        flow = SwitchFlow(client=client)
        flow._log = mock.Mock()

        with mock.patch("api.flow.time.sleep"):
            flow._wait_robot_stable()

        self.assertEqual(flow.WAIST_STABLE_MAX_RANGE_DEG, 0.03)
        self.assertEqual(flow.IMU_STABLE_MAX_RANGE_DEG, 0.03)
        self.assertEqual(flow.WAIST_STABLE_TIMEOUT_S, 20.0)
        self.assertGreaterEqual(client.torso.call_count, 2)
        client.perpendicular.assert_not_called()
        client.joints.assert_not_called()

    def test_stable_waist_does_not_pass_while_imu_is_moving(self):
        client = mock.Mock()
        sample_count = 0

        def torso():
            nonlocal sample_count
            sample_count += 1
            # 0.0006 rad ≈ 0.034°：应被新的 0.03° 阈值拦住。
            imu_pitch = 0.0006 if sample_count <= 7 and sample_count % 2 else 0.0
            return {
                "ok": True,
                "waist_names": ["waist_yaw", "waist_roll", "waist_pitch"],
                "waist_rad": [0.1, -0.05, 0.02],
                "imu_rpy": [0.0, imu_pitch, 0.0],
            }

        client.torso.side_effect = torso
        flow = SwitchFlow(client=client)
        flow._log = mock.Mock()

        with mock.patch("api.flow.time.sleep"):
            flow._wait_robot_stable()

        self.assertGreater(client.torso.call_count, 7)

    def test_torso_endpoint_exposes_waist_and_imu_together(self):
        torso = {
            "waist_names": ["waist_yaw", "waist_roll", "waist_pitch"],
            "waist_rad": [0.1, 0.2, 0.3],
            "imu_rpy": [0.01, 0.02, 0.03],
        }
        with mock.patch.object(reach_execution, "_read_torso", return_value=torso):
            result = reach_execution.reach_torso()

        self.assertTrue(result["ok"])
        self.assertEqual(result["waist_rad"], torso["waist_rad"])
        self.assertEqual(result["imu_rpy"], torso["imu_rpy"])

    def test_scene_mismatch_is_archived_for_future_training(self):
        pointcloud = mock.Mock()
        pointcloud.capture.return_value = {
            "ok": True,
            "capture_id": "capture-mismatch",
        }
        pointcloud.auto_target.return_value = {
            "ok": True,
            "matched_detection_name": "就地",
            "target_point_slot": 1,
            "panel_center_wall_m": [0.0, 0.0, 0.0],
            "target_wall_m": [0.01, 0.02, 0.03],
        }
        pointcloud.save_scene_mismatch.return_value = {
            "ok": True,
            "image": "data/training_samples/scene_mismatch/sample.jpg",
        }
        flow = SwitchFlow(
            client=mock.Mock(),
            pointcloud=pointcloud,
            site="factory",
            flip_kind="remote_to_close",
        )
        flow._log = mock.Mock()

        picked, error = flow._pointcloud_pick_once(1)

        self.assertIsNone(picked)
        self.assertIn("「就地」", error)
        pointcloud.save_scene_mismatch.assert_called_once_with(
            "capture-mismatch",
            {
                "observed_scene": "就地",
                "expected_scene": "远方",
                "site": "factory",
                "flip_kind": "remote_to_close",
                "direction": "rtl",
                "round": 1,
                "attempt": 1,
            },
        )

    def test_first_round_wall_offset_is_additive_and_then_stops(self):
        pointcloud = mock.Mock()
        pointcloud.capture.side_effect = [
            {"ok": True, "capture_id": "round-1"},
            {"ok": True, "capture_id": "round-2"},
        ]
        pointcloud.auto_target.return_value = {
            "ok": True,
            "matched_detection_name": "远方",
            "target_point_slot": 1,
            "panel_center_wall_m": [0.0, 0.0, 0.0],
            "target_wall_m": [0.01, 0.02, 0.03],
            "target_camera_m": [1.0, 2.0, 3.0],
            "wall_axes_camera": [
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
        }
        pointcloud.confirm.return_value = {
            "ok": True,
            "p_root": [1.0, 2.0, 3.0],
        }
        flow = SwitchFlow(
            client=mock.Mock(),
            pointcloud=pointcloud,
            site="factory",
            flip_kind="remote_to_close",
            target_offset_wall_m=(0.01, 0.02, 0.03),
            first_round_offset_wall_m=(0.004, 0.005, 0.006),
        )
        flow._log = mock.Mock()

        flow._pointcloud_pick_once(1, round_no=1)
        flow._pointcloud_pick_once(1, round_no=2)

        first = pointcloud.confirm.call_args_list[0].args[1]
        second = pointcloud.confirm.call_args_list[1].args[1]
        self.assertEqual(
            first["adjustment_wall_mm"],
            {"x": 14.0, "y": 25.0, "z": 36.0},
        )
        self.assertEqual(
            first["first_round_adjustment_wall_mm"],
            {"x": 4.0, "y": 5.0, "z": 6.0},
        )
        self.assertEqual(
            second["adjustment_wall_mm"],
            {"x": 10.0, "y": 20.0, "z": 30.0},
        )
        self.assertEqual(
            second["first_round_adjustment_wall_mm"],
            {"x": 0.0, "y": 0.0, "z": 0.0},
        )

    def test_cabinet_axis_direction_mirrors_x_but_keeps_downward_tilt(self):
        plane = {
            "left_root": [-1.0, 0.0, 0.0],
            "right_root": [1.0, 0.0, 0.0],
            "wall_up_root": [0.0, 0.0, 1.0],
        }
        left = SwitchFlow(
            client=mock.Mock(),
            site="factory",
            flip_kind="remote_to_close",
        )._sidestep_direction(plane)
        right = SwitchFlow(
            client=mock.Mock(),
            site="factory",
            flip_kind="close_to_remote",
        )._sidestep_direction(plane)

        cosine = math.cos(math.radians(15))
        downward = -math.sin(math.radians(15))
        self.assertAlmostEqual(left[0], -cosine)
        self.assertAlmostEqual(right[0], cosine)
        self.assertAlmostEqual(left[2], downward)
        self.assertAlmostEqual(right[2], downward)

    def test_detect_scene_uses_requested_target_and_direction(self):
        already_done = SwitchFlow(
            client=mock.Mock(),
            yolo=_YoloSequence("远方"),
            site="factory",
            flip_kind="close_to_remote",
        )
        needs_flip = SwitchFlow(
            client=mock.Mock(),
            yolo=_YoloSequence("就地"),
            site="factory",
            flip_kind="close_to_remote",
        )

        self.assertFalse(already_done.detect_scene()["need_flip"])
        result = needs_flip.detect_scene()
        self.assertTrue(result["need_flip"])
        self.assertEqual(result["direction"], "ltr")

    def test_verify_flip_accepts_requested_target(self):
        flow = SwitchFlow(
            client=mock.Mock(),
            yolo=_YoloSequence("远方"),
            site="factory",
            flip_kind="close_to_remote",
        )
        flow._save_flip_evidence = mock.Mock()

        self.assertTrue(flow.verify_flip())
        flow._save_flip_evidence.assert_called_once()
        self.assertTrue(flow._save_flip_evidence.call_args.kwargs["success"])

    def test_verify_flip_rejects_source_state_after_second_look(self):
        flow = SwitchFlow(
            client=mock.Mock(),
            yolo=_YoloSequence("就地", "就地"),
            site="factory",
            flip_kind="close_to_remote",
        )
        flow._save_flip_evidence = mock.Mock()

        with mock.patch("api.flow.time.sleep"):
            self.assertFalse(flow.verify_flip())
        self.assertFalse(flow._save_flip_evidence.call_args.kwargs["success"])


class FactoryDispatchTests(unittest.TestCase):
    def test_factory_accepts_both_directions(self):
        self.assertTrue(dispatch._kind_supported("factory", "remote_to_close"))
        self.assertTrue(dispatch._kind_supported("factory", "close_to_remote"))
        self.assertFalse(dispatch._kind_supported("lab", "remote_to_close"))

    def test_direction_specific_default_offsets_are_resolved(self):
        defaults = {
            "defaults": {
                "offset_preset_by_kind": {
                    "remote_to_close": "左拨",
                    "close_to_remote": "右拨",
                },
                "first_round_offset_wall_mm_by_kind": {
                    "remote_to_close": {"x": 1, "y": 11, "z": 2},
                    "close_to_remote": {"x": -3, "y": 12, "z": 4},
                },
            },
            "offset_presets": [
                {"name": "左拨", "offset_mm": {"x": 5, "y": 1, "z": -2}},
                {"name": "右拨", "offset_mm": {"x": -6, "y": 2, "z": -3}},
            ],
        }

        left, _ = dispatch._resolve_offset(None, defaults, "remote_to_close")
        right, _ = dispatch._resolve_offset(None, defaults, "close_to_remote")
        explicit, source = dispatch._resolve_offset(
            {"target_offset_wall_mm": {"x": 1, "y": 2, "z": 3}},
            defaults,
            "close_to_remote",
        )
        first_left, _ = dispatch._resolve_first_round_offset(
            None, defaults, "remote_to_close"
        )
        first_right, _ = dispatch._resolve_first_round_offset(
            None, defaults, "close_to_remote"
        )

        self.assertEqual(left, (5, 1, -2))
        self.assertEqual(right, (-6, 2, -3))
        self.assertEqual(first_left, (1.0, 11.0, 2.0))
        self.assertEqual(first_right, (-3.0, 12.0, 4.0))
        self.assertEqual(explicit, (1.0, 2.0, 3.0))
        self.assertEqual(source, "请求指定")

    def test_factory_close_to_remote_task_reaches_worker(self):
        defaults = {
            "schema_version": 3,
            "defaults": {
                "site": "factory",
                "offset_preset_by_kind": {
                    "remote_to_close": "",
                    "close_to_remote": "",
                },
                "first_round_offset_wall_mm_by_kind": {
                    "remote_to_close": {"x": 0, "y": 0, "z": 0},
                    "close_to_remote": {"x": 0, "y": 7, "z": 0},
                },
                "lift_mm": {"base": 0, "step": 0, "max": 30},
            },
            "offset_presets": [],
        }
        with dispatch._lock:
            original_task = dispatch._task
            original_check = dispatch._check
            original_stats = dict(dispatch._task_stats)
            dispatch._task = None
            dispatch._check = None
        try:
            with (
                mock.patch.object(dispatch, "_current_defaults",
                                  return_value=defaults),
                mock.patch.object(dispatch.threading, "Thread") as thread,
            ):
                result = dispatch.task_submit({
                    "language": "Change the switch from close to remote",
                    "site": "factory",
                })
            self.assertTrue(result["ok"])
            self.assertEqual(dispatch._task["direction"], "ltr")
            self.assertEqual(dispatch._task["flip_from"], "就地")
            self.assertEqual(dispatch._task["flip_to"], "远方")
            self.assertEqual(
                dispatch._task["first_round_offset_wall_mm"],
                [0.0, 7.0, 0.0],
            )
            thread.return_value.start.assert_called_once()
        finally:
            with dispatch._lock:
                dispatch._task = original_task
                dispatch._check = original_check
                dispatch._task_stats.clear()
                dispatch._task_stats.update(original_stats)


if __name__ == "__main__":
    unittest.main()
