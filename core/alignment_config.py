from __future__ import annotations

import json
import math
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALIGNMENT_CONFIG_PATH = PROJECT_ROOT / "config" / "waist_alignment.json"

DEFAULT_ALIGNMENT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "coarse": {
        "target_deg": -7.0,
        "command_tolerance_deg": 0.75,
        "accept_min_deg": -8.5,
        "accept_max_deg": 0.0,
    },
    "fine": {
        "target_deg": -3.0,
        "command_tolerance_deg": 1.5,
        "accept_min_deg": -5.0,
        "accept_max_deg": 5.0,
    },
}


def _finite_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数字")
    return number


def validate_alignment_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("腰部对齐配置必须是 JSON object")
    if int(payload.get("schema_version", -1)) != 1:
        raise ValueError("腰部对齐配置 schema_version 必须为 1")

    validated: dict[str, Any] = {"schema_version": 1}
    for stage in ("coarse", "fine"):
        raw = payload.get(stage)
        if not isinstance(raw, dict):
            raise ValueError(f"{stage} 必须是 JSON object")
        target = _finite_number(raw.get("target_deg"), f"{stage}.target_deg")
        command_tol = _finite_number(
            raw.get("command_tolerance_deg"),
            f"{stage}.command_tolerance_deg",
        )
        accept_min = _finite_number(
            raw.get("accept_min_deg"), f"{stage}.accept_min_deg"
        )
        accept_max = _finite_number(
            raw.get("accept_max_deg"), f"{stage}.accept_max_deg"
        )
        if not -30.0 <= accept_min < accept_max <= 30.0:
            raise ValueError(f"{stage} 验收范围必须满足 -30 ≤ 最小值 < 最大值 ≤ 30")
        if not accept_min <= target <= accept_max:
            raise ValueError(f"{stage} 目标角必须位于验收范围内")
        if not 0.1 <= command_tol <= 10.0:
            raise ValueError(f"{stage}.command_tolerance_deg 必须在 0.1~10°")
        if target - command_tol < accept_min or target + command_tol > accept_max:
            raise ValueError(f"{stage} 自动对中停止范围必须包含在流程验收范围内")
        validated[stage] = {
            "target_deg": target,
            "command_tolerance_deg": command_tol,
            "accept_min_deg": accept_min,
            "accept_max_deg": accept_max,
        }
    return validated


def load_alignment_config(
    path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        return deepcopy(DEFAULT_ALIGNMENT_CONFIG)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取腰部对齐配置 {config_path}: {exc}") from exc
    return validate_alignment_config(payload)


def save_alignment_config(
    payload: Any,
    path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
) -> dict[str, Any]:
    validated = validate_alignment_config(payload)
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
