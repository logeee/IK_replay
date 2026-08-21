from __future__ import annotations

import json
import math
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GRAVITY_PROFILES_PATH = PROJECT_ROOT / "config" / "gravity_compensation.json"
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

BASELINE_PROFILE: dict[str, Any] = {
    "version": "0.0.0",
    "label": "未标定前的重力补偿版本",
    "description": "建立版本管理前正在使用的参数快照",
    "created_at": "2026-08-21T09:32:00+08:00",
    "parent_version": None,
    "source": "baseline",
    "parameters": {
        "grav_alpha": 1.0,
        "payload_kg": 0.0,
        "grav_in_float": True,
        "use_imu_gravity": False,
    },
}

DEFAULT_GRAVITY_PROFILES: dict[str, Any] = {
    "schema_version": 1,
    "active_version": "0.0.0",
    "versions": [BASELINE_PROFILE],
}


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数字")
    return number


def validate_parameters(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("parameters 必须是 JSON object")
    grav_alpha = _finite(payload.get("grav_alpha"), "grav_alpha")
    payload_kg = _finite(payload.get("payload_kg"), "payload_kg")
    if not 0.0 <= grav_alpha <= 1.2:
        raise ValueError("grav_alpha 必须在 0.0~1.2")
    if not 0.0 <= payload_kg <= 20.0:
        raise ValueError("payload_kg 必须在 0.0~20.0kg")
    for name in ("grav_in_float", "use_imu_gravity"):
        if not isinstance(payload.get(name), bool):
            raise ValueError(f"{name} 必须是 boolean")
    return {
        "grav_alpha": grav_alpha,
        "payload_kg": payload_kg,
        "grav_in_float": payload["grav_in_float"],
        "use_imu_gravity": payload["use_imu_gravity"],
    }


def validate_profile(payload: Any, *, known_versions: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("重力补偿版本必须是 JSON object")
    version = str(payload.get("version") or "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("版本号必须使用 x.y.z 格式，例如 0.1.0")
    label = str(payload.get("label") or "").strip()
    description = str(payload.get("description") or "").strip()
    if not label or len(label) > 100:
        raise ValueError("版本名称不能为空且不能超过100字")
    if len(description) > 500:
        raise ValueError("版本说明不能超过500字")
    parent = payload.get("parent_version")
    if parent is not None:
        parent = str(parent)
        if parent not in known_versions:
            raise ValueError(f"父版本不存在: {parent}")
    created_at = str(payload.get("created_at") or "").strip()
    if not created_at:
        raise ValueError("created_at 不能为空")
    return {
        "version": version,
        "label": label,
        "description": description,
        "created_at": created_at,
        "parent_version": parent,
        "source": str(payload.get("source") or "manual"),
        "parameters": validate_parameters(payload.get("parameters")),
    }


def validate_registry(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("重力补偿版本库必须是 JSON object")
    if int(payload.get("schema_version", -1)) != 1:
        raise ValueError("重力补偿版本库 schema_version 必须为 1")
    raw_versions = payload.get("versions")
    if not isinstance(raw_versions, list) or not raw_versions:
        raise ValueError("versions 至少需要一个版本")
    versions: list[dict[str, Any]] = []
    known: set[str] = set()
    # Parent references are intentionally restricted to an earlier immutable
    # snapshot, keeping history acyclic and easy to audit.
    for raw in raw_versions:
        profile = validate_profile(raw, known_versions=known)
        if profile["version"] in known:
            raise ValueError(f"版本号重复: {profile['version']}")
        known.add(profile["version"])
        versions.append(profile)
    active = str(payload.get("active_version") or "")
    if active not in known:
        raise ValueError(f"当前启用版本不存在: {active}")
    return {
        "schema_version": 1,
        "active_version": active,
        "versions": versions,
    }


def load_registry(
    path: str | Path = DEFAULT_GRAVITY_PROFILES_PATH,
) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        return deepcopy(DEFAULT_GRAVITY_PROFILES)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取重力补偿版本库 {config_path}: {exc}") from exc
    return validate_registry(payload)


def save_registry(
    payload: Any,
    path: str | Path = DEFAULT_GRAVITY_PROFILES_PATH,
) -> dict[str, Any]:
    validated = validate_registry(payload)
    config_path = Path(path).expanduser().resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
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


def active_profile(
    registry: dict[str, Any],
    version: str | None = None,
) -> dict[str, Any]:
    selected = str(version or registry["active_version"])
    for profile in registry["versions"]:
        if profile["version"] == selected:
            return deepcopy(profile)
    raise ValueError(f"重力补偿版本不存在: {selected}")


def create_profile(
    *,
    version: str,
    label: str,
    description: str,
    parameters: dict[str, Any],
    path: str | Path = DEFAULT_GRAVITY_PROFILES_PATH,
    activate: bool = False,
    source: str = "manual",
) -> dict[str, Any]:
    registry = load_registry(path)
    if any(item["version"] == version for item in registry["versions"]):
        raise ValueError(f"版本 {version} 已存在；历史版本不可覆盖")
    profile = {
        "version": version,
        "label": label,
        "description": description,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "parent_version": registry["active_version"],
        "source": source,
        "parameters": parameters,
    }
    # Validate before mutating the registry so a failed save remains atomic.
    validated = validate_profile(
        profile,
        known_versions={item["version"] for item in registry["versions"]},
    )
    registry["versions"].append(validated)
    if activate:
        registry["active_version"] = validated["version"]
    save_registry(registry, path)
    return validated


def activate_profile(
    version: str,
    path: str | Path = DEFAULT_GRAVITY_PROFILES_PATH,
) -> dict[str, Any]:
    registry = load_registry(path)
    profile = active_profile(registry, version)
    registry["active_version"] = profile["version"]
    save_registry(registry, path)
    return profile
