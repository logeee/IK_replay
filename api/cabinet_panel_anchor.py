"""自动选点模型 ``panel_anchor``：面板矩形中心 → 锚点 → 点 1/点 3。

与 0.2.0-s（``cabinet_target_finder.predict_target``，参考点 = 旋钮 mask
矩形中心）并列的另一种「粉点 → 绿点」模型。差别只在参考点与偏移组织：

    参考点   = YOLO「面板」类 mask 内点云拟合的矩形几何中心（相机系 3D，
               与柜面系方法二同一套 ``fit_yolo_panel_rectangle``）
    锚点     = 参考点 + anchor_offset（墙面系）——约定为点 1 与点 3 的中点，
               物理意义是旋钮轴心；它从不被人工点击，由标定数据算出
    目的点   = 锚点 + point{1,3}_offset（墙面系）
    去 1 还是 3 仍由置信度最高的旋钮类（旋钮右 → 1，旋钮左 → 3）决定，
    但不再使用旋钮 mask 的几何。

守卫（全部抛 ValueError，由 auto_target 统一按「算法找点失败」兜底）：
* 没有「面板」框 / 没有旋钮类框；
* 面板框贴到图像边缘（``border_margin_px``）——面板出画会让矩形中心漂移；
* 拟合出的矩形长/短边与标定时尺寸（``panel_size_mm``）偏差超过
  ``max_size_deviation_ratio``（两者都配置了才检查）；
* 偏移尚未标定（全零）。

偏移由 ``tools/calibrate_panel_anchor.py`` 从标定数据集算出并写入 18000
注册表 ``target_model.params``；本模块只做运行时几何。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from core.target_models import (
    PANEL_ANCHOR,
    TARGET_MODEL_VERSIONS,
    panel_anchor_is_calibrated,
)

from .cabinet_frame.method2_panel_edges import select_panel_box
from .cabinet_panel_fit import fit_yolo_panel_rectangle
from .pointcloud_core import PointCloud, detection_pixel_mask
from .switch_states import PANEL_CLASS, SCENE_CLASSES, SCENE_LEFT, SCENE_RIGHT

MODEL_VERSION = TARGET_MODEL_VERSIONS[PANEL_ANCHOR]
# 旋钮类别 → (点位, 参数键)
_SLOT_RULES: dict[str, tuple[int, str]] = {
    SCENE_RIGHT: (1, "point1_offset_wall_mm"),
    SCENE_LEFT: (3, "point3_offset_wall_mm"),
}


def select_knob_detection(
    boxes: list[dict[str, Any]] | None,
) -> tuple[int, dict[str, Any]]:
    """置信度最高的旋钮类（旋钮左/右）框及下标；没有则抛 ValueError。"""
    candidates: list[tuple[int, dict[str, Any]]] = []
    seen: list[str] = []
    for index, box in enumerate(boxes or []):
        name = str(box.get("name", ""))
        if name not in SCENE_CLASSES:
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
            f"当前帧没有旋钮类实例（{'/'.join(SCENE_CLASSES)}）{detail}，"
            "无法判断去点 1 还是点 3"
        )
    return max(candidates, key=lambda item: float(item[1].get("conf", 0.0)))


def check_panel_box_inside_image(
    box: dict[str, Any],
    image_shape: tuple[int, int],
    margin_px: int,
) -> None:
    """面板框四边离图像边缘都要 ≥ margin_px，否则视为出画。"""
    height, width = int(image_shape[0]), int(image_shape[1])
    try:
        x1, y1, x2, y2 = [float(v) for v in box["xyxy"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("「面板」框缺少 xyxy") from exc
    margin = float(max(0, int(margin_px)))
    touching: list[str] = []
    if x1 < margin:
        touching.append("左")
    if y1 < margin:
        touching.append("上")
    if x2 > width - margin:
        touching.append("右")
    if y2 > height - margin:
        touching.append("下")
    if touching:
        raise ValueError(
            f"「{PANEL_CLASS}」框贴到图像{'/'.join(touching)}边缘"
            f"（要求离边 ≥ {margin:g} px），面板可能出画，矩形中心不可信"
        )


def fit_panel_reference(
    cloud: PointCloud,
    boxes: list[dict[str, Any]] | None,
    image_shape: tuple[int, int] | list[int],
    wall_plane: dict[str, Any] | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    """拟合「面板」矩形，返回参考点（``rectangle_center_camera_m``）等。

    ``cloud`` 用 7005 的快照点云（YOLO 框内已加密采样）。矩形定向优先
    用柜面系 X/Z（与旋钮拟合同款 ``preferred_axes_camera``），没有柜面系
    时退回 Hough 定向。失败抛 ValueError。
    """
    if len(image_shape) != 2:
        raise ValueError("图像尺寸必须是 (height, width)")
    shape = (int(image_shape[0]), int(image_shape[1]))
    box_index, box = select_panel_box(boxes)
    check_panel_box_inside_image(
        box, shape, int(params.get("border_margin_px", 0)))

    points = np.asarray(cloud.positions, dtype=np.float64)
    pixels = np.asarray(cloud.pixels, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("PointCloud positions 必须是 N×3")
    if pixels.shape != (points.shape[0], 2):
        raise ValueError("像素坐标必须与点云一一对应")
    inside = detection_pixel_mask(pixels[:, 0], pixels[:, 1], box,
                                  image_shape=shape)
    mask_points = points[inside]
    mask_points = mask_points[np.isfinite(mask_points).all(axis=1)]
    min_points = int(params.get("min_points", 300))
    if mask_points.shape[0] < min_points:
        raise ValueError(
            f"「{PANEL_CLASS}」mask 内有效点不足：{mask_points.shape[0]} < "
            f"{min_points}"
        )

    preferred_axes = None
    if isinstance(wall_plane, dict) and wall_plane.get("calibrated"):
        try:
            preferred_axes = (
                np.asarray(wall_plane["x_axis_camera"], dtype=np.float64),
                np.asarray(wall_plane["z_axis_camera"], dtype=np.float64),
            )
        except (KeyError, TypeError, ValueError):
            preferred_axes = None
    try:
        rectangle = fit_yolo_panel_rectangle(
            mask_points,
            threshold_m=float(params.get("plane_threshold_mm", 4.0)) / 1000.0,
            min_points=min_points,
            min_inlier_ratio=float(params.get("min_inlier_ratio", 0.35)),
            seed=40_000 + int(box_index),
            preferred_axes_camera=preferred_axes,
        )
    except ValueError as exc:
        raise ValueError(f"「{PANEL_CLASS}」矩形拟合失败：{exc}") from exc

    long_mm = float(rectangle["long_length_m"]) * 1000.0
    short_mm = float(rectangle["short_length_m"]) * 1000.0
    size_ref = [float(v) for v in params.get("panel_size_mm", [0.0, 0.0])]
    ratio = float(params.get("max_size_deviation_ratio", 0.0))
    size_check: dict[str, Any] = {"enabled": False}
    if ratio > 0 and len(size_ref) == 2 and min(size_ref) > 0:
        deviations = (
            abs(long_mm - size_ref[0]) / size_ref[0],
            abs(short_mm - size_ref[1]) / size_ref[1],
        )
        size_check = {
            "enabled": True,
            "reference_mm": size_ref,
            "measured_mm": [long_mm, short_mm],
            "deviation_ratio": list(deviations),
            "max_allowed_ratio": ratio,
        }
        if max(deviations) > ratio:
            raise ValueError(
                f"「{PANEL_CLASS}」矩形 {long_mm:.0f}×{short_mm:.0f} mm 与标定"
                f"尺寸 {size_ref[0]:.0f}×{size_ref[1]:.0f} mm 偏差 "
                f"{max(deviations):.0%} > {ratio:.0%}，疑似出画/误检"
            )

    return {
        "available": True,
        "reference_source": "yolo-panel-class-rectangle-center",
        "rectangle_center_camera_m": rectangle["rectangle_center_camera_m"],
        "rectangle_corners_camera_m": rectangle["rectangle_corners_camera_m"],
        "normal_camera": rectangle["normal_camera"],
        "long_axis_camera": rectangle["long_axis_camera"],
        "short_axis_camera": rectangle["short_axis_camera"],
        "long_length_m": rectangle["long_length_m"],
        "short_length_m": rectangle["short_length_m"],
        "orientation_source": rectangle["orientation_source"],
        "inlier_count": rectangle["inlier_count"],
        "inlier_ratio": rectangle["inlier_ratio"],
        "rms_m": rectangle["rms_m"],
        "mask_point_count": int(mask_points.shape[0]),
        "detection": {
            "box_index": int(box_index),
            "cls": int(box.get("cls", -1)),
            "name": PANEL_CLASS,
            "conf": float(box.get("conf", 0.0)),
            "xyxy": box.get("xyxy"),
            "used_polygon_mask": bool(box.get("polygon") is not None),
        },
        "size_check": size_check,
    }


def _wall_axes(wall_plane: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    if not isinstance(wall_plane, dict) or not wall_plane.get("calibrated"):
        raise ValueError(f"{MODEL_VERSION} 仅支持已标定柜面坐标系")
    origin = np.asarray(wall_plane.get("origin_camera_m"), dtype=np.float64)
    axes = np.asarray(
        [wall_plane.get("x_axis_camera"), wall_plane.get("y_axis_camera"),
         wall_plane.get("z_axis_camera")],
        dtype=np.float64,
    )
    if (origin.shape != (3,) or axes.shape != (3, 3)
            or not np.isfinite(origin).all() or not np.isfinite(axes).all()):
        raise ValueError("柜面坐标系无效")
    lengths = np.linalg.norm(axes, axis=1)
    if np.any(lengths < 1e-9):
        raise ValueError("柜面坐标轴长度为零")
    axes = axes / lengths[:, None]
    if not np.allclose(axes @ axes.T, np.eye(3), atol=1e-4):
        raise ValueError("柜面坐标轴必须正交")
    return origin, axes


def predict_target_panel_anchor(
    panel_reference: dict[str, Any],
    knob_detection_name: str,
    wall_plane: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    """面板中心 + anchor_offset + point_offset → 目的点。输出契约与
    ``cabinet_target_finder.predict_target`` 一致（flow / 网页 / 存档共用）。"""
    if not isinstance(panel_reference, dict) or not panel_reference.get("available"):
        raise ValueError("当前帧面板矩形中心不可用")
    if not panel_anchor_is_calibrated(params):
        raise ValueError(
            f"{MODEL_VERSION} 偏移尚未标定（anchor/point1/point3 全为 0），"
            "请先运行 tools/calibrate_panel_anchor.py 并写入 18000"
        )
    rule = _SLOT_RULES.get(str(knob_detection_name or ""))
    if rule is None:
        raise ValueError(
            f"模型 {MODEL_VERSION} 不支持检测类别“{knob_detection_name or '未知'}”"
            f"（仅支持：{'、'.join(_SLOT_RULES)}）"
        )
    slot, point_key = rule
    origin, axes = _wall_axes(wall_plane)
    reference_camera = np.asarray(
        panel_reference["rectangle_center_camera_m"], dtype=np.float64)
    if reference_camera.shape != (3,) or not np.isfinite(reference_camera).all():
        raise ValueError("面板矩形中心无效")

    anchor_offset = np.asarray(params["anchor_offset_wall_mm"], dtype=np.float64) / 1000.0
    point_offset = np.asarray(params[point_key], dtype=np.float64) / 1000.0
    offset_wall = anchor_offset + point_offset

    reference_wall = (reference_camera - origin) @ axes.T
    anchor_wall = reference_wall + anchor_offset
    target_wall = anchor_wall + point_offset
    target_camera = origin + target_wall @ axes
    anchor_camera = origin + anchor_wall @ axes
    return {
        "model_version": MODEL_VERSION,
        "selection_source": f"target-finder/{MODEL_VERSION}",
        "target_point_slot": slot,
        "matched_detection_name": str(knob_detection_name),
        "offset_wall_m": offset_wall.tolist(),
        "anchor_offset_wall_m": anchor_offset.tolist(),
        "point_offset_wall_m": point_offset.tolist(),
        "target_wall_m": target_wall.tolist(),
        "target_camera_m": target_camera.tolist(),
        "anchor_wall_m": anchor_wall.tolist(),
        "anchor_camera_m": anchor_camera.tolist(),
        "reference_source": panel_reference.get(
            "reference_source", "yolo-panel-class-rectangle-center"),
        "reference_center_camera_m": reference_camera.tolist(),
        "reference_center_wall_m": reference_wall.tolist(),
        # 与 0.2.0-s 同名字段：flow 日志 / 网页粉点 / 存档都读这两个
        "panel_center_camera_m": reference_camera.tolist(),
        "panel_center_wall_m": reference_wall.tolist(),
        "panel_detection": panel_reference.get("detection"),
        "panel_fit_quality": {
            key: panel_reference.get(key)
            for key in (
                "inlier_count", "inlier_ratio", "rms_m",
                "long_length_m", "short_length_m", "orientation_source",
            )
        },
        "panel_size_check": panel_reference.get("size_check"),
    }


__all__ = [
    "MODEL_VERSION",
    "check_panel_box_inside_image",
    "fit_panel_reference",
    "predict_target_panel_anchor",
    "select_knob_detection",
]
