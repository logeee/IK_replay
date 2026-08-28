from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from api.pointcloud_core import (
    BACKGROUND_COLOR,
    PALETTE,
    build_pointcloud,
    decode_pointcloud,
    detection_pixel_mask,
    encode_pointcloud,
    fit_surface_plane,
    point_from_pixel,
)
from api import pointcloud_viewer
from adapters.reach import perception


class PointCloudGeometryTest(unittest.TestCase):
    def test_back_projects_color_aligned_depth_and_converts_bgr_to_rgb(self):
        depth = np.full((2, 3), 1000, dtype=np.float32)
        bgr = np.zeros((2, 3, 3), dtype=np.uint8)
        bgr[1, 2] = [10, 20, 30]

        cloud = build_pointcloud(
            depth,
            bgr,
            (2.0, 4.0, 1.0, 0.5),
            [],
            stride=1,
        )

        self.assertEqual(cloud.count, 6)
        index = np.flatnonzero(
            (cloud.pixels[:, 0] == 2) & (cloud.pixels[:, 1] == 1)
        )[0]
        np.testing.assert_allclose(
            cloud.positions[index],
            [0.5, 0.125, 1.0],
            rtol=0,
            atol=1e-6,
        )
        np.testing.assert_array_equal(cloud.rgb[index], [30, 20, 10])
        np.testing.assert_array_equal(cloud.semantic[index], BACKGROUND_COLOR)
        self.assertEqual(int(cloud.class_ids[index]), -1)

    def test_filters_invalid_depth_and_uses_original_pixel_coordinates(self):
        depth = np.array(
            [
                [0, 1000, 0, 1000],
                [100, 1000, 5000, 1000],
                [0, 1000, 0, 1000],
            ],
            dtype=np.float32,
        )
        bgr = np.zeros((3, 4, 3), dtype=np.uint8)

        cloud = build_pointcloud(
            depth,
            bgr,
            (1.0, 1.0, 0.0, 0.0),
            [],
            stride=2,
            z_min_m=0.15,
            z_max_m=3.0,
        )

        self.assertEqual(cloud.count, 0)
        self.assertEqual(cloud.pixels.shape, (0, 2))

    def test_rejects_mismatched_image_and_invalid_parameters(self):
        depth = np.ones((2, 2), dtype=np.float32)
        bgr = np.zeros((3, 2, 3), dtype=np.uint8)
        with self.assertRaisesRegex(ValueError, "尺寸不一致"):
            build_pointcloud(depth, bgr, (1, 1, 0, 0), [])
        with self.assertRaisesRegex(ValueError, "stride"):
            build_pointcloud(depth, np.zeros((2, 2, 3), np.uint8),
                             (1, 1, 0, 0), [], stride=0)

    def test_rgb_pixel_uses_nearest_valid_frozen_depth(self):
        depth = np.zeros((5, 6), dtype=np.float32)
        depth[2, 3] = 1250.0
        result = point_from_pixel(
            depth,
            (100.0, 100.0, 2.5, 2.0),
            1,
            1,
            search_radius=3,
        )
        self.assertEqual(result["pixel"], [3, 2])
        self.assertAlmostEqual(result["depth_mm"], 1250.0)
        np.testing.assert_allclose(
            result["p_camera"], [0.00625, 0.0, 1.25], atol=1e-8
        )

    def test_distortion_compensation_matches_rgb_pick_and_cloud_point(self):
        depth = np.full((5, 7), 1000, dtype=np.float32)
        bgr = np.zeros((5, 7, 3), dtype=np.uint8)
        intrinsics = (120.0, 118.0, 3.0, 2.0)
        distortion = np.array([0.18, -0.04, 0.001, -0.002, 0.0])
        cloud = build_pointcloud(
            depth,
            bgr,
            intrinsics,
            [],
            stride=1,
            distortion=distortion,
        )
        index = np.flatnonzero(
            (cloud.pixels[:, 0] == 6) & (cloud.pixels[:, 1] == 1)
        )[0]
        picked = point_from_pixel(
            depth,
            intrinsics,
            6,
            1,
            search_radius=0,
            distortion=distortion,
        )

        np.testing.assert_allclose(
            cloud.positions[index], picked["p_camera"], atol=1e-7
        )
        self.assertNotAlmostEqual(
            float(cloud.positions[index, 0]),
            (6.0 - intrinsics[2]) / intrinsics[0],
            places=7,
        )

    def test_fits_surface_plane_from_frozen_depth(self):
        depth = np.full((40, 50), 1000.0, dtype=np.float32)
        plane = fit_surface_plane(
            depth,
            (100.0, 100.0, 24.5, 19.5),
            [0.0, 0.0, 1.0],
            radius_m=0.2,
        )
        self.assertIsNotNone(plane)
        np.testing.assert_allclose(
            plane["normal_cam"], [0.0, 0.0, -1.0], atol=1e-6
        )
        self.assertLess(plane["rms_mm"], 1e-6)


