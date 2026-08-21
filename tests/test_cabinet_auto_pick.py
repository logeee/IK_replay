from __future__ import annotations

import unittest

import numpy as np

from api.cabinet_panel_fit import (
    analyze_yolo_mask_panel,
    fit_yolo_panel_rectangle,
)
from api.cabinet_target_finder import predict_target
from api.cabinet_wall_frame import build_wall_coordinate_frame
from api.pointcloud_core import PointCloud


class CabinetTargetFinderTest(unittest.TestCase):
    def wall_plane(self) -> dict:
        return {
            "calibrated": True,
            "origin_camera_m": [1.0, 2.0, 3.0],
            "x_axis_camera": [1.0, 0.0, 0.0],
            "y_axis_camera": [0.0, 1.0, 0.0],
            "z_axis_camera": [0.0, 0.0, 1.0],
        }

    def panel_fit(self, name: str) -> dict:
        return {
            "available": True,
            "rectangle_center_camera_m": [1.12, 1.99, 3.20],
            "detection": {"name": name, "conf": 0.95},
            "inlier_ratio": 0.9,
            "rms_m": 0.001,
        }

    def test_remote_generates_point_one(self):
        prediction = predict_target(self.panel_fit("远方"), self.wall_plane())

        self.assertEqual(prediction["model_version"], "0.2.0-s")
        self.assertEqual(prediction["target_point_slot"], 1)
        self.assertEqual(prediction["matched_detection_name"], "远方")
        np.testing.assert_allclose(
            prediction["offset_wall_m"],
            [0.04793951829, 0.00586060655, -0.01953248751],
            atol=1e-12,
        )

    def test_local_generates_mirrored_point_three(self):
        remote = predict_target(self.panel_fit("远方"), self.wall_plane())
        local = predict_target(self.panel_fit("就地"), self.wall_plane())

        self.assertEqual(local["target_point_slot"], 3)
        self.assertEqual(local["matched_detection_name"], "就地")
        self.assertAlmostEqual(
            local["offset_wall_m"][0], -remote["offset_wall_m"][0]
        )
        np.testing.assert_allclose(
            local["offset_wall_m"][1:],
            remote["offset_wall_m"][1:],
            atol=1e-12,
        )

    def test_unknown_detection_class_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不支持检测类别"):
            predict_target(self.panel_fit("其他"), self.wall_plane())

    def test_missing_panel_fit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "面板"):
            predict_target(
                {"available": False, "reason": "有效点不足"},
                self.wall_plane(),
            )


