"""自动选点模型：core 枚举/配置校验、注册表兼容、panel_anchor 几何。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from api import cabinet_panel_anchor as anchor
from api.pointcloud_core import PointCloud
from api.switch_states import PANEL_CLASS, SCENE_LEFT, SCENE_RIGHT
from core import capability_registry as reg
from core import target_models as tm


class ConfigTest(unittest.TestCase):
    def test_default_is_knob_mask_center_with_no_params(self):
        config = tm.default_target_model_config()
        self.assertEqual(config["method"], tm.KNOB_MASK_CENTER)
        self.assertEqual(config["params"], {})
        self.assertEqual(tm.TARGET_MODEL_VERSIONS[tm.KNOB_MASK_CENTER], "0.2.0-s")

    def test_panel_anchor_defaults_are_the_builtin_calibration(self):
        # 偏移与 0.2.0-s 一样写死在代码里：缺省即可用，不会退成全零
        config = tm.validate_target_model_config({"method": "Panel_Anchor"})
        self.assertEqual(config["method"], tm.PANEL_ANCHOR)
        params = config["params"]
        self.assertEqual(params["anchor_offset_wall_mm"], [-87.5, -6.03, -62.22])
        self.assertEqual(params["point1_offset_wall_mm"], [48.19, 5.94, -18.19])
        self.assertEqual(params["point3_offset_wall_mm"], [-48.19, 5.94, -18.19])
        self.assertEqual(params["panel_size_mm"], [241.4, 182.0])
        self.assertEqual(params["border_margin_px"], 8)
        self.assertTrue(tm.panel_anchor_is_calibrated(params))
        # 注册表里就算写了全零也不生效：向量永远是代码常量
        zeros = tm.validate_target_model_config({"method": tm.PANEL_ANCHOR, "params": {
            "anchor_offset_wall_mm": [0, 0, 0], "point1_offset_wall_mm": [0, 0, 0],
            "point3_offset_wall_mm": [0, 0, 0]}})["params"]
        self.assertEqual(zeros["anchor_offset_wall_mm"], [-87.5, -6.03, -62.22])
        self.assertTrue(tm.panel_anchor_is_calibrated(zeros))

    def test_vectors_validated_but_ignored(self):
        # 格式仍校验（及早发现写错的配置），值一律用代码常量
        config = tm.validate_target_model_config({
            "method": tm.PANEL_ANCHOR,
            "params": {"anchor_offset_wall_mm": ["1", 2, -3.5],
                       "point1_offset_wall_mm": [40, 0, 0]},
        })
        self.assertEqual(config["params"]["anchor_offset_wall_mm"], [-87.5, -6.03, -62.22])
        self.assertEqual(config["params"]["point1_offset_wall_mm"], [48.19, 5.94, -18.19])
        self.assertTrue(tm.panel_anchor_is_calibrated(config["params"]))
        with self.assertRaisesRegex(ValueError, "长度 3"):
            tm.validate_target_model_params(
                tm.PANEL_ANCHOR, {"anchor_offset_wall_mm": [1, 2]})
        with self.assertRaisesRegex(ValueError, "长度 2"):
            tm.validate_target_model_params(
                tm.PANEL_ANCHOR, {"panel_size_mm": [1, 2, 3]})
        with self.assertRaisesRegex(ValueError, "超范围"):
            tm.validate_target_model_params(
                tm.PANEL_ANCHOR, {"point3_offset_wall_mm": [0, 0, 5000]})
        with self.assertRaisesRegex(ValueError, "未知参数"):
            tm.validate_target_model_params(tm.PANEL_ANCHOR, {"foo": 1})
        with self.assertRaisesRegex(ValueError, "未知参数"):
            tm.validate_target_model_params(
                tm.KNOB_MASK_CENTER, {"anchor_offset_wall_mm": [0, 0, 0]})
        with self.assertRaisesRegex(ValueError, "method 只能是"):
            tm.validate_target_model_config({"method": "nope"})


class RegistryTest(unittest.TestCase):
    def test_legacy_registry_gets_default_target_model(self):
        registry = reg.seed_registry()
        self.assertEqual(registry["target_model"]["method"], tm.KNOB_MASK_CENTER)
        self.assertEqual(reg.target_model_config(registry)["method"],
                         tm.KNOB_MASK_CENTER)
        self.assertEqual(reg.target_model_config(None)["method"],
                         tm.KNOB_MASK_CENTER)

    def test_roundtrip_panel_anchor(self):
        registry = reg.seed_registry()
        registry["target_model"] = {
            "method": tm.PANEL_ANCHOR,
            "params": {"anchor_offset_wall_mm": [10, 0, -5],
                       "point1_offset_wall_mm": [25, 0, 0],
                       "point3_offset_wall_mm": [-25, 0, 0],
                       "panel_size_mm": [300, 200]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            reg.save_registry(registry, path)
            loaded = reg.load_registry(path)
        params = loaded["target_model"]["params"]
        # 向量不随注册表走：读回来仍是代码常量
        self.assertEqual(params["anchor_offset_wall_mm"], [-87.5, -6.03, -62.22])
        self.assertEqual(params["panel_size_mm"], [241.4, 182.0])
        self.assertEqual(params["min_points"], 300)   # 缺省补齐


def _wall_plane() -> dict:
    return {
        "calibrated": True,
        "origin_camera_m": [0.0, 0.0, 0.8],
        "x_axis_camera": [1.0, 0.0, 0.0],
        "y_axis_camera": [0.0, 0.0, 1.0],
        "z_axis_camera": [0.0, -1.0, 0.0],
    }


class PredictTest(unittest.TestCase):
    def params(self) -> dict:
        # 预测函数只认传入的 params 字典；这里直接构造，不经过（会改回常量的）校验
        return {**tm.default_target_model_params(tm.PANEL_ANCHOR),
                "anchor_offset_wall_mm": [100, 10, -50],
                "point1_offset_wall_mm": [30, 0, 0],
                "point3_offset_wall_mm": [-30, 0, 0]}

    def reference(self) -> dict:
        return {"available": True,
                "rectangle_center_camera_m": [-0.1, 0.05, 0.8],
                "detection": {"name": PANEL_CLASS, "conf": 0.9}}

    def test_two_level_offsets_compose(self):
        right = anchor.predict_target_panel_anchor(
            self.reference(), SCENE_RIGHT, _wall_plane(), self.params())
        left = anchor.predict_target_panel_anchor(
            self.reference(), SCENE_LEFT, _wall_plane(), self.params())
        self.assertEqual(right["target_point_slot"], 1)
        self.assertEqual(left["target_point_slot"], 3)
        self.assertEqual(right["model_version"], "1.0.0-pa")
        # 面板中心墙面系 = (-0.1, 0, -0.05)；锚点 + (0.1, 0.01, -0.05)
        np.testing.assert_allclose(right["panel_center_wall_m"], [-0.1, 0.0, -0.05])
        np.testing.assert_allclose(right["anchor_wall_m"], [0.0, 0.01, -0.10])
        np.testing.assert_allclose(right["target_wall_m"], [0.03, 0.01, -0.10])
        np.testing.assert_allclose(left["target_wall_m"], [-0.03, 0.01, -0.10])
        np.testing.assert_allclose(right["offset_wall_m"], [0.13, 0.01, -0.05])
        # 回到相机系：x 同向，wall y → cam z，wall z → -cam y
        np.testing.assert_allclose(right["target_camera_m"], [0.03, 0.10, 0.81])
        self.assertEqual(right["matched_detection_name"], SCENE_RIGHT)

    def test_rejects_uncalibrated_and_unknown_class(self):
        uncalibrated = {**tm.default_target_model_params(tm.PANEL_ANCHOR),
                        "anchor_offset_wall_mm": [0.0, 0.0, 0.0],
                        "point1_offset_wall_mm": [0.0, 0.0, 0.0],
                        "point3_offset_wall_mm": [0.0, 0.0, 0.0]}
        with self.assertRaisesRegex(ValueError, "尚未标定"):
            anchor.predict_target_panel_anchor(
                self.reference(), SCENE_RIGHT, _wall_plane(), uncalibrated)
        with self.assertRaisesRegex(ValueError, "不支持检测类别"):
            anchor.predict_target_panel_anchor(
                self.reference(), PANEL_CLASS, _wall_plane(), self.params())
        with self.assertRaisesRegex(ValueError, "已标定"):
            anchor.predict_target_panel_anchor(
                self.reference(), SCENE_RIGHT, {"calibrated": False}, self.params())


class GuardsTest(unittest.TestCase):
    def test_select_knob_ignores_panel_class(self):
        boxes = [{"name": PANEL_CLASS, "conf": 0.99},
                 {"name": SCENE_LEFT, "conf": 0.5},
                 {"name": SCENE_RIGHT, "conf": 0.7}]
        index, box = anchor.select_knob_detection(boxes)
        self.assertEqual((index, box["name"]), (2, SCENE_RIGHT))
        with self.assertRaisesRegex(ValueError, "没有旋钮类实例"):
            anchor.select_knob_detection([{"name": PANEL_CLASS, "conf": 0.99}])

    def test_border_guard(self):
        anchor.check_panel_box_inside_image(
            {"xyxy": [20, 20, 600, 400]}, (480, 640), 8)
        with self.assertRaisesRegex(ValueError, "右/下边缘"):
            anchor.check_panel_box_inside_image(
                {"xyxy": [20, 20, 636, 478]}, (480, 640), 8)

    def test_fit_panel_reference_on_synthetic_panel(self):
        # 正对相机、深 0.8 m 的 0.30×0.20 m 面板；像素按 fx=fy=800, cx=320, cy=240
        fx = fy = 800.0
        cx, cy = 320.0, 240.0
        xs = np.linspace(-0.15, 0.15, 150)
        ys = np.linspace(-0.10, 0.10, 100)
        gx, gy = np.meshgrid(xs, ys)
        points = np.column_stack((gx.ravel(), gy.ravel(), np.full(gx.size, 0.8)))
        u = points[:, 0] / 0.8 * fx + cx
        v = points[:, 1] / 0.8 * fy + cy
        cloud = PointCloud(
            positions=points.astype(np.float32),
            rgb=np.zeros((points.shape[0], 3), np.uint8),
            semantic=np.zeros((points.shape[0], 3), np.uint8),
            pixels=np.column_stack((u, v)).astype(np.uint16),
            class_ids=np.zeros(points.shape[0], np.int16),
        )
        box = {"name": PANEL_CLASS, "conf": 0.9, "cls": 0,
               "xyxy": [float(u.min()) - 2, float(v.min()) - 2,
                        float(u.max()) + 2, float(v.max()) + 2]}
        params = tm.default_target_model_params(tm.PANEL_ANCHOR)
        params["panel_size_mm"] = [300.0, 200.0]
        params["max_size_deviation_ratio"] = 0.1
        reference = anchor.fit_panel_reference(
            cloud, [box], (480, 640), _wall_plane(), params)
        self.assertTrue(reference["available"])
        np.testing.assert_allclose(
            reference["rectangle_center_camera_m"], [0.0, 0.0, 0.8], atol=0.01)
        self.assertAlmostEqual(reference["long_length_m"], 0.30, delta=0.02)
        self.assertTrue(reference["size_check"]["enabled"])
        # 尺寸守卫：标定尺寸与实测差太多 → 拒绝
        params["panel_size_mm"] = [500.0, 200.0]
        with self.assertRaisesRegex(ValueError, "偏差"):
            anchor.fit_panel_reference(cloud, [box], (480, 640), _wall_plane(), params)


if __name__ == "__main__":
    unittest.main()