class SemanticColoringTest(unittest.TestCase):
    def test_instance_polygon_masks_out_box_background(self):
        u, v = np.meshgrid(np.arange(5), np.arange(5))
        detection = {
            "xyxy": [0, 0, 4, 4],
            "polygon": [[0, 0], [4, 0], [0, 4]],
        }
        inside = detection_pixel_mask(
            u.reshape(-1),
            v.reshape(-1),
            detection,
            image_shape=(5, 5),
        ).reshape(5, 5)

        self.assertTrue(inside[1, 1])
        self.assertFalse(inside[4, 4])
        cloud = build_pointcloud(
            np.full((5, 5), 1000, dtype=np.float32),
            np.zeros((5, 5, 3), dtype=np.uint8),
            (100, 100, 2, 2),
            [{"cls": 2, "conf": 0.9, **detection}],
            stride=1,
        )
        inside_index = np.flatnonzero(
            (cloud.pixels[:, 0] == 1) & (cloud.pixels[:, 1] == 1)
        )[0]
        outside_index = np.flatnonzero(
            (cloud.pixels[:, 0] == 4) & (cloud.pixels[:, 1] == 4)
        )[0]
        self.assertEqual(int(cloud.class_ids[inside_index]), 2)
        self.assertEqual(int(cloud.class_ids[outside_index]), -1)

    def test_yolo_neighborhood_is_sampled_at_every_pixel(self):
        depth = np.full((8, 8), 1000, dtype=np.float32)
        bgr = np.zeros((8, 8, 3), dtype=np.uint8)
        boxes = [{"cls": 2, "conf": 0.9, "xyxy": [5, 5, 6, 6]}]

        cloud = build_pointcloud(
            depth,
            bgr,
            (100, 100, 4, 4),
            boxes,
            stride=4,
            box_padding_ratio=0.0,
        )

        sampled_pixels = {tuple(pixel) for pixel in cloud.pixels.tolist()}
        self.assertEqual(cloud.count, 8)
        self.assertTrue(
            {(5, 5), (6, 5), (5, 6), (6, 6)}.issubset(sampled_pixels)
        )

    def test_dense_padding_does_not_expand_semantic_box_label(self):
        depth = np.full((8, 8), 1000, dtype=np.float32)
        bgr = np.zeros((8, 8, 3), dtype=np.uint8)
        cloud = build_pointcloud(
            depth,
            bgr,
            (100, 100, 4, 4),
            [{"cls": 3, "conf": 0.9, "xyxy": [5, 5, 6, 6]}],
            stride=4,
            box_padding_ratio=0.5,
        )

        padded = np.flatnonzero(
            (cloud.pixels[:, 0] == 7) & (cloud.pixels[:, 1] == 7)
        )[0]
        inside = np.flatnonzero(
            (cloud.pixels[:, 0] == 5) & (cloud.pixels[:, 1] == 5)
        )[0]
        self.assertEqual(int(cloud.class_ids[padded]), -1)
        self.assertEqual(int(cloud.class_ids[inside]), 3)

    def test_colors_box_pixels_and_higher_confidence_overlap_wins(self):
        depth = np.full((3, 4), 1000, dtype=np.float32)
        bgr = np.zeros((3, 4, 3), dtype=np.uint8)
        boxes = [
            {"cls": 1, "conf": 0.2, "xyxy": [0, 0, 3, 2]},
            {"cls": 5, "conf": 0.9, "xyxy": [2, 1, 2, 1]},
        ]

        cloud = build_pointcloud(depth, bgr, (1, 1, 0, 0), boxes, stride=1)
        overlap = np.flatnonzero(
            (cloud.pixels[:, 0] == 2) & (cloud.pixels[:, 1] == 1)
        )[0]

        self.assertTrue(np.all(cloud.class_ids != -1))
        self.assertEqual(int(cloud.class_ids[overlap]), 5)
        np.testing.assert_array_equal(cloud.semantic[overlap], PALETTE[5])
        other = np.flatnonzero(
            (cloud.pixels[:, 0] == 0) & (cloud.pixels[:, 1] == 0)
        )[0]
        self.assertEqual(int(cloud.class_ids[other]), 1)
        np.testing.assert_array_equal(cloud.semantic[other], PALETTE[1])

    def test_ignores_malformed_detection_box(self):
        depth = np.full((1, 1), 1000, dtype=np.float32)
        bgr = np.zeros((1, 1, 3), dtype=np.uint8)
        cloud = build_pointcloud(
            depth,
            bgr,
            (1, 1, 0, 0),
            [{"cls": "bad"}, {"cls": 2, "xyxy": None}],
            stride=1,
        )
        self.assertEqual(int(cloud.class_ids[0]), -1)


