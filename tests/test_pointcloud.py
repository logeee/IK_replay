from __future__ import annotations

import unittest
from unittest import mock

import cv2
import numpy as np

from api.pointcloud_core import (
    BACKGROUND_COLOR,
    PALETTE,
    build_pointcloud,
    decode_pointcloud,
    encode_pointcloud,
)
from api import pointcloud_viewer


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


class SemanticColoringTest(unittest.TestCase):
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

    def tearDown(self):
        pointcloud_viewer._model = self.old_model
        pointcloud_viewer._model_name = self.old_model_name
        pointcloud_viewer._names = self.old_names
        pointcloud_viewer._latest = self.old_latest

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
            })

        self.assertTrue(metadata["ok"])
        self.assertEqual(metadata["point_count"], 4)
        self.assertEqual(metadata["source"]["frame_id"], "frame-7")
        self.assertEqual(metadata["boxes"][0]["name"], "target")
        response = pointcloud_viewer.pointcloud_data(metadata["capture_id"])
        cloud = decode_pointcloud(response.body)
        self.assertEqual(cloud.count, 4)
        np.testing.assert_array_equal(cloud.class_ids, np.full(4, 2))

    def test_old_or_unknown_capture_id_is_not_downloadable(self):
        response = pointcloud_viewer.pointcloud_data("missing")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
