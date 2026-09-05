"""四级能力注册表：臂侧 → 手型号 → 任务配置 → 实现方式。

18000 配置中心（tools/capability_server.py）读写本文件描述的注册表；
17001 调度启动时读取「激活组合」（臂 + 手型号）推导可接任务，并把该
组合的手眼标定路径传给 reach_server（重启生效，不做热切换）。

文件: config/capability_registry.json。首次加载不存在时按当前真机
已验证的两个动作生成种子（右臂 / 因时-右-1 / 旋钮右到左、左到右 / 拨动），
参数与现有代码默认值一致——注册表保持种子内容时行为完全不变。

手眼标定按「臂 + 手型号」组合归档：config/hand_eye/{arm}__{hand_id}/
handeye3d_result.json。一二级组合相同则共用同一份标定。

起手式认领（sequence_claims）：data/sequences 是全组合共享的公共动作池，
认领挂在**能力条目**（臂+手+任务+方式）上——拨和扭是不同条目，各认各的
起手式，互不影响；未认领的动作在该条目选档时不可用（严格模式）。18001
录制新序列时上报（臂+手+动作名），18000 拿动作名匹配该组合各条目的起手
式正则（条目没配就用方向内置正则），命中谁自动认领给谁；谁都不命中就留
池待手动认领。历史存量在首次迁移时按正则拆给 右臂+因时-右-1 的条目。
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
# 公共动作池：18001 录制的动作序列（相对项目根）
SEQUENCES_SUBDIR = Path("data") / "sequences"
# 序列文件名的时间戳后缀（同名多文件 = 同一动作的多次录制）
SEQUENCE_STAMP_RE = re.compile(r"_\d{8}_\d{6}$")
# 存量序列迁移时的默认归属组合（历史轨迹都是该组合录制的）
LEGACY_SEQUENCE_COMBO = ("right_arm", "yinshi-1-right")
# 方向内置起手式正则（能力条目没配 pose_pattern 时的兜底；与 api/flow.py
# 的选档行为一致——flow 从这里取，保持单一来源）。第 1 捕获组 = 档位距离 m。
BUILTIN_POSE_PATTERNS: dict[str, str] = {
    "rtl": r"^\s*(\d+(?:\.\d+)?)-起手式新\s*$",
    "ltr": r"^\s*(\d+(?:\.\d+)?)-左-起手式\s*$",
}
# 位点池：18001 录制的单个路点（相对项目根）
WAYPOINTS_SUBDIR = Path("data") / "waypoints"
# flick 流程固定要用的两个公共位点（api/flow.py 的起手式起点 + 回落点）；
# 迁移时给存量 flick 条目预置，新条目由用户在页面自行挑选
FLOW_REQUIRED_WAYPOINTS: tuple[str, ...] = ("录制点位1", "起手点测试")

# 种子迁移时尝试从旧的固定路径复制标定（只在机器人本机存在）
LEGACY_CALIB_SOURCE = ("/home/robot/yx/project/calib/hand_eye_3D/"
                       "handeye3d_data/biaoding/handeye3d_result.json")

ARMS = ("right_arm", "left_arm")
ARM_LABELS = {"right_arm": "右臂", "left_arm": "左臂"}
# 18001 运动后端：legacy=原按节拍下发关节路点；pink=世界系 PINK 闭环跟踪
# （补偿躯干漂移，需 pinocchio/pin-pink）。切换后重启 18001 生效。
MOTION_BACKENDS = ("legacy", "pink")
MOTION_BACKEND_LABELS = {"legacy": "原方案（关节路点直发）",
                         "pink": "PINK 世界系闭环跟踪"}
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
    hand_id = "yinshi-1-right"
    return validate_registry({
        "schema_version": 1,
        "active": {"arm": "right_arm", "hand_id": hand_id},
        "hands": [{
            "id": hand_id,
            "name": "因时-右-1",
            "design_side": "right",
            "tool_out_mm": 15.0,
            "hand_web_device_id": "inspire_dfx",
            "tcp_point_id": "tip:R_index_tip",
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
    hand_web_device_id = str(raw.get("hand_web_device_id") or "").strip()
    if hand_web_device_id and not ID_RE.fullmatch(hand_web_device_id):
        raise ValueError(
            f"hands[{index}].hand_web_device_id 只能含小写字母、数字、_、-")
    tcp_point_id = str(raw.get("tcp_point_id") or "").strip()
    return {
        "id": _clean_id(raw.get("id"), f"hands[{index}].id", "hand"),
        "name": _clean_name(raw.get("name"), f"hands[{index}].name"),
        "design_side": side,
        "tool_out_mm": _clean_number(
            raw.get("tool_out_mm", 15.0),
            f"hands[{index}].tool_out_mm", 0.0, 100.0),
        "hand_web_device_id": hand_web_device_id,
        "tcp_point_id": tcp_point_id,
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


def _validate_sequence_claim(raw: Any, index: int,
                             capability_ids: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"sequence_claims[{index}] 必须是 JSON object")
    capability_id = str(raw.get("capability_id") or "").strip()
    if capability_id not in capability_ids:
        raise ValueError(
            f"sequence_claims[{index}].capability_id "
            f"指向不存在的能力条目「{capability_id}」")
    def _clean_names(key: str) -> list[str]:
        values = raw.get(key)
        if values is None:
            values = []
        if not isinstance(values, list):
            raise ValueError(f"sequence_claims[{index}].{key} 必须是数组")
        cleaned: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return sorted(cleaned)

    return {
        "capability_id": capability_id,
        # 认领的起手式动作名
        "names": _clean_names("names"),
        # 手选的非终点位点名（终点位点不落库：由已认领起手式自动推导）
        "waypoint_names": _clean_names("waypoint_names"),
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

    raw_claims = payload.get("sequence_claims")
    if raw_claims is None:
        raw_claims = []
    if not isinstance(raw_claims, list):
        raise ValueError("sequence_claims 必须是数组")
    capability_ids = {c["id"] for c in capabilities}
    sequence_claims = []
    claimed_caps: set[str] = set()
    for i, raw in enumerate(raw_claims):
        claim = _validate_sequence_claim(raw, i, capability_ids)
        if claim["capability_id"] in claimed_caps:
            raise ValueError(
                f"起手式认领条目重复：{claim['capability_id']}")
        claimed_caps.add(claim["capability_id"])
        sequence_claims.append(claim)

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
        motion_backend = str(
            raw_active.get("motion_backend") or "legacy").strip().lower()
        if motion_backend not in MOTION_BACKENDS:
            raise ValueError(
                f"active.motion_backend 必须是 {'/'.join(MOTION_BACKENDS)}，"
                f"收到「{motion_backend}」")
        active = {"arm": arm, "hand_id": hand_id,
                  "motion_backend": motion_backend}

    return {
        "schema_version": 1,
        "active": active,
        "hands": hands,
        "calibrations": calibrations,
        "capabilities": capabilities,
        "sequence_claims": sequence_claims,
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

    ready 时附带标定文件里的 solved_at / residual_mm / num_samples；若归档的
    是 hand_eye_3D 的合并版结果（含手安装标定 T_wrist2hand），一并暴露安装
    标定状态和按食指指尖推导的 tool_out_mm 建议值（仅供参考，登记值仍是权威）。
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
        "has_mount": False,
        "mount_solved_at": None,
        "mount_residual_mm": None,
        "suggested_tool_out_mm": None,
    }
    if abs_path.is_file():
        info["status"] = "ready"
        try:
            payload = json.loads(abs_path.read_text(encoding="utf-8"))
            info["solved_at"] = payload.get("solved_at")
            info["residual_mm"] = payload.get("residual_mm")
            info["num_samples"] = payload.get("num_samples")
            if payload.get("T_wrist2hand") is not None:
                info["has_mount"] = True
                info["mount_solved_at"] = payload.get("mount_solved_at")
                info["mount_residual_mm"] = payload.get("mount_residual_mm")
                info["suggested_tool_out_mm"] = _suggested_tool_out_mm(payload)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return info


def _suggested_tool_out_mm(payload: dict[str, Any]) -> float | None:
    """由安装标定推导 tool_out_mm 建议值。

    18001 的 TCP = p_tool 沿腕系 +x 外移 tool_out_mm；安装标定给出了食指
    指尖在腕系的真实坐标，两者 x 分量之差就是应补的外移量。超出登记
    范围（0~100mm）视为数据异常，不给建议。
    """
    try:
        p_tool = payload.get("p_tool_wrist_m")
        tips = payload.get("tcp_points_wrist_m") or []
        if not isinstance(p_tool, list) or len(p_tool) != 3:
            return None
        index_tip = next(
            (tip for tip in tips
             if isinstance(tip, dict) and "index" in str(tip.get("id", "")).lower()),
            None,
        )
        if index_tip is None:
            return None
        delta_mm = (float(index_tip["p_wrist_m"][0]) - float(p_tool[0])) * 1000.0
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    if not math.isfinite(delta_mm) or not 0.0 <= delta_mm <= 100.0:
        return None
    return round(delta_mm, 1)


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


# ------------------------------------------------------------------ 起手式认领


def claimed_sequence_names(registry: dict[str, Any],
                           capability_id: str) -> list[str]:
    """能力条目已认领的动作名列表；没有认领记录视为空（严格：没认领=不可用）。"""
    for claim in registry.get("sequence_claims") or []:
        if claim["capability_id"] == capability_id:
            return list(claim["names"])
    return []


def effective_pose_pattern(capability: dict[str, Any]) -> str | None:
    """条目实际生效的起手式正则：自配的优先，否则按任务方向取内置。

    cw/ccw 等没有内置正则的方向返回 None（不参与自动认领路由）。
    """
    pattern = str(capability.get("assets", {}).get("pose_pattern") or "")
    if pattern:
        return pattern
    return BUILTIN_POSE_PATTERNS.get(capability["task"]["direction"])


def route_sequence_claim(registry: dict[str, Any], arm: str, hand_id: str,
                         name: str) -> list[str]:
    """自动认领路由：动作名命中组合下哪些条目的起手式正则，就归谁。

    含停用条目（录制时临时停用不该丢认领）；正则非法或方向无内置正则的
    条目跳过。返回命中的 capability_id 列表（可能为空 = 留池待手动认领）。
    """
    matched: list[str] = []
    for cap in registry.get("capabilities") or []:
        if cap["arm"] != arm or cap["hand_id"] != hand_id:
            continue
        pattern = effective_pose_pattern(cap)
        if not pattern:
            continue
        try:
            if re.match(pattern, name):
                matched.append(cap["id"])
        except re.error:
            continue
    return matched


def derive_endpoint_name(sequence_name: str,
                         last_waypoint_file: str | None = None) -> str:
    """起手式配套终点位点名，与 api/flow.py 的运行时规则一致。

    优先用序列最后一个路点文件名去掉时间戳（choose_opening_pose 同款）；
    序列不含路点时按命名规则兜底（_pose_endpoint_name 同款）：
    「X-左-起手式」→「X-左-终点」，其余 →「X…终点」。
    """
    if last_waypoint_file:
        return re.sub(r"_\d{8}_\d{6}\.json$", "", str(last_waypoint_file))
    name = str(sequence_name or "").strip()
    if not name:
        return ""
    if re.match(BUILTIN_POSE_PATTERNS["ltr"], name):
        return re.sub(r"-起手式$", "-终点", name)
    return f"{name}终点"


def sequence_pool(root: Path = PROJECT_ROOT) -> list[dict[str, Any]]:
    """扫描公共动作池（data/sequences），按动作名聚合。

    同名多时间戳文件视为同一动作的多次录制，取 created_at 最新的一份的
    元数据（chain_id / recorded_combo / endpoint_name）。文件损坏跳过。
    """
    sequences_dir = Path(root) / SEQUENCES_SUBDIR
    if not sequences_dir.is_dir():
        return []
    by_name: dict[str, dict[str, Any]] = {}
    for path in sorted(sequences_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "").strip()
        if not name:
            name = SEQUENCE_STAMP_RE.sub("", path.stem)
        entry = by_name.setdefault(name, {
            "name": name,
            "files": 0,
            "latest_file": "",
            "latest_created_at": "",
            "chain_id": None,
            "recorded_combo": None,
            "endpoint_name": "",
        })
        entry["files"] += 1
        created = str(data.get("created_at") or "")
        if created >= entry["latest_created_at"]:
            entry["latest_created_at"] = created
            entry["latest_file"] = path.name
            entry["chain_id"] = data.get("chain_id")
            combo = data.get("recorded_combo")
            entry["recorded_combo"] = combo if isinstance(combo, dict) else None
            waypoints = data.get("waypoints") or []
            entry["endpoint_name"] = derive_endpoint_name(
                name, str(waypoints[-1]) if waypoints else None)
    return sorted(by_name.values(), key=lambda item: item["name"])


def waypoint_pool(root: Path = PROJECT_ROOT) -> list[dict[str, Any]]:
    """扫描位点池（data/waypoints），按位点名聚合（同名=多次录制）。"""
    waypoints_dir = Path(root) / WAYPOINTS_SUBDIR
    if not waypoints_dir.is_dir():
        return []
    by_name: dict[str, dict[str, Any]] = {}
    for path in sorted(waypoints_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "").strip()
        if not name:
            name = SEQUENCE_STAMP_RE.sub("", path.stem)
        entry = by_name.setdefault(name, {
            "name": name,
            "files": 0,
            "latest_file": "",
            "latest_created_at": "",
            "chain_id": None,
        })
        entry["files"] += 1
        created = str(data.get("created_at") or "")
        if created >= entry["latest_created_at"]:
            entry["latest_created_at"] = created
            entry["latest_file"] = path.name
            entry["chain_id"] = data.get("chain_id")
    return sorted(by_name.values(), key=lambda item: item["name"])


def claimed_waypoint_names(registry: dict[str, Any], capability_id: str,
                           pool: list[dict[str, Any]]) -> list[str]:
    """条目生效的位点集合 = 手选位点 ∪ 已认领起手式的推导终点。

    pool 传 sequence_pool()（或 18000 payload 里的 sequence_pool）；
    已认领但不在池中的起手式按命名规则兜底推导，保证运行时超集。
    """
    claim = next((c for c in registry.get("sequence_claims") or []
                  if c["capability_id"] == capability_id), None)
    if claim is None:
        return []
    endpoint_by_name = {
        str(entry.get("name") or ""): str(entry.get("endpoint_name") or "")
        for entry in pool
    }
    effective: set[str] = set(claim.get("waypoint_names") or [])
    for sequence_name in claim.get("names") or []:
        endpoint = (endpoint_by_name.get(sequence_name)
                    or derive_endpoint_name(sequence_name))
        if endpoint:
            effective.add(endpoint)
    return sorted(effective)


def migrate_sequence_claims(
    path: str | Path = DEFAULT_REGISTRY_PATH,
    root: Path = PROJECT_ROOT,
) -> bool:
    """一次性迁移到「能力条目级」认领。返回是否执行了迁移。

    两种旧状态都能迁：
    - 文件没有 sequence_claims 键（最早期）→ 把现有动作池视为
      LEGACY_SEQUENCE_COMBO 的存量；
    - 键里是组合级旧格式（带 arm/hand_id）→ 取各组合的已认领名单。
    然后统一按正则路由拆到该组合的各能力条目上；已是新格式（带
    capability_id）则不动。"""
    registry_path = Path(path).expanduser().resolve()
    if not registry_path.exists():
        return False
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    raw_claims = raw.get("sequence_claims")
    is_capability_format = isinstance(raw_claims, list) and all(
        isinstance(c, dict) and "capability_id" in c
        for c in raw_claims)
    if is_capability_format and all(
            "waypoint_names" in c for c in raw_claims):
        return False   # 已是最新格式（含空列表）

    if is_capability_format:
        # 只差位点字段：flick 条目预置流程必需公共位点，其余从空开始
        registry = load_registry(registry_path)
        method_by_id = {c["id"]: c["method"]
                        for c in registry["capabilities"]}
        for claim in registry["sequence_claims"]:
            raw_entry = next(
                (c for c in raw_claims
                 if c.get("capability_id") == claim["capability_id"]), {})
            if ("waypoint_names" not in raw_entry
                    and method_by_id.get(claim["capability_id"]) == "flick"):
                claim["waypoint_names"] = sorted(
                    set(claim["waypoint_names"])
                    | set(FLOW_REQUIRED_WAYPOINTS))
        save_registry(registry, registry_path)
        return True

    # 组合 → 存量动作名
    combo_names: dict[tuple[str, str], list[str]] = {}
    if raw_claims is None:
        combo_names[LEGACY_SEQUENCE_COMBO] = [
            entry["name"] for entry in sequence_pool(root)]
    elif isinstance(raw_claims, list):
        for old in raw_claims:
            if not isinstance(old, dict):
                continue
            combo = (str(old.get("arm") or ""),
                     str(old.get("hand_id") or ""))
            names = [str(n or "").strip() for n in old.get("names") or []]
            combo_names[combo] = [n for n in names if n]
    else:
        return False

    # 先按去掉认领的原始内容过校验，拿到规范化的能力条目再做路由
    base = dict(raw)
    base["sequence_claims"] = []
    registry = validate_registry(base)
    routed: dict[str, list[str]] = {}
    for (arm, hand_id), names in combo_names.items():
        for name in names:
            for cap_id in route_sequence_claim(registry, arm, hand_id, name):
                bucket = routed.setdefault(cap_id, [])
                if name not in bucket:
                    bucket.append(name)
    method_by_id = {c["id"]: c["method"] for c in registry["capabilities"]}
    registry["sequence_claims"] = [
        {"capability_id": cap_id, "names": names,
         # flick 条目预置流程必需公共位点（起手式起点 + 回落点）
         "waypoint_names": (list(FLOW_REQUIRED_WAYPOINTS)
                            if method_by_id.get(cap_id) == "flick" else [])}
        for cap_id, names in routed.items()
    ]
    save_registry(registry, registry_path)
    return True
