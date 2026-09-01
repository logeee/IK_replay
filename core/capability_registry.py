"""四级能力注册表：臂侧 → 手型号 → 任务配置 → 实现方式。

18000 配置中心（tools/capability_server.py）读写本文件描述的注册表；
17001 调度启动时读取「激活组合」（臂 + 手型号）推导可接任务，并把该
组合的手眼标定路径传给 reach_server（重启生效，不做热切换）。

文件: config/capability_registry.json。首次加载不存在时按当前真机
已验证的两个动作生成种子（右臂 / 因时-右-1 / 旋钮右到左、左到右 / 拨动），
参数与现有代码默认值一致——注册表保持种子内容时行为完全不变。

手眼标定按「臂 + 手型号」组合归档：config/hand_eye/{arm}__{hand_id}/
handeye3d_result.json。一二级组合相同则共用同一份标定。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "capability_registry.json"
CALIB_ROOT = PROJECT_ROOT / "config" / "hand_eye"
CALIB_FILENAME = "handeye3d_result.json"

# 种子迁移时尝试从旧的固定路径复制标定（只在机器人本机存在）
LEGACY_CALIB_SOURCE = ("/home/robot/yx/project/calib/hand_eye_3D/"
                       "handeye3d_data/biaoding/handeye3d_result.json")

ARMS = ("right_arm", "left_arm")
ARM_LABELS = {"right_arm": "右臂", "left_arm": "左臂"}
DESIGN_SIDES = ("right", "left")
SITES = ("lab", "factory")
# 任务的物理方向：rtl=向左拨（右到左）、ltr=向右拨（左到右）、
# cw/ccw=顺/逆时针旋转（拧类任务预留）
DIRECTIONS = ("rtl", "ltr", "cw", "ccw")
NAME_MAX = 40
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

# 实现方式（四级）多态参数：每种方式各自的参数块与默认值。
# flick 默认值与 api/flow.py / adapters/reach/execution.py 现有默认一致。
METHOD_PARAM_SPECS: dict[str, dict[str, dict[str, float]]] = {
    "flick": {
        "sidestep_cm": {"default": 10.0, "min": 0.0, "max": 30.0},
        "push_force_n": {"default": 15.0, "min": 0.0, "max": 50.0},
        "push_hold_s": {"default": 1.5, "min": 0.0, "max": 5.0},
        "down_deg": {"default": 15.0, "min": -45.0, "max": 45.0},
    },
    # 拧（中心螺丝等）尚未实现：只留配置位，运行时返回 NOT_IMPLEMENTED
    "twist": {},
}
METHODS = tuple(METHOD_PARAM_SPECS)
METHOD_LABELS = {"flick": "拨动", "twist": "拧（未实现）"}
IMPLEMENTED_METHODS = frozenset({"flick"})

# 现有起手式命名正则（与 api/flow.py 的 NEW/LEFT_POSE_PATTERN 一致）
POSE_PATTERN_RTL = r"^\s*(\d+(?:\.\d+)?)-起手式新\s*$"
POSE_PATTERN_LTR = r"^\s*(\d+(?:\.\d+)?)-左-起手式\s*$"


def calib_rel_path(arm: str, hand_id: str) -> str:
    """标定归档的仓库相对路径（一二级组合唯一确定）。"""
    return f"config/hand_eye/{arm}__{hand_id}/{CALIB_FILENAME}"


def calib_abs_path(arm: str, hand_id: str,
                   root: Path = PROJECT_ROOT) -> Path:
    return Path(root) / calib_rel_path(arm, hand_id)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def seed_registry() -> dict[str, Any]:
    """已验证的两个动作作为种子：右臂 / 因时-右-1 / 拨动 ×2。

    task.sites 复刻原 SITE_SUPPORTED_KINDS：向左拨（rtl）lab+factory 都
    验证过，向右拨（ltr）只在 factory 验证过。
    """
    hand_id = "yinshi-right-1"
    return validate_registry({
        "schema_version": 1,
        "active": {"arm": "right_arm", "hand_id": hand_id},
        "hands": [{
            "id": hand_id,
            "name": "因时-右-1",
            "design_side": "right",
            "tool_out_mm": 15.0,
            "notes": "",
        }],
        "calibrations": [{
            "arm": "right_arm",
            "hand_id": hand_id,
            "source_path": LEGACY_CALIB_SOURCE,
            "registered_at": datetime.now().isoformat(timespec="seconds"),
        }],
        "capabilities": [
            {
                "id": "cap-rtl-flick",
                "arm": "right_arm",
                "hand_id": hand_id,
                "task": {"name": "旋钮右到左", "direction": "rtl",
                         "sites": ["lab", "factory"]},
                "method": "flick",
                "method_params": {},
                "assets": {"pose_pattern": POSE_PATTERN_RTL,
                           "endpoint_pattern": ""},
                "enabled": True,
                "notes": "真机已验证",
            },
            {
                "id": "cap-ltr-flick",
                "arm": "right_arm",
                "hand_id": hand_id,
                "task": {"name": "旋钮左到右", "direction": "ltr",
                         "sites": ["factory"]},
                "method": "flick",
                "method_params": {},
                "assets": {"pose_pattern": POSE_PATTERN_LTR,
                           "endpoint_pattern": ""},
                "enabled": True,
                "notes": "真机已验证（工厂柜）",
            },
        ],
    })


# ------------------------------------------------------------------ 校验


def _clean_name(value: Any, field: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError(f"{field} 不能为空")
    if len(name) > NAME_MAX:
        raise ValueError(f"{field}「{name[:12]}…」过长（限 {NAME_MAX} 字符）")
    return name


def _clean_id(value: Any, field: str, prefix: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return new_id(prefix)
    if not ID_RE.match(raw):
        raise ValueError(
            f"{field}「{raw}」非法：限小写字母/数字/中划线/下划线，"
            f"以字母或数字开头，≤48 字符")
    return raw


def _clean_arm(value: Any, field: str) -> str:
    arm = str(value or "").strip().lower()
    if arm not in ARMS:
        raise ValueError(f"{field} 只能是 {' / '.join(ARMS)}（收到 {value!r}）")
    return arm


def _clean_number(value: Any, field: str, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if not math.isfinite(number) or not lo <= number <= hi:
        raise ValueError(f"{field} 超范围：限 {lo:g}~{hi:g}（收到 {value}）")
    return number


def validate_method_params(method: str, value: Any,
                           field: str = "method_params") -> dict[str, float]:
    """按实现方式校验参数块；缺省键按该方式默认值补齐。"""
    if method not in METHOD_PARAM_SPECS:
        raise ValueError(f"未知实现方式 {method!r}（支持 {' / '.join(METHODS)}）")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 JSON object")
    spec = METHOD_PARAM_SPECS[method]
    unknown = set(value) - set(spec)
    if unknown:
        raise ValueError(f"{field} 含未知参数：{sorted(unknown)}")
    result: dict[str, float] = {}
    for key, limits in spec.items():
        raw = value.get(key)
        if raw is None:
            result[key] = limits["default"]
        else:
            result[key] = _clean_number(
                raw, f"{field}.{key}", limits["min"], limits["max"])
    return result


def _clean_pose_pattern(value: Any, field: str) -> str:
    pattern = str(value or "").strip()
    if not pattern:
        return ""
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{field} 不是合法正则：{exc}") from exc
    if compiled.groups < 1:
        raise ValueError(
            f"{field} 至少要有一个捕获组（起手式档位距离，如 (\\d+(?:\\.\\d+)?)）")
    return pattern


def _validate_hand(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"hands[{index}] 必须是 JSON object")
    side = str(raw.get("design_side") or "").strip().lower()
    if side not in DESIGN_SIDES:
        raise ValueError(
            f"hands[{index}].design_side 只能是 right / left（收到 "
            f"{raw.get('design_side')!r}）")
    return {
        "id": _clean_id(raw.get("id"), f"hands[{index}].id", "hand"),
        "name": _clean_name(raw.get("name"), f"hands[{index}].name"),
        "design_side": side,
        "tool_out_mm": _clean_number(
            raw.get("tool_out_mm", 15.0),
            f"hands[{index}].tool_out_mm", 0.0, 100.0),
        "notes": str(raw.get("notes") or "").strip(),
    }


def _validate_calibration(raw: Any, index: int,
                          hand_ids: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"calibrations[{index}] 必须是 JSON object")
    arm = _clean_arm(raw.get("arm"), f"calibrations[{index}].arm")
    hand_id = str(raw.get("hand_id") or "").strip()
    if hand_id not in hand_ids:
        raise ValueError(
            f"calibrations[{index}].hand_id 指向不存在的手型号「{hand_id}」")
    return {
        "arm": arm,
        "hand_id": hand_id,
        "path": calib_rel_path(arm, hand_id),
        "source_path": str(raw.get("source_path") or "").strip(),
        "registered_at": str(raw.get("registered_at") or "").strip(),
    }


def _validate_capability(raw: Any, index: int,
                         hand_ids: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"capabilities[{index}] 必须是 JSON object")
    field = f"capabilities[{index}]"
    hand_id = str(raw.get("hand_id") or "").strip()
    if hand_id not in hand_ids:
        raise ValueError(f"{field}.hand_id 指向不存在的手型号「{hand_id}」")

    task_raw = raw.get("task")
    if not isinstance(task_raw, dict):
        raise ValueError(f"{field}.task 必须是 JSON object")
    direction = str(task_raw.get("direction") or "").strip().lower()
    if direction not in DIRECTIONS:
        raise ValueError(
            f"{field}.task.direction 只能是 {' / '.join(DIRECTIONS)}"
            f"（收到 {task_raw.get('direction')!r}）")
    sites_raw = task_raw.get("sites")
    if sites_raw is None:
        sites_raw = list(SITES)
    if not isinstance(sites_raw, list):
        raise ValueError(f"{field}.task.sites 必须是数组")
    sites: list[str] = []
    for site in sites_raw:
        clean = str(site or "").strip().lower()
        if clean not in SITES:
            raise ValueError(
                f"{field}.task.sites 含未知现场 {site!r}（支持 lab / factory）")
        if clean not in sites:
            sites.append(clean)

    method = str(raw.get("method") or "").strip().lower()
    if method not in METHODS:
        raise ValueError(
            f"{field}.method 只能是 {' / '.join(METHODS)}（收到 "
            f"{raw.get('method')!r}）")

    assets_raw = raw.get("assets")
    if assets_raw is None:
        assets_raw = {}
    if not isinstance(assets_raw, dict):
        raise ValueError(f"{field}.assets 必须是 JSON object")

    return {
        "id": _clean_id(raw.get("id"), f"{field}.id", "cap"),
        "arm": _clean_arm(raw.get("arm"), f"{field}.arm"),
        "hand_id": hand_id,
        "task": {
            "name": _clean_name(task_raw.get("name"), f"{field}.task.name"),
            "direction": direction,
            "sites": sites,
        },
        "method": method,
        "method_params": validate_method_params(
            method, raw.get("method_params"), f"{field}.method_params"),
        "assets": {
            "pose_pattern": _clean_pose_pattern(
                assets_raw.get("pose_pattern"), f"{field}.assets.pose_pattern"),
            "endpoint_pattern": str(
                assets_raw.get("endpoint_pattern") or "").strip(),
        },
        "enabled": bool(raw.get("enabled", True)),
        "notes": str(raw.get("notes") or "").strip(),
    }


def validate_registry(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("注册表必须是 JSON object")
    version = int(payload.get("schema_version", -1))
    if version != 1:
        raise ValueError("注册表 schema_version 必须为 1")

    raw_hands = payload.get("hands")
    if raw_hands is None:
        raw_hands = []
    if not isinstance(raw_hands, list):
        raise ValueError("hands 必须是数组")
    hands = [_validate_hand(raw, i) for i, raw in enumerate(raw_hands)]
    hand_ids: set[str] = set()
    hand_names: set[str] = set()
    for hand in hands:
        if hand["id"] in hand_ids:
            raise ValueError(f"手型号 id「{hand['id']}」重复")
        if hand["name"] in hand_names:
            raise ValueError(f"手型号名「{hand['name']}」重复")
        hand_ids.add(hand["id"])
        hand_names.add(hand["name"])

    raw_calibs = payload.get("calibrations")
    if raw_calibs is None:
        raw_calibs = []
    if not isinstance(raw_calibs, list):
        raise ValueError("calibrations 必须是数组")
    calibrations = []
    combos: set[tuple[str, str]] = set()
    for i, raw in enumerate(raw_calibs):
        calib = _validate_calibration(raw, i, hand_ids)
        combo = (calib["arm"], calib["hand_id"])
        if combo in combos:
            raise ValueError(
                f"标定组合重复：{calib['arm']} + {calib['hand_id']}")
        combos.add(combo)
        calibrations.append(calib)

    raw_caps = payload.get("capabilities")
    if raw_caps is None:
        raw_caps = []
    if not isinstance(raw_caps, list):
        raise ValueError("capabilities 必须是数组")
    capabilities = []
    cap_ids: set[str] = set()
    for i, raw in enumerate(raw_caps):
        cap = _validate_capability(raw, i, hand_ids)
        if cap["id"] in cap_ids:
            raise ValueError(f"能力 id「{cap['id']}」重复")
        cap_ids.add(cap["id"])
        capabilities.append(cap)

    raw_active = payload.get("active")
    active: dict[str, str] | None = None
    if raw_active:
        if not isinstance(raw_active, dict):
            raise ValueError("active 必须是 JSON object")
        arm = _clean_arm(raw_active.get("arm"), "active.arm")
        hand_id = str(raw_active.get("hand_id") or "").strip()
        if hand_id not in hand_ids:
            raise ValueError(
                f"active.hand_id 指向不存在的手型号「{hand_id}」")
        active = {"arm": arm, "hand_id": hand_id}

    return {
        "schema_version": 1,
        "active": active,
        "hands": hands,
        "calibrations": calibrations,
        "capabilities": capabilities,
    }


# ------------------------------------------------------------------ 读写


def load_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    """读取注册表；文件不存在时返回种子（不落盘，由调用方决定是否保存）。"""
    registry_path = Path(path).expanduser().resolve()
    if not registry_path.exists():
        return seed_registry()
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取能力注册表 {registry_path}: {exc}") from exc
    return validate_registry(payload)


def save_registry(payload: Any,
                  path: str | Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    validated = validate_registry(payload)
    registry_path = Path(path).expanduser().resolve()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{registry_path.name}.",
        suffix=".tmp",
        dir=registry_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(validated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, registry_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return validated


def ensure_registry(
    path: str | Path = DEFAULT_REGISTRY_PATH,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """加载注册表；首次运行落盘种子，并尽力从旧路径归档种子标定。"""
    registry_path = Path(path).expanduser().resolve()
    first_run = not registry_path.exists()
    registry = load_registry(registry_path)
    if first_run:
        save_registry(registry, registry_path)
    for calib in registry["calibrations"]:
        try_import_calibration(calib, root)
    return registry


# ------------------------------------------------------------------ 标定


def try_import_calibration(calib: dict[str, Any],
                           root: Path = PROJECT_ROOT) -> bool:
    """归档文件缺失且 source_path 存在时复制入库；返回归档文件是否就绪。"""
    target = Path(root) / calib["path"]
    if target.is_file():
        return True
    source = calib.get("source_path") or ""
    source_path = Path(source).expanduser() if source else None
    if source_path and source_path.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)
        return True
    return False


def calibration_info(registry: dict[str, Any], arm: str, hand_id: str,
                     root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """某组合的标定状态：ready（归档就绪）/ pending（待补）/ missing（未登记）。

    ready 时附带标定文件里的 solved_at / residual_mm / num_samples。
    """
    entry = None
    for calib in registry.get("calibrations") or []:
        if calib["arm"] == arm and calib["hand_id"] == hand_id:
            entry = calib
            break
    rel_path = calib_rel_path(arm, hand_id)
    abs_path = Path(root) / rel_path
    info: dict[str, Any] = {
        "arm": arm,
        "hand_id": hand_id,
        "path": rel_path,
        "status": "missing" if entry is None else "pending",
        "source_path": (entry or {}).get("source_path", ""),
        "registered_at": (entry or {}).get("registered_at", ""),
        "solved_at": None,
        "residual_mm": None,
        "num_samples": None,
    }
    if abs_path.is_file():
        info["status"] = "ready"
        try:
            payload = json.loads(abs_path.read_text(encoding="utf-8"))
            info["solved_at"] = payload.get("solved_at")
            info["residual_mm"] = payload.get("residual_mm")
            info["num_samples"] = payload.get("num_samples")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return info


# ------------------------------------------------------------------ 查询


def find_hand(registry: dict[str, Any], hand_id: str) -> dict[str, Any] | None:
    for hand in registry.get("hands") or []:
        if hand["id"] == hand_id:
            return hand
    return None


def find_capability(registry: dict[str, Any],
                    cap_id: str) -> dict[str, Any] | None:
    for cap in registry.get("capabilities") or []:
        if cap["id"] == cap_id:
            return cap
    return None


def active_combo(registry: dict[str, Any]) -> dict[str, str] | None:
    return registry.get("active")


def enabled_capabilities(registry: dict[str, Any], arm: str,
                         hand_id: str) -> list[dict[str, Any]]:
    return [cap for cap in registry.get("capabilities") or []
            if cap["enabled"] and cap["arm"] == arm
            and cap["hand_id"] == hand_id]


def capability_for(registry: dict[str, Any], arm: str, hand_id: str,
                   direction: str, site: str) -> dict[str, Any] | None:
    """激活组合下，某物理方向 + 现场对应的已启用能力（无则 None）。"""
    for cap in enabled_capabilities(registry, arm, hand_id):
        if (cap["task"]["direction"] == direction
                and site in cap["task"]["sites"]):
            return cap
    return None
