"""柜面坐标系构建方法：枚举/参数校验（core）与分发/契约校验（api）。"""
from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

from api import cabinet_frame
from api.cabinet_frame import build_cabinet_frame, validate_cabinet_frame
from core import cabinet_frame_methods as methods
from core import capability_registry as reg


def _plane_inputs():
    """一块正对相机、深 1 m 的平面（方法一能拟合出来的最小场景）。"""
    x_values, y_values = np.meshgrid(
        np.linspace(-0.25, 0.25, 60), np.linspace(-0.20, 0.20, 50))
    points = np.column_stack(
        (x_values.ravel(), y_values.ravel(), np.ones(x_values.size)))
    pixel_u, pixel_v = np.meshgrid(np.arange(60), np.arange(50))
    pixels = np.column_stack((pixel_u.ravel(), pixel_v.ravel()))
    return points, pixels, (50, 60)


class EnumAndParamsTest(unittest.TestCase):
    def test_default_config_is_method1_with_legacy_values(self):
        config = methods.default_cabinet_frame_config()
        self.assertEqual(config["method"], methods.METHOD1_PLANE_ANALYSIS)
        # 与改造前 pointcloud_viewer._ensure_wall_plane 的硬编码一致
        self.assertEqual(config["params"], {
            "plane_threshold_mm": 8.0,
            "stride": 3,
            "min_plane_points": 300,
            "plane_analysis_max_points": 200000,
        })

    def test_params_fill_defaults_and_reject_unknown_or_out_of_range(self):
        filled = methods.validate_cabinet_frame_params(
            methods.METHOD1_PLANE_ANALYSIS, {"stride": "2"})
        self.assertEqual(filled["stride"], 2)
        self.assertIsInstance(filled["stride"], int)
        self.assertEqual(filled["plane_threshold_mm"], 8.0)
        with self.assertRaisesRegex(ValueError, "未知参数"):
            methods.validate_cabinet_frame_params(
                methods.METHOD1_PLANE_ANALYSIS, {"foo": 1})
        with self.assertRaisesRegex(ValueError, "超范围"):
            methods.validate_cabinet_frame_params(
                methods.METHOD1_PLANE_ANALYSIS, {"plane_threshold_mm": 0.1})
        with self.assertRaisesRegex(ValueError, "必须是整数"):
            methods.validate_cabinet_frame_params(
                methods.METHOD1_PLANE_ANALYSIS, {"stride": 2.5})

    def test_config_rejects_unknown_method_and_normalizes_case(self):
        config = methods.validate_cabinet_frame_config(
            {"method": " Method1_Plane_Analysis "})
        self.assertEqual(config["method"], methods.METHOD1_PLANE_ANALYSIS)
        with self.assertRaisesRegex(ValueError, "method 只能是"):
            methods.validate_cabinet_frame_config({"method": "method9"})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            methods.validate_cabinet_frame_config([])

    def test_every_registered_method_has_label_and_implementation(self):
        # core 枚举、标签、api 分发表三处必须同步登记
        self.assertEqual(set(methods.CABINET_FRAME_METHODS),
                         set(methods.CABINET_FRAME_METHOD_LABELS))
        self.assertEqual(set(methods.CABINET_FRAME_METHODS),
                         set(cabinet_frame.available_methods()))
        self.assertIn(methods.DEFAULT_CABINET_FRAME_METHOD,
                      methods.CABINET_FRAME_METHODS)


class RegistryIntegrationTest(unittest.TestCase):
    def test_registry_without_cabinet_frame_gets_method1_defaults(self):
        seed = reg.seed_registry()
        seed.pop("cabinet_frame", None)
        validated = reg.validate_registry(seed)
        self.assertEqual(validated["cabinet_frame"],
                         methods.default_cabinet_frame_config())
        self.assertEqual(reg.cabinet_frame_config(validated)["method"],
                         methods.METHOD1_PLANE_ANALYSIS)
        self.assertEqual(reg.cabinet_frame_config(None),
                         methods.default_cabinet_frame_config())

    def test_registry_keeps_custom_params_and_rejects_bad_method(self):
        seed = reg.seed_registry()
        seed["cabinet_frame"] = {
            "method": methods.METHOD1_PLANE_ANALYSIS,
            "params": {"plane_threshold_mm": 6, "stride": 2},
        }
        validated = reg.validate_registry(seed)
        self.assertEqual(validated["cabinet_frame"]["params"]["stride"], 2)
        self.assertEqual(
            validated["cabinet_frame"]["params"]["plane_threshold_mm"], 6.0)
        self.assertEqual(
            validated["cabinet_frame"]["params"]["min_plane_points"], 300)
        seed["cabinet_frame"] = {"method": "nope"}
        with self.assertRaisesRegex(ValueError, "cabinet_frame.method"):
            reg.validate_registry(seed)


