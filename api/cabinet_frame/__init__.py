"""柜面坐标系构建：按 18000 配置的方法分发，并统一校验输出契约。

用法（7005 ``api/pointcloud_viewer._ensure_wall_plane``）::

    from api.cabinet_frame import build_cabinet_frame
    frame = build_cabinet_frame(
        {"method": "method1_plane_analysis", "params": {...}},
        points_xyz, pixel_coordinates, image_shape,
    )

已有方法：
* ``method1_plane_analysis``（method1_plane_analysis.py）整帧多平面分析；
* ``method2_panel_edges``（method2_panel_edges.py）YOLO「面板」mask 矩形边。

新增方法的步骤：
1. ``core/cabinet_frame_methods.py`` 登记 id / 标签 / 参数规格
   （18000 校验与页面下拉都从那里取）；
2. 本包新建 ``methodN_xxx.py`` 实现 ``(inputs, params) -> dict``；
3. 在下方 ``_METHOD_BUILDERS`` 登记。

输出契约（所有方法必须满足，``validate_cabinet_frame`` 强制检查）：
    origin_camera_m   [3]   坐标系原点（相机系，m）
    x_axis_camera     [3]   X=右（面向柜面时的画面右）
    y_axis_camera     [3]   Y=入墙（柜面法向，背离相机）
    z_axis_camera     [3]   Z=上
    三轴单位化、正交、右手系（x × y = z）
    calibrated        bool  下游 predict_target 要求为 True
    axis_estimation   str   X 轴来源说明（日志/诊断用）
    method            str   方法 id（分发层写入）
    method_label      str   方法中文标签（分发层写入）
其余字段方法自定（诊断用），会原样透传给 ``wall_coordinate``/存档。

下游依赖见 api/cabinet_target_finder.py（origin + 三轴 + calibrated）、
api/cabinet_panel_fit.py（X/Z 轴作为面板矩形定向偏好）、
api/pointcloud_viewer.confirm（X/Z 轴覆盖选点横移轴）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from core.cabinet_frame_methods import (
    CABINET_FRAME_METHOD_LABELS,
    CABINET_FRAME_METHODS,
    DEFAULT_CABINET_FRAME_METHOD,
    METHOD1_PLANE_ANALYSIS,
    METHOD2_PANEL_EDGES,
    default_cabinet_frame_config,
    validate_cabinet_frame_config,
)

_REQUIRED_AXES = ("x_axis_camera", "y_axis_camera", "z_axis_camera")
_ORTHOGONALITY_ATOL = 1e-4   # 与 cabinet_target_finder.predict_target 一致


@dataclass(frozen=True)
class CabinetFrameInputs:
    """一帧冻结 RGB-D 的几何输入（相机系）。

    方法一只用 points/pixels/image_shape；方法二另外用 boxes（YOLO
    「面板」类多边形 mask）；其余字段给后续方法预留。
    """
    points_xyz: np.ndarray                       # N×3，相机系 m
    pixel_coordinates: np.ndarray                # N×2，(u, v)
    image_shape: tuple[int, int]                 # (height, width)
    depth_mm: np.ndarray | None = None
    intrinsics: tuple[float, float, float, float] | None = None
    boxes: list[dict[str, Any]] | None = None
    T_cam2root: np.ndarray | None = None


Builder = Callable[[CabinetFrameInputs, dict[str, Any]], dict[str, Any]]


def _build_method1(inputs: CabinetFrameInputs,
                   params: dict[str, Any]) -> dict[str, Any]:
    # 延迟导入：方法实现只在真正用到时加载（cv2/numpy 重依赖），
    # 也让测试能用 mock.patch 替换方法模块内的函数。
    from . import method1_plane_analysis as impl

    return impl.build_wall_coordinate_frame(
        inputs.points_xyz,
        inputs.pixel_coordinates,
        inputs.image_shape,
        plane_threshold_m=float(params["plane_threshold_mm"]) / 1000.0,
        stride=int(params["stride"]),
        min_plane_points=int(params["min_plane_points"]),
        plane_analysis_max_points=int(params["plane_analysis_max_points"]),
    )


def _build_method2(inputs: CabinetFrameInputs,
                   params: dict[str, Any]) -> dict[str, Any]:
    from . import method2_panel_edges as impl

    return impl.build_panel_edge_frame(
        inputs.points_xyz,
        inputs.pixel_coordinates,
        inputs.image_shape,
        inputs.boxes,
        plane_threshold_m=float(params["plane_threshold_mm"]) / 1000.0,
        min_points=int(params["min_points"]),
        min_inlier_ratio=float(params["min_inlier_ratio"]),
        max_horizontal_tilt_deg=float(params["max_horizontal_tilt_deg"]),
    )


_METHOD_BUILDERS: dict[str, Builder] = {
    METHOD1_PLANE_ANALYSIS: _build_method1,
    METHOD2_PANEL_EDGES: _build_method2,
}


def available_methods() -> tuple[str, ...]:
    """已登记实现的方法 id（应与 core 枚举一致，测试守护）。"""
    return tuple(_METHOD_BUILDERS)


def validate_cabinet_frame(frame: Any, *, method: str) -> dict[str, Any]:
    """校验并规范化方法输出：三轴单位化/正交/右手系，补 method 等字段。

    返回新 dict（不改动入参）。不满足契约抛 ValueError——这是算法 bug
    而非现场问题，但仍用 ValueError 让上游按「拟合失败」统一兜底。
    """
    if not isinstance(frame, dict):
        raise ValueError(f"柜面坐标系方法 {method} 返回值必须是字典")
    result = dict(frame)
    try:
        origin = np.asarray(result["origin_camera_m"], dtype=np.float64)
        axes = np.asarray(
            [result[key] for key in _REQUIRED_AXES], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"柜面坐标系方法 {method} 输出缺少 origin_camera_m 或三轴: {exc}"
        ) from exc
    if origin.shape != (3,) or not np.isfinite(origin).all():
        raise ValueError(f"柜面坐标系方法 {method} 的原点无效")
    if axes.shape != (3, 3) or not np.isfinite(axes).all():
        raise ValueError(f"柜面坐标系方法 {method} 的坐标轴无效")
    lengths = np.linalg.norm(axes, axis=1)
    if np.any(lengths < 1e-9):
        raise ValueError(f"柜面坐标系方法 {method} 的坐标轴长度为零")
    axes = axes / lengths[:, None]
    if not np.allclose(axes @ axes.T, np.eye(3), atol=_ORTHOGONALITY_ATOL):
        raise ValueError(f"柜面坐标系方法 {method} 的坐标轴不正交")
    if float(np.dot(np.cross(axes[0], axes[1]), axes[2])) < 0:
        raise ValueError(
            f"柜面坐标系方法 {method} 的坐标轴不是右手系（x×y 应为 z）")
    result["origin_camera_m"] = origin.tolist()
    for key, axis in zip(_REQUIRED_AXES, axes):
        result[key] = axis.tolist()
    result.setdefault("calibrated", True)
    result.setdefault("axis_estimation", "unspecified")
    result["method"] = method
    result["method_label"] = CABINET_FRAME_METHOD_LABELS.get(method, method)
    return result


def build_cabinet_frame(
    config: dict[str, Any] | None,
    points_xyz: np.ndarray,
    pixel_coordinates: np.ndarray,
    image_shape: tuple[int, int] | list[int],
    **extra_inputs: Any,
) -> dict[str, Any]:
    """按配置 ``{"method", "params"}`` 构建柜面坐标系并校验契约。

    ``config`` 为 None 时用方法一 + 默认参数（与 18000 未配置时一致）。
    未登记实现的方法抛 ValueError。
    """
    normalized = (validate_cabinet_frame_config(config)
                  if config is not None else default_cabinet_frame_config())
    method = normalized["method"]
    builder = _METHOD_BUILDERS.get(method)
    if builder is None:
        raise ValueError(
            f"柜面坐标系方法 {method!r} 已登记但 7005 没有实现"
            f"（已实现：{' / '.join(_METHOD_BUILDERS)}）")
    inputs = CabinetFrameInputs(
        points_xyz=np.asarray(points_xyz),
        pixel_coordinates=np.asarray(pixel_coordinates),
        image_shape=(int(image_shape[0]), int(image_shape[1])),
        **extra_inputs,
    )
    frame = builder(inputs, normalized["params"])
    return validate_cabinet_frame(frame, method=method)


__all__ = [
    "CABINET_FRAME_METHODS",
    "CABINET_FRAME_METHOD_LABELS",
    "CabinetFrameInputs",
    "DEFAULT_CABINET_FRAME_METHOD",
    "available_methods",
    "build_cabinet_frame",
    "validate_cabinet_frame",
]
