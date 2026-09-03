"""Cabinet target prediction using the fixed 0.2.0-s panel-center model."""

from __future__ import annotations

from typing import Any

import numpy as np

from .switch_states import SCENE_LEFT, SCENE_RIGHT


MODEL_VERSION = "0.2.0-s"
# 键为开关物理指向类别（Xuanniu_D.pt）。偏移沿用旧模型标定值：旧「远方」
# 规则在工厂柜上对应开关在右（+x），旧「就地」对应开关在左（−x）。
# ⚠ 换模型后框/掩码几何可能有差异，首次实机使用建议手动模式核对一次
# 取点落位再放开自动执行。
_DETECTION_RULES: dict[str, tuple[int, np.ndarray]] = {
    SCENE_RIGHT: (
        1,
        np.array(
            [0.04793951829, 0.00586060655, -0.01953248751],
            dtype=np.float64,
        ),
    ),
    SCENE_LEFT: (
        3,
        np.array(
            [-0.04793951829, 0.00586060655, -0.01953248751],
            dtype=np.float64,
        ),
    ),
}


def predict_target(
    panel_fit: dict[str, Any],
    wall_plane: dict[str, Any],
) -> dict[str, Any]:
    """Predict point 1/3 from the fitted panel center and YOLO class."""
    if not isinstance(panel_fit, dict) or not panel_fit.get("available"):
        reason = (
            panel_fit.get("reason") if isinstance(panel_fit, dict) else None
        )
        raise ValueError(
            f"当前帧面板中心不可用{f'：{reason}' if reason else ''}"
        )
    if not isinstance(wall_plane, dict):
        raise ValueError("墙面坐标系必须是字典")
    if not bool(wall_plane.get("calibrated")):
        raise ValueError(f"{MODEL_VERSION} 仅支持已标定坐标系")

    origin = np.asarray(
        wall_plane.get("origin_camera_m"), dtype=np.float64
    )
    axes = np.asarray(
        [
            wall_plane.get("x_axis_camera"),
            wall_plane.get("y_axis_camera"),
            wall_plane.get("z_axis_camera"),
        ],
        dtype=np.float64,
    )
    panel_center_camera = np.asarray(
        panel_fit.get("rectangle_center_camera_m"), dtype=np.float64
    )
    if (
        origin.shape != (3,)
        or axes.shape != (3, 3)
        or panel_center_camera.shape != (3,)
        or not np.isfinite(origin).all()
        or not np.isfinite(axes).all()
        or not np.isfinite(panel_center_camera).all()
    ):
        raise ValueError("面板中心或墙面坐标系无效")
    axis_lengths = np.linalg.norm(axes, axis=1)
    if np.any(axis_lengths < 1e-9):
        raise ValueError("墙面坐标轴长度为零")
    unit_axes = axes / axis_lengths[:, None]
    if not np.allclose(unit_axes @ unit_axes.T, np.eye(3), atol=1e-4):
        raise ValueError("墙面坐标轴必须正交")

    detection = panel_fit.get("detection")
    detection_name = (
        str(detection.get("name"))
        if isinstance(detection, dict) and detection.get("name") is not None
        else ""
    )
    rule = _DETECTION_RULES.get(detection_name)
    if rule is None:
        supported = "、".join(_DETECTION_RULES)
        raise ValueError(
            f"模型 {MODEL_VERSION} 不支持检测类别"
            f"“{detection_name or '未知'}”（仅支持：{supported}）"
        )
    target_point_slot, offset_wall = rule
    reference_center_wall = (panel_center_camera - origin) @ axes.T
    target_wall = reference_center_wall + offset_wall
    target_camera = origin + target_wall @ axes
    return {
        "model_version": MODEL_VERSION,
        "selection_source": f"target-finder/{MODEL_VERSION}",
        "target_point_slot": target_point_slot,
        "matched_detection_name": detection_name,
        "offset_wall_m": offset_wall.tolist(),
        "target_wall_m": target_wall.tolist(),
        "target_camera_m": target_camera.tolist(),
        "reference_source": "yolo-panel-rectangle-center",
        "reference_center_camera_m": panel_center_camera.tolist(),
        "reference_center_wall_m": reference_center_wall.tolist(),
        "panel_center_camera_m": panel_center_camera.tolist(),
        "panel_center_wall_m": reference_center_wall.tolist(),
        "panel_detection": detection,
        "panel_fit_quality": {
            key: panel_fit.get(key)
            for key in (
                "inlier_count",
                "inlier_ratio",
                "rms_m",
                "long_length_m",
                "short_length_m",
                "orientation_source",
            )
        },
    }


__all__ = ["MODEL_VERSION", "predict_target"]
