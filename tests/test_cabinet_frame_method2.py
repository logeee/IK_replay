"""柜面坐标系方法二：面板 mask 矩形边定轴（合成场景）。"""
from __future__ import annotations

import unittest

import numpy as np

from api.cabinet_frame import build_cabinet_frame
from api.cabinet_frame.method2_panel_edges import (
    build_panel_edge_frame,
    select_panel_box,
)
from api.switch_states import PANEL_CLASS, SCENE_RIGHT
from core.cabinet_frame_methods import METHOD2_PANEL_EDGES

FX = FY = 600.0
CX, CY = 640.0, 360.0
IMAGE_SHAPE = (720, 1280)


def _project(points: np.ndarray) -> np.ndarray:
    return np.column_stack((
        CX + points[:, 0] / points[:, 2] * FX,
        CY + points[:, 1] / points[:, 2] * FY,
    ))


def _panel_scene(tilt_deg: float, depth: float = 0.8):
    """画面里一块 0.28×0.20 m 的面板，绕光轴旋转 tilt_deg；四周是更远的背景墙。

    返回 (points, pixels, boxes, e_horizontal, e_vertical)。e_horizontal 是
    面板横边的相机系方向（略偏画面水平），e_vertical 是竖边方向（朝画面下）。
    """
    theta = np.radians(tilt_deg)
    e_h = np.array([np.cos(theta), np.sin(theta), 0.0])
    e_v = np.array([-np.sin(theta), np.cos(theta), 0.0])
    center = np.array([0.02, -0.01, depth])
    a, b = np.meshgrid(np.linspace(-0.14, 0.14, 120),
                       np.linspace(-0.10, 0.10, 90))
    panel = center + a.ravel()[:, None] * e_h + b.ravel()[:, None] * e_v
    rng = np.random.default_rng(3)
    panel[:, 2] += rng.normal(0.0, 0.0005, panel.shape[0])

    # 背景墙：更远的整面平面，与面板边缘不平行以免混入拟合
    gx, gy = np.meshgrid(np.linspace(-0.6, 0.6, 90),
                         np.linspace(-0.4, 0.4, 60))
    wall = np.column_stack((gx.ravel(), gy.ravel(), np.full(gx.size, 1.3)))
    wall_px = _project(wall)
    panel_px = _project(panel)
    corners = center + np.array([
        [-0.14, -0.10], [0.14, -0.10], [0.14, 0.10], [-0.14, 0.10],
    ]) @ np.vstack((e_h, e_v))
    polygon = _project(corners)
    # 背景点里落在面板多边形内的剔掉（被面板遮挡）
    import cv2
    raster = np.zeros(IMAGE_SHAPE, np.uint8)
    cv2.fillPoly(raster, [np.rint(polygon).astype(np.int32)], 1)
    keep = raster[np.clip(np.rint(wall_px[:, 1]).astype(int), 0, 719),
                  np.clip(np.rint(wall_px[:, 0]).astype(int), 0, 1279)] == 0
    wall, wall_px = wall[keep], wall_px[keep]

    points = np.vstack((wall, panel))
    pixels = np.vstack((wall_px, panel_px))
    x1, y1 = polygon.min(axis=0)
    x2, y2 = polygon.max(axis=0)
    boxes = [
        {"cls": 2, "name": SCENE_RIGHT, "conf": 0.97,
         "xyxy": [x1 + 40, y1 + 40, x1 + 120, y1 + 120]},
        {"cls": 0, "name": PANEL_CLASS, "conf": 0.5,
         "xyxy": [x1, y1, x2, y2], "polygon": polygon.tolist()},
        {"cls": 0, "name": PANEL_CLASS, "conf": 0.9,
         "xyxy": [x1, y1, x2, y2], "polygon": polygon.tolist()},
    ]
    return points, pixels, boxes, e_h, e_v


class SelectPanelBoxTest(unittest.TestCase):
    def test_picks_highest_confidence_panel_and_ignores_knobs(self):
        boxes = [
            {"name": SCENE_RIGHT, "conf": 0.99},
            {"name": PANEL_CLASS, "conf": 0.4},
            {"name": PANEL_CLASS, "conf": 0.8},
        ]
        index, box = select_panel_box(boxes)
        self.assertEqual(index, 2)
        self.assertEqual(box["conf"], 0.8)

    def test_no_panel_reports_what_was_seen(self):
        with self.assertRaisesRegex(ValueError, "没有 YOLO「面板」实例.*旋钮右"):
            select_panel_box([{"name": SCENE_RIGHT, "conf": 0.9}])
        with self.assertRaisesRegex(ValueError, "没有 YOLO「面板」实例"):
            select_panel_box(None)