class PointCloudProtocolTest(unittest.TestCase):
    def test_binary_round_trip_preserves_all_arrays(self):
        depth = np.full((2, 3), 750, dtype=np.float32)
        bgr = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
        source = build_pointcloud(
            depth,
            bgr,
            (100, 101, 1, 0.5),
            [{"cls": 3, "conf": 0.8, "xyxy": [1, 0, 2, 1]}],
            stride=1,
        )

        encoded = encode_pointcloud(source)
        decoded = decode_pointcloud(encoded)

        self.assertEqual(len(encoded), 16 + source.count * 24)
        np.testing.assert_array_equal(decoded.positions, source.positions)
        np.testing.assert_array_equal(decoded.rgb, source.rgb)
        np.testing.assert_array_equal(decoded.semantic, source.semantic)
        np.testing.assert_array_equal(decoded.pixels, source.pixels)
        np.testing.assert_array_equal(decoded.class_ids, source.class_ids)

    def test_decoder_rejects_bad_header_and_truncated_data(self):
        with self.assertRaisesRegex(ValueError, "头不完整"):
            decode_pointcloud(b"PCV1")
        with self.assertRaisesRegex(ValueError, "不支持"):
            decode_pointcloud(b"NOPE" + bytes(12))
        with self.assertRaisesRegex(ValueError, "长度"):
            decode_pointcloud(b"PCV1" + (1).to_bytes(4, "little")
                              + (1).to_bytes(4, "little") + bytes(4))


class _FakeBox:
    cls = np.array([2])
    conf = np.array([0.875])
    xyxy = np.array([[0.0, 0.0, 1.0, 1.0]])


class _FakeResult:
    boxes = [_FakeBox()]


class _FakeModel:
    def predict(self, image, *, conf, verbose):
        if image.shape != (2, 2, 3) or verbose or conf != 0.25:
            raise AssertionError("YOLO 输入参数异常")
        return [_FakeResult()]