class CabinetGeometryTest(unittest.TestCase):
    @staticmethod
    def pointcloud(
        points: np.ndarray,
        pixels: np.ndarray,
        *,
        rgb: np.ndarray | None = None,
    ) -> PointCloud:
        count = points.shape[0]
        return PointCloud(
            positions=np.asarray(points, dtype=np.float32),
            rgb=(
                np.full((count, 3), 120, dtype=np.uint8)
                if rgb is None
                else np.asarray(rgb, dtype=np.uint8)
            ),
            semantic=np.zeros((count, 3), dtype=np.uint8),
            pixels=np.asarray(pixels, dtype=np.uint16),
            class_ids=np.full(count, -1, dtype=np.int16),
        )

    def test_build_wall_coordinate_frame_is_right_handed(self):
        x_values, y_values = np.meshgrid(
            np.linspace(-0.25, 0.25, 60),
            np.linspace(-0.20, 0.20, 50),
        )
        points = np.column_stack(
            (
                x_values.ravel(),
                y_values.ravel(),
                np.ones(x_values.size),
            )
        )
        pixel_u, pixel_v = np.meshgrid(np.arange(60), np.arange(50))
        pixels = np.column_stack((pixel_u.ravel(), pixel_v.ravel()))

        wall = build_wall_coordinate_frame(
            points,
            pixels,
            (50, 60),
            plane_threshold_m=0.004,
            stride=1,
            min_plane_points=100,
            plane_analysis_max_points=5_000,
        )

        x_axis = np.asarray(wall["x_axis_camera"])
        y_axis = np.asarray(wall["y_axis_camera"])
        z_axis = np.asarray(wall["z_axis_camera"])
        self.assertTrue(wall["calibrated"])
        np.testing.assert_allclose(
            np.column_stack((x_axis, y_axis, z_axis)).T
            @ np.column_stack((x_axis, y_axis, z_axis)),
            np.eye(3),
            atol=1e-6,
        )
        np.testing.assert_allclose(np.cross(x_axis, y_axis), z_axis, atol=1e-6)

    def test_panel_rectangle_rejects_knob_and_handles_occlusion(self):
        rng = np.random.default_rng(7)
        long_positions = rng.uniform(-0.14, 0.14, 10_000)
        short_positions = rng.uniform(-0.10, 0.10, 10_000)
        visible = ~(
            (long_positions > 0.03) & (short_positions < -0.01)
        )
        long_positions = long_positions[visible]
        short_positions = short_positions[visible]
        angle = np.radians(17.0)
        panel = np.column_stack(
            (
                long_positions * np.cos(angle)
                - short_positions * np.sin(angle),
                long_positions * np.sin(angle)
                + short_positions * np.cos(angle),
                0.8
                + rng.normal(0.0, 0.0012, long_positions.shape[0]),
            )
        )
        knob = np.column_stack(
            (
                rng.normal(-0.03, 0.025, 1_500),
                rng.normal(0.02, 0.025, 1_500),
                rng.normal(0.72, 0.008, 1_500),
            )
        )
        outliers = rng.uniform(
            [-0.18, -0.14, 0.65],
            [0.18, 0.14, 0.95],
            size=(300, 3),
        )

        fitted = fit_yolo_panel_rectangle(
            np.vstack((panel, knob, outliers))
        )

        self.assertTrue(fitted["available"])
        self.assertAlmostEqual(fitted["long_length_m"], 0.28, delta=0.02)
        self.assertAlmostEqual(fitted["short_length_m"], 0.20, delta=0.02)
        self.assertGreater(fitted["excluded_point_count"], 1_500)
        np.testing.assert_allclose(
            fitted["rectangle_center_camera_m"],
            np.mean(fitted["rectangle_corners_camera_m"], axis=0),
            atol=1e-12,
        )

    def test_panel_analysis_uses_highest_confidence_polygon(self):
        x_values, y_values = np.meshgrid(
            np.linspace(-0.12, 0.12, 100),
            np.linspace(-0.08, 0.08, 70),
        )
        points = np.column_stack(
            (
                x_values.ravel(),
                y_values.ravel(),
                np.full(x_values.size, 0.8),
            )
        )
        pixels = np.column_stack(
            (
                100 + (x_values.ravel() + 0.12) * 400,
                100 + (y_values.ravel() + 0.08) * 400,
            )
        )
        boxes = [
            {
                "cls": 3,
                "name": "lower",
                "conf": 0.4,
                "xyxy": [0, 0, 10, 10],
            },
            {
                "cls": 1,
                "name": "远方",
                "conf": 0.95,
                "xyxy": [95, 95, 205, 170],
                "polygon": [
                    [95, 95],
                    [205, 95],
                    [205, 170],
                    [95, 170],
                ],
            },
        ]

        fitted = analyze_yolo_mask_panel(
            self.pointcloud(points, pixels),
            boxes,
            image_shape=(300, 300),
            wall_plane=None,
        )

        self.assertTrue(fitted["available"])
        self.assertEqual(fitted["detection"]["box_index"], 1)
        self.assertEqual(fitted["detection"]["name"], "远方")
        self.assertTrue(fitted["detection"]["used_polygon_mask"])
        self.assertGreater(fitted["mask_point_count"], 6_000)

    def test_panel_analysis_reports_too_few_mask_points(self):
        points = np.zeros((20, 3), dtype=np.float32)
        points[:, 2] = 1.0
        pixels = np.column_stack((np.arange(20), np.arange(20)))

        fitted = analyze_yolo_mask_panel(
            self.pointcloud(points, pixels),
            [
                {
                    "name": "远方",
                    "conf": 0.9,
                    "xyxy": [0, 0, 30, 30],
                }
            ],
            image_shape=(40, 40),
            wall_plane=None,
        )

        self.assertFalse(fitted["available"])
        self.assertIn("有效点不足", fitted["reason"])


if __name__ == "__main__":
    unittest.main()