class DispatchTest(unittest.TestCase):
    def test_none_config_runs_method1_and_stamps_method(self):
        points, pixels, shape = _plane_inputs()
        frame = build_cabinet_frame(None, points, pixels, shape)
        self.assertEqual(frame["method"], methods.METHOD1_PLANE_ANALYSIS)
        self.assertEqual(
            frame["method_label"],
            methods.CABINET_FRAME_METHOD_LABELS[methods.METHOD1_PLANE_ANALYSIS])
        self.assertTrue(frame["calibrated"])
        axes = np.asarray([frame["x_axis_camera"], frame["y_axis_camera"],
                           frame["z_axis_camera"]])
        np.testing.assert_allclose(axes @ axes.T, np.eye(3), atol=1e-6)
        np.testing.assert_allclose(np.cross(axes[0], axes[1]), axes[2],
                                   atol=1e-6)

    def test_params_are_forwarded_to_method1(self):
        points, pixels, shape = _plane_inputs()
        config = {
            "method": methods.METHOD1_PLANE_ANALYSIS,
            "params": {"plane_threshold_mm": 4, "stride": 1,
                       "min_plane_points": 100,
                       "plane_analysis_max_points": 5000},
        }
        fake = {
            "origin_camera_m": [0, 0, 1],
            "x_axis_camera": [1, 0, 0],
            "y_axis_camera": [0, 0, 1],
            "z_axis_camera": [0, -1, 0],
            "axis_estimation": "camera-up-projection",
        }
        with mock.patch(
            "api.cabinet_frame.method1_plane_analysis."
            "build_wall_coordinate_frame",
            return_value=fake,
        ) as build:
            frame = build_cabinet_frame(config, points, pixels, shape)
        kwargs = build.call_args.kwargs
        self.assertAlmostEqual(kwargs["plane_threshold_m"], 0.004)
        self.assertEqual(kwargs["stride"], 1)
        self.assertEqual(kwargs["min_plane_points"], 100)
        self.assertEqual(kwargs["plane_analysis_max_points"], 5000)
        self.assertEqual(frame["method"], methods.METHOD1_PLANE_ANALYSIS)
        self.assertTrue(frame["calibrated"])   # 契约层补齐

    def test_unimplemented_method_is_rejected(self):
        points, pixels, shape = _plane_inputs()
        with mock.patch.dict(cabinet_frame._METHOD_BUILDERS, clear=True):
            with self.assertRaisesRegex(ValueError, "没有实现"):
                build_cabinet_frame(None, points, pixels, shape)

    def test_contract_rejects_non_orthogonal_or_left_handed_axes(self):
        base = {
            "origin_camera_m": [0, 0, 1],
            "x_axis_camera": [1, 0, 0],
            "y_axis_camera": [0, 0, 1],
            "z_axis_camera": [0, -1, 0],
        }
        good = validate_cabinet_frame(base, method="m")
        self.assertEqual(good["method"], "m")
        self.assertTrue(good["calibrated"])
        self.assertEqual(good["axis_estimation"], "unspecified")
        with self.assertRaisesRegex(ValueError, "不正交"):
            validate_cabinet_frame(
                {**base, "y_axis_camera": [0.5, 0, 1]}, method="m")
        with self.assertRaisesRegex(ValueError, "右手系"):
            validate_cabinet_frame(
                {**base, "z_axis_camera": [0, 1, 0]}, method="m")
        with self.assertRaisesRegex(ValueError, "缺少"):
            validate_cabinet_frame({"origin_camera_m": [0, 0, 1]}, method="m")
        # 非单位轴会被归一化
        scaled = validate_cabinet_frame(
            {**base, "x_axis_camera": [2, 0, 0]}, method="m")
        self.assertEqual(scaled["x_axis_camera"], [1.0, 0.0, 0.0])

    def test_compat_import_path_still_works(self):
        from api.cabinet_wall_frame import (
            build_wall_coordinate_frame, fit_dominant_plane)
        from api.cabinet_frame import method1_plane_analysis as impl
        self.assertIs(build_wall_coordinate_frame,
                      impl.build_wall_coordinate_frame)
        self.assertIs(fit_dominant_plane, impl.fit_dominant_plane)


if __name__ == "__main__":
    unittest.main()
