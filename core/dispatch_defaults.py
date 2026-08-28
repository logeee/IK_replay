"""17001 外部调用默认配置：默认现场 + 命名的目的点偏移配置。

外部平台调 /task/flip、/check/flip 时通常只带 language；这里保存的默认
现场（lab/factory）和默认偏移配置（墙面系 mm，如「右手偏移配置-1」）
会自动套用。请求 body 里显式给了 site / target_offset_wall_mm 时以请求
为准——网页手动单次测试用的就是显式参数，和默认配置互不干扰。

文件: config/dispatch_defaults.json，由 17001 网页读改存。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DISPATCH_DEFAULTS_PATH = (
    PROJECT_ROOT / "config" / "dispatch_defaults.json"
)

SITES = ("lab", "factory")
OFFSET_LIMIT_MM = 50.0     # 单轴上限，与 /task/flip 的校验一致
PRESET_NAME_MAX = 40
LIFT_LIMIT_MM = 50.0       # 拨点上抬各项上限（首轮/每轮递增/封顶）

# 拨点上抬（抵消重力下垂）出厂值：首轮 10 mm，每重试一轮 +10 mm，封顶 30 mm
DEFAULT_LIFT_MM: dict[str, float] = {"base": 10.0, "step": 10.0, "max": 30.0}
FLIP_KINDS = ("close_to_remote", "remote_to_close")

DEFAULT_DISPATCH_DEFAULTS: dict[str, Any] = {
    "schema_version": 2,
    "defaults": {
        "site": "factory",
        # 两个任务方向独立标定；"" = 不套偏移配置。
        "offset_preset_by_kind": {
            "close_to_remote": "",
            "remote_to_close": "",
        },
        "lift_mm": dict(DEFAULT_LIFT_MM),
    },
    "offset_presets": [],
}


def _offset_axis(value: Any, name: str) -> float:
    try:
        number = float(0.0 if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字（mm）") from exc
    if not math.isfinite(number) or abs(number) > OFFSET_LIMIT_MM:
        raise ValueError(
            f"{name} 超范围：单轴限 ±{OFFSET_LIMIT_MM:g} mm（收到 {value}）"
        )
    return number


def validate_lift_mm(value: Any, name: str = "lift_mm") -> dict[str, float]:
    """拨点上抬 {"base":首轮,"step":每轮递增,"max":封顶}（mm，0~50）。

    缺省键按出厂值补齐；base=首轮就抬多少，之后每重试一轮加 step，
    合计不超过 max（max 小于 base 时等效于所有轮都按 max 抬）。
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是 {{base,step,max}} 对象（单位 mm）")
    result: dict[str, float] = {}
    for key in ("base", "step", "max"):
        raw = value.get(key)
        if raw is None:
            result[key] = DEFAULT_LIFT_MM[key]
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}.{key} 必须是数字（mm）") from exc
        if not math.isfinite(number) or not 0 <= number <= LIFT_LIMIT_MM:
            raise ValueError(
                f"{name}.{key} 超范围：限 0~{LIFT_LIMIT_MM:g} mm（收到 {raw}）"
            )
        result[key] = number
    return result


def validate_offset_mm(value: Any, name: str = "offset_mm") -> dict[str, float]:
    """{"x":右,"y":入墙,"z":上}（mm），缺省轴按 0。"""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是 {{x,y,z}} 对象（单位 mm）")
    return {
        axis: _offset_axis(value.get(axis), f"{name}.{axis}")
        for axis in ("x", "y", "z")
    }


def validate_dispatch_defaults(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("默认配置必须是 JSON object")
    version = int(payload.get("schema_version", -1))
    if version not in (1, 2):
        raise ValueError("默认配置 schema_version 必须为 1 或 2")

    raw_presets = payload.get("offset_presets")
    if raw_presets is None:
        raw_presets = []
    if not isinstance(raw_presets, list):
        raise ValueError("offset_presets 必须是数组")
    presets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_presets):
        if not isinstance(raw, dict):
            raise ValueError(f"offset_presets[{index}] 必须是 JSON object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"offset_presets[{index}].name 不能为空")
        if len(name) > PRESET_NAME_MAX:
            raise ValueError(
                f"配置名「{name[:12]}…」过长（限 {PRESET_NAME_MAX} 字符）"
            )
        if name in seen:
            raise ValueError(f"配置名「{name}」重复")
        seen.add(name)
        presets.append({
            "name": name,
            "offset_mm": validate_offset_mm(
                raw.get("offset_mm"), f"offset_presets[{index}].offset_mm"
            ),
        })

    raw_defaults = payload.get("defaults")
    if not isinstance(raw_defaults, dict):
        raise ValueError("defaults 必须是 JSON object")
    site = str(raw_defaults.get("site") or "").strip().lower()
    if site not in SITES:
        raise ValueError("defaults.site 只能是 lab 或 factory")
    # v1 只有一个 offset_preset；迁移时先让两个方向都沿用它，避免旧配置失效。
    legacy_preset = str(raw_defaults.get("offset_preset") or "").strip()
    raw_by_kind = raw_defaults.get("offset_preset_by_kind")
    if raw_by_kind is None:
        raw_by_kind = {kind: legacy_preset for kind in FLIP_KINDS}
    if not isinstance(raw_by_kind, dict):
        raise ValueError("defaults.offset_preset_by_kind 必须是对象")
    preset_by_kind: dict[str, str] = {}
    for kind in FLIP_KINDS:
        preset_name = str(raw_by_kind.get(kind) or "").strip()
        if preset_name and preset_name not in seen:
            raise ValueError(
                f"defaults.offset_preset_by_kind.{kind} "
                f"指向不存在的配置「{preset_name}」"
            )
        preset_by_kind[kind] = preset_name
    lift_mm = validate_lift_mm(raw_defaults.get("lift_mm"), "defaults.lift_mm")

    return {
        "schema_version": 2,
        "defaults": {
            "site": site,
            "offset_preset_by_kind": preset_by_kind,
            "lift_mm": lift_mm,
        },
        "offset_presets": presets,
    }


def load_dispatch_defaults(
    path: str | Path = DEFAULT_DISPATCH_DEFAULTS_PATH,
) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        return deepcopy(DEFAULT_DISPATCH_DEFAULTS)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取默认配置 {config_path}: {exc}") from exc
    return validate_dispatch_defaults(payload)


def save_dispatch_defaults(
    payload: Any,
    path: str | Path = DEFAULT_DISPATCH_DEFAULTS_PATH,
) -> dict[str, Any]:
    validated = validate_dispatch_defaults(payload)
    config_path = Path(path).expanduser().resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        dir=config_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(validated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return validated


def find_offset_preset(
    config: dict[str, Any], name: str
) -> dict[str, Any] | None:
    for preset in config.get("offset_presets") or []:
        if preset.get("name") == name:
            return preset
    return None
