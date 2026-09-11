"""自动选点模型（粉点 → 绿点）的枚举与配置校验（18000 配置项，单一来源）。

自动选点 = 7005 在冻结的 RGB-D 帧上，从某个**参考点**出发，沿柜面坐标系
（X=右, Y=入墙, Z=上）加固定偏移得到目的点（绿点）。参考点从哪来、偏移
怎么组织，就是「模型」：

* knob_mask_center —— 现有 0.2.0-s（``api/cabinet_target_finder.predict_target``）：
  参考点 = 置信度最高的旋钮类（旋钮左/右）mask 拟合矩形中心，偏移是代码
  内置常量；旋钮右 → 点 1，旋钮左 → 点 3。无可配参数。
* panel_anchor —— 面板锚点法（``api/cabinet_panel_anchor.py``）：
  参考点 = YOLO「面板」类 mask 拟合矩形中心（与柜面系方法二同一套拟合），
  两级偏移：面板中心 --anchor_offset--> 锚点 --point{1,3}_offset--> 目的点。
  锚点由标定脚本定义（默认点 1/点 3 均值的中点，或旋钮 mask 中心均值），
  point1 / point3 是任意三维向量——两点**不要求**等高或对称。旋钮左/右
  类别仍决定去点 1 还是点 3，但运行时不再使用旋钮 mask 的几何。偏移由
  ``tools/calibrate_panel_anchor.py`` 从标定数据算出后写入 18000 注册表。

本模块**不能**依赖 numpy/cv2：18000（core/capability_registry.py）和 7005
都从这里取枚举，18000 进程不装视觉依赖。配置存在
config/capability_registry.json 顶层 ``target_model`` 键：
    {"method": "<id>", "params": {<方法自有参数>}}
7005 启动时拉取快照决定用哪种模型（改配置后重启 7005 生效）。
"""
from __future__ import annotations

import math
from typing import Any

KNOB_MASK_CENTER = "knob_mask_center"
PANEL_ANCHOR = "panel_anchor"

TARGET_MODELS: tuple[str, ...] = (KNOB_MASK_CENTER, PANEL_ANCHOR)
TARGET_MODEL_LABELS: dict[str, str] = {
    KNOB_MASK_CENTER: "旋钮 mask 中心 + 固定偏移（0.2.0-s）",
    PANEL_ANCHOR: "面板矩形中心 → 锚点 → 点 1/3（两级偏移，需标定）",
}
DEFAULT_TARGET_MODEL = KNOB_MASK_CENTER
# 运行时写进结果 / 选点记录的 model_version（knob 沿用历史值）
TARGET_MODEL_VERSIONS: dict[str, str] = {
    KNOB_MASK_CENTER: "0.2.0-s",
    PANEL_ANCHOR: "1.0.0-pa",
}

# 标量参数规格（向量参数单独校验，见 _VECTOR_PARAMS）
TARGET_MODEL_PARAM_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    KNOB_MASK_CENTER: {},
    PANEL_ANCHOR: {
        "plane_threshold_mm": {
            "default": 4.0, "min": 1.0, "max": 20.0,
            "label": "面板平面内点阈值 (mm)",
        },
        "min_points": {
            "default": 300, "min": 10, "max": 50000, "integer": True,
            "label": "面板 mask 最少点数",
        },
        "min_inlier_ratio": {
            "default": 0.35, "min": 0.05, "max": 1.0,
            "label": "平面内点最低比例",
        },
        "border_margin_px": {
            "default": 8, "min": 0, "max": 200, "integer": True,
            "label": "面板框离图像边缘最小距离 (px)，不足则拒绝（防出画）",
        },
        "max_size_deviation_ratio": {
            "default": 0.15, "min": 0.0, "max": 1.0,
            "label": "面板矩形长/短边相对标定尺寸允许偏差比例（0=不检查）",
        },
    },
}
# 向量参数（mm，墙面系 x右/y入墙/z上）。默认值就是写死的标定结果（与 0.2.0-s
# 把常量写在代码里一个做法）：2026-09-12 会话 20260912_002932_hhy-mianban，
# 11 帧旋钮 mask 中心 + 7 帧点 1，点 3 按旋钮中心左右镜像。重新标定时用
# tools/calibrate_panel_anchor.py 得到新值后改这里。
_VECTOR_PARAMS: dict[str, dict[str, dict[str, Any]]] = {
    PANEL_ANCHOR: {
        "anchor_offset_wall_mm": {
            "label": "面板矩形中心 → 锚点偏移 (mm)",
            "default": [-87.5, -6.03, -62.22],
        },
        "point1_offset_wall_mm": {
            "label": "锚点 → 点 1（旋钮右）偏移 (mm)",
            "default": [48.19, 5.94, -18.19],
        },
        "point3_offset_wall_mm": {
            "label": "锚点 → 点 3（旋钮左）偏移 (mm)",
            "default": [-48.19, 5.94, -18.19],
        },
        "panel_size_mm": {
            "label": "标定时面板矩形 [长边, 短边] (mm)，0 = 不检查尺寸",
            "length": 2,
            "default": [241.4, 182.0],
        },
    },
}


