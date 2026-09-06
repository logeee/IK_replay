"""柜面坐标系构建方法的枚举与参数规格（18000 配置项，单一来源）。

柜面坐标系 = 7005 点云服务在冻结的 RGB-D 帧上拟合出的 (X=右, Y=入墙,
Z=上) 右手系，供自动定位（面板中心 → 墙面系偏移 → 点 1/3）、选点横移
轴、扭转基和墙面系人工偏移共用。构建方法可以有多种：

* method1_plane_analysis —— 方法一（原有实现，``api/cabinet_frame/
  method1_plane_analysis.py``）：RANSAC 主平面定 Y 轴，多平面分割 + P0
  边界线 / 次平面主轴 / 平面交线三级回退定 X 轴。
* method2_panel_edges —— 方法二（``api/cabinet_frame/method2_panel_edges.py``）：
  取 YOLO「面板」类 mask 内的点云，拟合面板平面（Y 轴）与矩形两条边，
  更接近画面水平的边为 X 轴，Z = X × Y。依赖 Xuanniu_hhy.pt 的面板类。
* 后续方法在 ``CABINET_FRAME_METHODS`` 登记 id / 标签 / 参数规格，并在
  ``api/cabinet_frame/__init__.py`` 的分发表登记实现即可。

本模块**不能**依赖 numpy/cv2：18000（core/capability_registry.py）和
7005（api/cabinet_frame）都从这里取枚举，18000 进程不装视觉依赖。
配置存在 config/capability_registry.json 顶层 ``cabinet_frame`` 键：
    {"method": "<id>", "params": {<方法自有参数>}}
7005 启动时拉取快照决定用哪种方法（改配置后重启 7005 生效）。
"""
from __future__ import annotations

import math
from typing import Any

METHOD1_PLANE_ANALYSIS = "method1_plane_analysis"
METHOD2_PANEL_EDGES = "method2_panel_edges"

# 参数规格：{方法 id: {参数名: {default, min, max, integer?, label}}}。
# 方法一的默认值与原 api/pointcloud_viewer._ensure_wall_plane 硬编码一致，
# 注册表保持默认时行为完全不变。
CABINET_FRAME_PARAM_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    METHOD1_PLANE_ANALYSIS: {
        "plane_threshold_mm": {
            "default": 8.0, "min": 1.0, "max": 50.0,
            "label": "平面内点阈值 (mm)",
        },
        "stride": {
            "default": 3, "min": 1, "max": 8, "integer": True,
            "label": "像素步长",
        },
        "min_plane_points": {
            "default": 300, "min": 3, "max": 20000, "integer": True,
            "label": "最少平面点数",
        },
        "plane_analysis_max_points": {
            "default": 200000, "min": 1000, "max": 1000000, "integer": True,
            "label": "X 轴分析最大点数",
        },
    },
    # 方法二默认值与 api/cabinet_panel_fit.fit_yolo_panel_rectangle 的默认一致
    METHOD2_PANEL_EDGES: {
        "plane_threshold_mm": {
            "default": 4.0, "min": 1.0, "max": 20.0,
            "label": "面板平面内点阈值 (mm)",
        },
        "min_points": {
            "default": 100, "min": 10, "max": 20000, "integer": True,
            "label": "面板 mask 最少点数",
        },
        "min_inlier_ratio": {
            "default": 0.35, "min": 0.05, "max": 1.0,
            "label": "平面内点最低比例",
        },
        "max_horizontal_tilt_deg": {
            "default": 30.0, "min": 5.0, "max": 45.0,
            "label": "X 轴相对画面水平最大倾角 (°)",
        },
    },
}
CABINET_FRAME_METHODS: tuple[str, ...] = tuple(CABINET_FRAME_PARAM_SPECS)
CABINET_FRAME_METHOD_LABELS: dict[str, str] = {
    METHOD1_PLANE_ANALYSIS: "方法一：多平面分析（P0 边界线定 X 轴）",
    METHOD2_PANEL_EDGES: "方法二：面板 mask 矩形边定轴（横边=X，竖边=Z）",
}
DEFAULT_CABINET_FRAME_METHOD = METHOD1_PLANE_ANALYSIS


def default_cabinet_frame_params(method: str) -> dict[str, float | int]:
    spec = CABINET_FRAME_PARAM_SPECS[method]
    return {key: limits["default"] for key, limits in spec.items()}


def validate_cabinet_frame_params(
    method: str, value: Any, field: str = "cabinet_frame.params",
) -> dict[str, float | int]:
    """按方法校验参数块；缺省键补默认，未知键拒绝，整数项取整。"""
    if method not in CABINET_FRAME_PARAM_SPECS:
        raise ValueError(
            f"未知柜面坐标系方法 {method!r}"
            f"（支持 {' / '.join(CABINET_FRAME_METHODS)}）")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 JSON object")
    spec = CABINET_FRAME_PARAM_SPECS[method]
    unknown = set(value) - set(spec)
    if unknown:
        raise ValueError(f"{field} 含未知参数：{sorted(unknown)}")
    result: dict[str, float | int] = {}
    for key, limits in spec.items():
        raw = value.get(key)
        if raw is None:
            result[key] = limits["default"]
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}.{key} 必须是数字") from exc
        lo, hi = float(limits["min"]), float(limits["max"])
        if not math.isfinite(number) or not lo <= number <= hi:
            raise ValueError(
                f"{field}.{key} 超范围：限 {lo:g}~{hi:g}（收到 {raw}）")
        if limits.get("integer"):
            if abs(number - round(number)) > 1e-9:
                raise ValueError(f"{field}.{key} 必须是整数（收到 {raw}）")
            result[key] = int(round(number))
        else:
            result[key] = number
    return result


def validate_cabinet_frame_config(
    value: Any, field: str = "cabinet_frame",
) -> dict[str, Any]:
    """校验顶层 cabinet_frame 配置；None/缺省 → 方法一 + 默认参数。"""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 JSON object")
    method = str(value.get("method") or DEFAULT_CABINET_FRAME_METHOD)
    method = method.strip().lower()
    if method not in CABINET_FRAME_METHODS:
        raise ValueError(
            f"{field}.method 只能是 {' / '.join(CABINET_FRAME_METHODS)}"
            f"（收到 {value.get('method')!r}）")
    return {
        "method": method,
        "params": validate_cabinet_frame_params(
            method, value.get("params"), f"{field}.params"),
    }


def default_cabinet_frame_config() -> dict[str, Any]:
    return validate_cabinet_frame_config(None)


__all__ = [
    "CABINET_FRAME_METHODS",
    "CABINET_FRAME_METHOD_LABELS",
    "CABINET_FRAME_PARAM_SPECS",
    "DEFAULT_CABINET_FRAME_METHOD",
    "METHOD1_PLANE_ANALYSIS",
    "METHOD2_PANEL_EDGES",
    "default_cabinet_frame_config",
    "default_cabinet_frame_params",
    "validate_cabinet_frame_config",
    "validate_cabinet_frame_params",
]