class PointCloudBackendTest(unittest.TestCase):
    def setUp(self):
        self.old_model = pointcloud_viewer._model
        self.old_model_name = pointcloud_viewer._model_name
        self.old_names = pointcloud_viewer._names
        self.old_latest = pointcloud_viewer._latest
        pointcloud_viewer._model = _FakeModel()
        pointcloud_viewer._model_name = "fake.pt"
        pointcloud_viewer._names = {2: "target"}
        pointcloud_viewer._latest = None
        pointcloud_viewer._capture_progress.clear()
        # 选点记录写到临时目录，别污染真实的 data/pick_history
        self._pick_dir = tempfile.TemporaryDirectory()
        self.old_pick_history = pointcloud_viewer.PICK_HISTORY_DIR
        pointcloud_viewer.PICK_HISTORY_DIR = Path(self._pick_dir.name)

    def tearDown(self):
        pointcloud_viewer._model = self.old_model
        pointcloud_viewer._model_name = self.old_model_name
        pointcloud_viewer._names = self.old_names
        pointcloud_viewer._latest = self.old_latest
        pointcloud_viewer._capture_progress.clear()
        pointcloud_viewer.PICK_HISTORY_DIR = self.old_pick_history
        self._pick_dir.cleanup()

    def test_capture_builds_downloadable_binary_from_one_snapshot(self):
        bgr = np.full((2, 2, 3), [10, 20, 30], dtype=np.uint8)
        ok, jpeg = cv2.imencode(".jpg", bgr)
        self.assertTrue(ok)
        snapshot = {
            "jpeg": jpeg.tobytes(),
            "depth_mm": np.full((2, 2), 1000, dtype=np.float32),
            "intrinsics": (100.0, 100.0, 0.5, 0.5),
            "metadata": {"frame_id": "frame-7"},
            "T_cam2root": None,
        }
        with mock.patch.object(
            pointcloud_viewer,
            "_fetch_rgbd_snapshot",
            return_value=snapshot,
        ):
            metadata = pointcloud_viewer.capture({
                "stride": 1,
                "z_min_m": 0.15,
                "z_max_m": 3.0,
                "conf": 0.25,
                "operation_id": "capture_test_1",
            })

        self.assertTrue(metadata["ok"])
        self.assertEqual(
            set(metadata["timings_ms"]),
            {"rgbd", "jpeg_decode", "yolo", "pointcloud", "encode"},
        )
        self.assertEqual(metadata["point_count"], 4)
        self.assertEqual(metadata["source"]["frame_id"], "frame-7")
        self.assertEqual(metadata["boxes"][0]["name"], "target")
        self.assertEqual(metadata["mask_instance_count"], 0)
        image_response = pointcloud_viewer.capture_image(metadata["capture_id"])
        self.assertEqual(image_response.media_type, "image/jpeg")
        self.assertEqual(image_response.body, jpeg.tobytes())
        response = pointcloud_viewer.pointcloud_data(metadata["capture_id"])
        cloud = decode_pointcloud(response.body)
        self.assertEqual(cloud.count, 4)
        np.testing.assert_array_equal(cloud.class_ids, np.full(4, 2))
        restored = pointcloud_viewer.capture_metadata(metadata["capture_id"])
        self.assertEqual(restored["capture_id"], metadata["capture_id"])
        progress = pointcloud_viewer.capture_progress("capture_test_1")
        self.assertTrue(progress["done"])
        self.assertFalse(progress["error"])
        self.assertEqual(progress["step"], 5)
        self.assertIn("后端完成", progress["message"])

    def test_inference_exports_instance_mask_polygon(self):
        class FakeMasks:
            xy = [np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])]

        class FakeMaskResult:
            boxes = [_FakeBox()]
            masks = FakeMasks()

        class FakeMaskModel:
            def predict(self, image, *, conf, verbose):
                return [FakeMaskResult()]

        pointcloud_viewer._model = FakeMaskModel()
        detections = pointcloud_viewer._infer(
            np.zeros((2, 2, 3), dtype=np.uint8),
            0.25,
        )

        self.assertEqual(
            detections[0]["polygon"],
            [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
        )

    def test_old_or_unknown_capture_id_is_not_downloadable(self):
        response = pointcloud_viewer.pointcloud_data("missing")
        self.assertEqual(response.status_code, 404)
        image_response = pointcloud_viewer.capture_image("missing")
        self.assertEqual(image_response.status_code, 404)

    def test_rgb_click_and_confirm_use_the_frozen_capture(self):
        bgr = np.full((2, 2, 3), [10, 20, 30], dtype=np.uint8)
        ok, jpeg = cv2.imencode(".jpg", bgr)
        self.assertTrue(ok)
        snapshot = {
            "jpeg": jpeg.tobytes(),
            "depth_mm": np.full((2, 2), 1000, dtype=np.float32),
            "intrinsics": (100.0, 100.0, 0.5, 0.5),
            "metadata": {"frame_id": "frozen-9"},
            "T_cam2root": np.eye(4).tolist(),
        }
        with mock.patch.object(
            pointcloud_viewer, "_fetch_rgbd_snapshot", return_value=snapshot
        ):
            metadata = pointcloud_viewer.capture({"stride": 1})

        picked = pointcloud_viewer.pointcloud_pixel(
            metadata["capture_id"], {"u": 1, "v": 1}
        )
        self.assertTrue(picked["ok"])
        self.assertEqual(picked["pixel"], [1, 1])
        np.testing.assert_allclose(
            picked["p_camera"], [0.005, 0.005, 1.0], atol=1e-7
        )

        upstream = mock.Mock()
        upstream.ok = True
        upstream.json.return_value = {
            "ok": True,
            "p_root": [0.005, 0.005, 1.0],
            "p_torso": [0.005, 0.005, 1.0],
        }
        with mock.patch.object(
            pointcloud_viewer._http, "post", return_value=upstream
        ) as post:
            confirmed = pointcloud_viewer.confirm_pointcloud_target(
                metadata["capture_id"],
                {
                    "p_camera": picked["p_camera"],
                    "surface_reference_camera": picked["p_camera"],
                    "pixel": picked["pixel"],
                    "adjustment_camera_m": [0.001, 0.0, 0.0],
                    "adjustment_wall_mm": {"x": 1.0, "y": 0.0, "z": 0.0},
                    "approach_offset_m": 0.0,
                    "selection_source": "target-finder/0.2.0-s",
                    "model_version": "0.2.0-s",
                    "target_point_slot": 1,
                    "matched_detection_name": "远方",
                },
            )
        self.assertTrue(confirmed["ok"])
        sent = post.call_args_list[0].kwargs["json"]
        self.assertEqual(sent["source_frame_id"], "frozen-9")
        self.assertEqual(sent["capture_id"], metadata["capture_id"])
        self.assertEqual(sent["pixel"], [1, 1])
        self.assertEqual(sent["adjustment_camera_m"], [0.001, 0.0, 0.0])
        self.assertEqual(
            sent["adjustment_wall_mm"], {"x": 1.0, "y": 0.0, "z": 0.0}
        )
        self.assertEqual(sent["selection_source"], "target-finder/0.2.0-s")
        self.assertEqual(sent["model_version"], "0.2.0-s")
        self.assertEqual(sent["target_point_slot"], 1)
        self.assertEqual(sent["matched_detection_name"], "远方")
        attached = post.call_args_list[1].kwargs["json"]
        self.assertEqual(attached["capture_id"], metadata["capture_id"])
        self.assertEqual(attached["record"], confirmed["record"])
        restored = pointcloud_viewer.capture_metadata(metadata["capture_id"])
        self.assertEqual(
            restored["confirmed_selection"]["result"]["p_root"],
            [0.005, 0.005, 1.0],
        )
        # 墙面系原始微调量要一路存进选点记录的 meta.json
        record_name = confirmed.get("record")
        self.assertTrue(record_name)
        record_meta = json.loads(
            (pointcloud_viewer.PICK_HISTORY_DIR / record_name / "meta.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            record_meta["adjustment_wall_mm"], {"x": 1.0, "y": 0.0, "z": 0.0}
        )

    def test_auto_target_uses_frozen_capture_and_caches_result(self):
        bgr = np.full((2, 2, 3), [10, 20, 30], dtype=np.uint8)
        ok, jpeg = cv2.imencode(".jpg", bgr)
        self.assertTrue(ok)
        snapshot = {
            "jpeg": jpeg.tobytes(),
            "depth_mm": np.full((2, 2), 1000, dtype=np.float32),
            "intrinsics": (100.0, 100.0, 0.5, 0.5),
            "metadata": {"frame_id": "auto-1"},
            "T_cam2root": np.eye(4).tolist(),
        }
        with mock.patch.object(
            pointcloud_viewer, "_fetch_rgbd_snapshot", return_value=snapshot
        ):
            metadata = pointcloud_viewer.capture({"stride": 1})

        wall = {
            "calibrated": True,
            "origin_camera_m": [0.0, 0.0, 1.0],
            "x_axis_camera": [1.0, 0.0, 0.0],
            "y_axis_camera": [0.0, 0.0, 1.0],
            "z_axis_camera": [0.0, -1.0, 0.0],
        }
        panel = {
            "available": True,
            "rectangle_center_camera_m": [0.0, 0.0, 1.0],
            "detection": {"name": "远方", "conf": 0.9},
        }
        prediction = {
            "model_version": "0.2.0-s",
            "selection_source": "target-finder/0.2.0-s",
            "target_point_slot": 1,
            "matched_detection_name": "远方",
            "target_camera_m": [0.05, 0.0, 1.0],
            "target_wall_m": [0.05, 0.0, 0.0],
            "offset_wall_m": [0.05, 0.0, 0.0],
        }
        with (
            mock.patch(
                "api.cabinet_wall_frame.build_wall_coordinate_frame",
                return_value=wall,
            ) as build_wall,
            mock.patch(
                "api.cabinet_panel_fit.analyze_yolo_mask_panel",
                return_value=panel,
            ) as fit_panel,
            mock.patch(
                "api.cabinet_target_finder.predict_target",
                return_value=prediction,
            ) as predict,
        ):
            first = pointcloud_viewer.auto_target(metadata["capture_id"])
            second = pointcloud_viewer.auto_target(metadata["capture_id"])

        self.assertTrue(first["ok"])
        self.assertEqual(second, first)
        self.assertEqual(first["target_point_slot"], 1)
        self.assertEqual(first["panel_center_camera_m"], [0.0, 0.0, 1.0])
        build_wall.assert_called_once()
        fit_panel.assert_called_once()
        predict.assert_called_once()

    def test_auto_target_failure_keeps_manual_pointcloud_available(self):
        bgr = np.full((2, 2, 3), [10, 20, 30], dtype=np.uint8)
        ok, jpeg = cv2.imencode(".jpg", bgr)
        self.assertTrue(ok)
        snapshot = {
            "jpeg": jpeg.tobytes(),
            "depth_mm": np.full((2, 2), 1000, dtype=np.float32),
            "intrinsics": (100.0, 100.0, 0.5, 0.5),
            "metadata": {"frame_id": "auto-fail"},
            "T_cam2root": np.eye(4).tolist(),
        }
        with mock.patch.object(
            pointcloud_viewer, "_fetch_rgbd_snapshot", return_value=snapshot
        ):
            metadata = pointcloud_viewer.capture({"stride": 1})
        with mock.patch(
            "api.cabinet_wall_frame.build_wall_coordinate_frame",
            side_effect=ValueError("柜面点不足"),
        ):
            response = pointcloud_viewer.auto_target(metadata["capture_id"])

        self.assertEqual(response.status_code, 422)
        self.assertIn("柜面点不足", response.body.decode("utf-8"))
        cloud_response = pointcloud_viewer.pointcloud_data(metadata["capture_id"])
        self.assertEqual(cloud_response.status_code, 200)

    def test_live_stream_proxies_reach_mjpeg_and_closes_upstream(self):
        upstream = mock.Mock()
        upstream.headers = {
            "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        }
        upstream.iter_content.return_value = iter([b"first", b"second"])
        with mock.patch.object(
            pointcloud_viewer._http,
            "get",
            return_value=upstream,
        ) as request:
            response = pointcloud_viewer.camera_stream()

        async def consume():
            return [chunk async for chunk in response.body_iterator]

        self.assertEqual(asyncio.run(consume()), [b"first", b"second"])
        request.assert_called_once_with(
            "http://127.0.0.1:18001/api/reach/stream",
            stream=True,
            timeout=(3.0, None),
        )
        upstream.close.assert_called_once()


class ReachPointCloudConfirmationTest(unittest.TestCase):
    def test_confirm_converts_frozen_camera_target_for_existing_planner(self):
        state = perception.state
        attributes = [
            "enabled", "T_cam2root", "T_cam2torso", "collision_checker",
            "plane", "pick_target_torso", "pick_target_root", "pick_pixel",
            "pick_torso", "pick_context", "pick_revision", "torso_diag",
        ]
        saved = {name: getattr(state, name) for name in attributes}
        try:
            state.enabled = True
            transform = np.eye(4)
            transform[:3, :3] = np.array(
                [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
            )
            state.T_cam2root = transform
            state.T_cam2torso = transform
            state.collision_checker = None
            state.pick_revision = 7
            with mock.patch.object(perception, "_read_torso", return_value=None):
                result = perception.confirm_pointcloud_pick({
                    "p_camera_surface": [0.1, 0.2, 1.0],
                    "pixel": [320, 240],
                    "adjustment_camera_m": [0.001, -0.002, 0.003],
                    "approach_offset_m": 0.01,
                    "source_frame_id": "frame-1",
                    "selection_source": "target-finder/0.2.0-s",
                    "model_version": "0.2.0-s",
                    "target_point_slot": 3,
                    "matched_detection_name": "就地",
                    "plane": {
                        "center_cam": [0.0, 0.0, 1.0],
                        "normal_cam": [0.0, 0.0, -1.0],
                        "rms_mm": 1.0,
                        "points": 500,
                        "radius_m": 0.12,
                    },
                })
            self.assertTrue(result["ok"])
            self.assertEqual(result["revision"], 8)
            np.testing.assert_allclose(result["p_root"], [0.99, 0.1, 0.2])
            self.assertEqual(result["selection_mode"], "frozen_rgbd_pointcloud")
            self.assertEqual(result["pixel"], [320, 240])
            self.assertEqual(result["plane"]["source"], "frozen_rgbd")
            self.assertEqual(
                state.pick_context["selection_mode"],
                "frozen_rgbd_pointcloud",
            )
            self.assertEqual(
                state.pick_context["selection_source"],
                "target-finder/0.2.0-s",
            )
            self.assertEqual(state.pick_context["model_version"], "0.2.0-s")
            self.assertEqual(state.pick_context["target_point_slot"], 3)
            self.assertEqual(state.pick_context["matched_detection_name"], "就地")
            self.assertEqual(result["target_point_slot"], 3)
            np.testing.assert_allclose(state.pick_target_root, result["p_root"])
            latest = perception.latest_pick()
            self.assertTrue(latest["available"])
            self.assertEqual(latest["revision"], 8)
            self.assertEqual(latest["selection_source"], "target-finder/0.2.0-s")
            np.testing.assert_allclose(latest["p_root"], result["p_root"])
            np.testing.assert_allclose(latest["p_torso"], result["p_torso"])
        finally:
            for name, value in saved.items():
                setattr(state, name, value)


if __name__ == "__main__":
    unittest.main()
