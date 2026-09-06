"""柜面坐标系构建·方法二：面板 mask 矩形边定轴。

输入是一帧冻结 RGB-D 的整帧点云 + YOLO 框（Xuanniu_hhy.pt，含「面板」
类的多边形 mask）。流程：

1. 取置信度最高的「面板」框（``switch_states.PANEL_CLASS``），用它的
   多边形 mask 圈出点云子集；
2. 复用 :func:`api.cabinet_panel_fit.fit_yolo_panel_rectangle`：RANSAC 拟合
   面板平面（法向 → Y 轴，指向柜内），边界 Hough 投票定矩形方向，给出
   长/短两条边的方向；
3. 两条边里更接近**画面水平**（相机 +x）的那条为 X 轴（取向画面右），
   Z = X × Y（以 X 为准重算，保证正交右手系，Z 应指向画面上方）；
4. 原点 = 相机原点在面板平面上的投影（与方法一同约定；下游
   predict_target 的推导与原点无关）。

失败（没有面板框 / mask 点太少 / 矩形拟合失败 / X 轴倾斜过大）一律抛
``ValueError``，不回退方法一——由上游按「柜面拟合失败」统一兜底。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ..cabinet_panel_fit import fit_yolo_panel_rectangle
from ..pointcloud_core import detection_pixel_mask
from ..switch_states import PANEL_CLASS

CAMERA_RIGHT = np.array([1.0, 0.0, 0.0])
CAMERA_UP = np.array([0.0, -1.0, 0.0])


def select_panel_box(boxes: list[dict[str, Any]] | None) -> tuple[int, dict[str, Any]]:
    """置信度最高的「面板」框及其在 boxes 里的下标；没有则抛 ValueError。"""
    candidates: list[tuple[int, dict[str, Any]]] = []
    seen: list[str] = []
    for index, box in enumerate(boxes or []):
        name = str(box.get("name", ""))
        if name != PANEL_CLASS:
            seen.append(name or str(box.get("cls", "?")))
            continue
        try:
            confidence = float(box.get("conf", 0.0))
        except (TypeError, ValueError):
            continue
        if np.isfinite(confidence):
            candidates.append((index, box))
    if not candidates:
        detail = f"，仅有 {sorted(set(seen))}" if seen else ""
        raise ValueError(
            f"当前帧没有 YOLO「{PANEL_CLASS}」实例{detail}，"
            "方法二无法构建柜面坐标系"
        )
    return max(candidates, key=lambda item: float(item[1].get("conf", 0.0)))


def build_panel_edge_frame(
    points_xyz: np.ndarray,
    pixel_coordinates: np.ndarray,
    image_shape: tuple[int, int] | list[int],
    boxes: list[dict[str, Any]] | None,
    *,
    plane_threshold_m: float = 0.004,
    min_points: int = 100,
    min_inlier_ratio: float = 0.35,
    max_horizontal_tilt_deg: float = 30.0,
) -> dict[str, Any]:
    points = np.asarray(points_xyz, dtype=np.float64)
    pixels = np.asarray(pixel_coordinates, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("点云必须是 N×3 数组")
    if pixels.shape != (points.shape[0], 2):
        raise ValueError("像素坐标必须与点云一一对应")
    if len(image_shape) != 2:
        raise ValueError("图像尺寸必须是 (height, width)")
    shape = (int(image_shape[0]), int(image_shape[1]))
    if shape[0] <= 0 or shape[1] <= 0:
        raise ValueError("图像尺寸必须为正数")
    if not 0.0 < max_horizontal_tilt_deg <= 90.0:
        raise ValueError("X 轴最大倾角必须在 (0, 90] 度")

    box_index, box = select_panel_box(boxes)
    inside = detection_pixel_mask(pixels[:, 0], pixels[:, 1], box,
                                  image_shape=shape)
    mask_points = points[inside]
    mask_points = mask_points[np.isfinite(mask_points).all(axis=1)]
    if mask_points.shape[0] < min_points:
        raise ValueError(
            f"「{PANEL_CLASS}」mask 内有效点不足：{mask_points.shape[0]} < "
            f"{min_points}，请靠近或让面板完整入镜"
        )

    try:
        rectangle = fit_yolo_panel_rectangle(
            mask_points,
            threshold_m=plane_threshold_m,
            min_points=min_points,
            min_inlier_ratio=min_inlier_ratio,
            seed=30_000 + int(box_index),
        )
    except ValueError as exc:
        raise ValueError(f"面板矩形拟合失败：{exc}") from exc

    # Y = 面板法向（fit_yolo_panel_rectangle 已保证 normal·center > 0，
    # 即背离相机 = 指向柜内）
    wall_y = np.asarray(rectangle["normal_camera"], dtype=np.float64)
    wall_y /= np.linalg.norm(wall_y)
    long_axis = np.asarray(rectangle["long_axis_camera"], dtype=np.float64)
    short_axis = np.asarray(rectangle["short_axis_camera"], dtype=np.float64)
    edge_candidates = (("long", long_axis), ("short", short_axis))
    # 更接近画面水平的那条边为 X（横边）；另一条即竖边，由 Z = X × Y 体现
    x_role, wall_x = max(
        edge_candidates,
        key=lambda item: abs(float(item[1] @ CAMERA_RIGHT)),
    )
    wall_x = wall_x - float(wall_x @ wall_y) * wall_y
    wall_x_length = float(np.linalg.norm(wall_x))
    if wall_x_length < 1e-6:
        raise ValueError("面板横边方向与法向平行，无法确定 X 轴")
    wall_x /= wall_x_length
    if float(wall_x @ CAMERA_RIGHT) < 0:
        wall_x = -wall_x
    horizontal_tilt_deg = float(np.degrees(np.arccos(
        np.clip(abs(float(wall_x @ CAMERA_RIGHT)), 0.0, 1.0))))
    if horizontal_tilt_deg > max_horizontal_tilt_deg:
        raise ValueError(
            f"面板横边相对画面水平倾斜 {horizontal_tilt_deg:.1f}° > "
            f"{max_horizontal_tilt_deg:g}°，疑似误检或相机严重歪斜"
        )
    wall_z = np.cross(wall_x, wall_y)
    wall_z /= np.linalg.norm(wall_z)
    if float(wall_z @ CAMERA_UP) <= 0:
        # X 取画面右、Y 指向柜内时 Z 必然朝上；到这里说明法向符号异常
        raise ValueError("面板法向方向异常，推导出的 Z 轴朝下")

    panel_center = np.asarray(
        rectangle["rectangle_center_camera_m"], dtype=np.float64)
    origin = float(panel_center @ wall_y) * wall_y
    # 竖边与 Z 的实际夹角：衡量矩形两边的正交度（仅诊断）
    other_axis = short_axis if x_role == "long" else long_axis
    vertical_residual_deg = float(np.degrees(np.arccos(
        np.clip(abs(float(other_axis @ wall_z)), 0.0, 1.0))))

    return {
        "origin_camera_m": origin.tolist(),
        "center_camera_m": panel_center.tolist(),
        "normal_camera": wall_y.tolist(),
        "x_axis_camera": wall_x.tolist(),
        "y_axis_camera": wall_y.tolist(),
        "z_axis_camera": wall_z.tolist(),
        "coordinate_system": "wall-right-handed-x-right-y-inward-z-up",
        "origin_definition": "camera-origin-projection-on-panel-plane",
        "axis_estimation": "panel-rectangle-edges",
        "calibrated": True,
        "threshold_m": float(plane_threshold_m),
        "inlier_count": int(rectangle["inlier_count"]),
        "inlier_ratio": float(rectangle["inlier_ratio"]),
        "rms_m": float(rectangle["rms_m"]),
        "sample_count": int(mask_points.shape[0]),
        "panel_detection": {
            "box_index": int(box_index),
            "cls": int(box.get("cls", -1)),
            "name": PANEL_CLASS,
            "conf": float(box.get("conf", 0.0)),
            "xyxy": box.get("xyxy"),
            "used_polygon_mask": bool(box.get("polygon") is not None),
        },
        "panel_rectangle": {
            "center_camera_m": rectangle["rectangle_center_camera_m"],
            "corners_camera_m": rectangle["rectangle_corners_camera_m"],
            "long_length_m": rectangle["long_length_m"],
            "short_length_m": rectangle["short_length_m"],
            "long_axis_camera": rectangle["long_axis_camera"],
            "short_axis_camera": rectangle["short_axis_camera"],
            "orientation_source": rectangle["orientation_source"],
            "orientation_support": rectangle["orientation_support"],
        },
        "x_axis_from_edge": x_role,
        "x_axis_horizontal_tilt_deg": horizontal_tilt_deg,
        "z_axis_vertical_edge_residual_deg": vertical_residual_deg,
    }


__all__ = ["build_panel_edge_frame", "select_panel_box"]