def _vector_default(meta: dict[str, Any]) -> list[float]:
    default = meta.get("default")
    if default is None:
        return [0.0] * int(meta.get("length", 3))
    return [float(v) for v in default]
_VECTOR_ABS_MAX_MM = 2000.0


def target_model_vector_params(method: str) -> dict[str, dict[str, Any]]:
    return dict(_VECTOR_PARAMS.get(method, {}))


def default_target_model_params(method: str) -> dict[str, Any]:
    spec = TARGET_MODEL_PARAM_SPECS[method]
    result: dict[str, Any] = {
        key: limits["default"] for key, limits in spec.items()}
    for key, meta in _VECTOR_PARAMS.get(method, {}).items():
        result[key] = _vector_default(meta)
    return result


def _clean_vector(raw: Any, field: str, length: int,
                  default: list[float] | None = None) -> list[float]:
    if raw is None:
        return list(default) if default is not None else [0.0] * length
    if not isinstance(raw, (list, tuple)) or len(raw) != length:
        raise ValueError(f"{field} 必须是长度 {length} 的数组")
    values: list[float] = []
    for index, item in enumerate(raw):
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}[{index}] 必须是数字") from exc
        if not math.isfinite(number) or abs(number) > _VECTOR_ABS_MAX_MM:
            raise ValueError(
                f"{field}[{index}] 超范围：|值| ≤ {_VECTOR_ABS_MAX_MM:g} mm"
                f"（收到 {item}）")
        values.append(number)
    return values


def validate_target_model_params(
    method: str, value: Any, field: str = "target_model.params",
) -> dict[str, Any]:
    """按模型校验参数块；缺省键补默认，未知键拒绝，整数项取整。"""
    if method not in TARGET_MODEL_PARAM_SPECS:
        raise ValueError(
            f"未知自动选点模型 {method!r}"
            f"（支持 {' / '.join(TARGET_MODELS)}）")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 JSON object")
    spec = TARGET_MODEL_PARAM_SPECS[method]
    vectors = _VECTOR_PARAMS.get(method, {})
    unknown = set(value) - set(spec) - set(vectors)
    if unknown:
        raise ValueError(f"{field} 含未知参数：{sorted(unknown)}")
    result: dict[str, Any] = {}
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
    # 向量偏移是代码常量：注册表/请求里给的值一律忽略（和 0.2.0-s 写死常量一致），
    # 只校验格式以便及早发现写错的配置
    for key, meta in vectors.items():
        _clean_vector(value.get(key), f"{field}.{key}", int(meta.get("length", 3)))
        result[key] = _vector_default(meta)
    return result


def validate_target_model_config(
    value: Any, field: str = "target_model",
) -> dict[str, Any]:
    """校验顶层 target_model 配置；None/缺省 → knob_mask_center。"""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 JSON object")
    method = str(value.get("method") or DEFAULT_TARGET_MODEL)
    method = method.strip().lower()
    if method not in TARGET_MODELS:
        raise ValueError(
            f"{field}.method 只能是 {' / '.join(TARGET_MODELS)}"
            f"（收到 {value.get('method')!r}）")
    return {
        "method": method,
        "params": validate_target_model_params(
            method, value.get("params"), f"{field}.params"),
    }


def default_target_model_config() -> dict[str, Any]:
    return validate_target_model_config(None)


def panel_anchor_is_calibrated(params: dict[str, Any]) -> bool:
    """anchor / point1 / point3 至少有一个非零分量才算标定过。"""
    for key in ("anchor_offset_wall_mm", "point1_offset_wall_mm",
                "point3_offset_wall_mm"):
        if any(abs(float(v)) > 1e-9 for v in params.get(key, [])):
            return True
    return False


__all__ = [
    "DEFAULT_TARGET_MODEL",
    "KNOB_MASK_CENTER",
    "PANEL_ANCHOR",
    "TARGET_MODELS",
    "TARGET_MODEL_LABELS",
    "TARGET_MODEL_PARAM_SPECS",
    "TARGET_MODEL_VERSIONS",
    "default_target_model_config",
    "default_target_model_params",
    "panel_anchor_is_calibrated",
    "target_model_vector_params",
    "validate_target_model_config",
    "validate_target_model_params",
]