class PanelEdgeFrameTest(unittest.TestCase):
    def test_axes_follow_panel_edges_not_camera(self):
        tilt = 6.0
        points, pixels, boxes, e_h, e_v = _panel_scene(tilt)
        frame = build_panel_edge_frame(points, pixels, IMAGE_SHAPE, boxes)

        x = np.asarray(frame["x_axis_camera"])
        y = np.asarray(frame["y_axis_camera"])
        z = np.asarray(frame["z_axis_camera"])
        # X 沿面板横边（不是相机 +x），指向画面右
        self.assertGreater(float(x @ e_h), np.cos(np.radians(1.5)))
        self.assertGreater(x[0], 0)
        # Y 指向柜内（相机 +z）
        self.assertGreater(y[2], 0.99)
        # Z 朝画面上方且沿竖边
        self.assertLess(z[1], 0)
        self.assertGreater(abs(float(z @ e_v)), np.cos(np.radians(1.5)))
        # 正交右手系
        axes = np.vstack((x, y, z))
        np.testing.assert_allclose(axes @ axes.T, np.eye(3), atol=1e-6)
        np.testing.assert_allclose(np.cross(x, y), z, atol=1e-6)
        self.assertAlmostEqual(frame["x_axis_horizontal_tilt_deg"], tilt,
                               delta=1.5)
        self.assertEqual(frame["axis_estimation"], "panel-rectangle-edges")
        self.assertTrue(frame["calibrated"])
        self.assertEqual(frame["panel_detection"]["box_index"], 2)
        self.assertEqual(frame["x_axis_from_edge"], "long")
        # 原点 = 相机原点在面板平面上的投影：在法向上，深度≈面板深度
        origin = np.asarray(frame["origin_camera_m"])
        np.testing.assert_allclose(np.cross(origin, y), 0, atol=1e-6)
        self.assertAlmostEqual(origin[2], 0.8, delta=0.01)

    def test_x_axis_prefers_more_horizontal_edge_even_if_short(self):
        """面板竖放（短边横向）时 X 仍取横边，而不是长边。"""
        points, pixels, boxes, e_h, e_v = _panel_scene(0.0)
        # 交换 e_h/e_v 的物理长度：把场景绕光轴转 90° 使长边竖直
        rot = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
        rotated = points @ rot.T
        pixels_r = _project(rotated)
        for box in boxes:
            if "polygon" in box:
                poly = np.asarray(box["polygon"])
                cam = np.column_stack((
                    (poly[:, 0] - CX) / FX * 0.8, (poly[:, 1] - CY) / FY * 0.8,
                    np.full(poly.shape[0], 0.8)))
                new_poly = _project(cam @ rot.T)
                box["polygon"] = new_poly.tolist()
                x1, y1 = new_poly.min(axis=0)
                x2, y2 = new_poly.max(axis=0)
                box["xyxy"] = [x1, y1, x2, y2]
        frame = build_panel_edge_frame(rotated, pixels_r, IMAGE_SHAPE, boxes)
        self.assertEqual(frame["x_axis_from_edge"], "short")
        self.assertGreater(np.asarray(frame["x_axis_camera"])[0], 0.99)

    def test_rejects_excessive_tilt(self):
        points, pixels, boxes, _, _ = _panel_scene(20.0)
        with self.assertRaisesRegex(ValueError, "倾斜 .* > 10"):
            build_panel_edge_frame(points, pixels, IMAGE_SHAPE, boxes,
                                   max_horizontal_tilt_deg=10.0)

    def test_missing_panel_or_sparse_mask_fails(self):
        points, pixels, boxes, _, _ = _panel_scene(0.0)
        knob_only = [b for b in boxes if b["name"] != PANEL_CLASS]
        with self.assertRaisesRegex(ValueError, "没有 YOLO「面板」实例"):
            build_panel_edge_frame(points, pixels, IMAGE_SHAPE, knob_only)
        with self.assertRaisesRegex(ValueError, "mask 内有效点不足"):
            build_panel_edge_frame(points, pixels, IMAGE_SHAPE, boxes,
                                   min_points=20_000)

    def test_dispatch_with_method2_config(self):
        points, pixels, boxes, _, _ = _panel_scene(3.0)
        frame = build_cabinet_frame(
            {"method": METHOD2_PANEL_EDGES, "params": {}},
            points, pixels, IMAGE_SHAPE, boxes=boxes,
        )
        self.assertEqual(frame["method"], METHOD2_PANEL_EDGES)
        self.assertEqual(frame["axis_estimation"], "panel-rectangle-edges")


if __name__ == "__main__":
    unittest.main()
