"""手臂资产的左右臂归属：JSON 字段 ``arm`` + 名字/文件名前缀 ``L-`` / ``R-``。

适用于所有按臂录制、会驱动手臂或手的落盘文件：位点（data/waypoints）、
动作序列（data/sequences）、横移录制（data/sidesteps）、灵巧手手位
（data/hand_poses）、TCP 工作点（data/tcp_points）。

安全约定（严格，没有兜底）：
· 每个文件必须带 ``"arm": "right_arm" | "left_arm"``（与 18000 注册表的
  ARMS 取值一致）；缺失或取值非法 = 无归属 → 不显示、不允许执行。
· ``name`` 以 ``R-`` / ``L-`` 开头（大写字母 + 半角横杠），文件名沿用
  ``<name>_<时间戳>.json`` 惯例，因此文件名同样以前缀开头，肉眼可辨。
  前缀只是给人看的标记；程序校验以 ``arm`` 字段为准，但两者矛盾时同样
  视为非法（防止改名后归属错乱）。
· 左臂绝不能使用右臂的文件，反之亦然：列表接口按激活臂过滤，执行 /
  回放 / 流程选档前再校验一次，不一致 → 拒绝。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

ARMS: tuple[str, ...] = ("right_arm", "left_arm")
ARM_PREFIX: dict[str, str] = {"right_arm": "R", "left_arm": "L"}
PREFIX_ARM: dict[str, str] = {prefix: arm for arm, prefix in ARM_PREFIX.items()}
ARM_LABELS: dict[str, str] = {"right_arm": "右臂", "left_arm": "左臂"}
# 名字开头的归属前缀：大写 L/R + 半角横杠
NAME_PREFIX_RE = re.compile(r"^([LR])-")
# 关节名前缀（URDF：right_shoulder_pitch_joint / left_...），用于交叉校验
JOINT_PREFIX_ARM: dict[str, str] = {"right_": "right_arm", "left_": "left_arm"}


class ArmMismatch(ValueError):
    """资产归属与当前臂不一致 / 无归属标记。"""


def normalize_arm(value: Any) -> str | None:
    """规范化臂取值；非法返回 None（调用方决定报错还是跳过）。"""
    arm = str(value or "").strip().lower()
    return arm if arm in ARMS else None


def arm_prefix(arm: str) -> str:
    """``right_arm`` → ``"R-"``。臂非法抛 ValueError。"""
    clean = normalize_arm(arm)
    if clean is None:
        raise ValueError(f"未知臂 {arm!r}（支持 {' / '.join(ARMS)}）")
    return f"{ARM_PREFIX[clean]}-"


def arm_from_name(name: str) -> str | None:
    """名字前缀推断归属：``R-xxx`` → right_arm；无前缀 → None。"""
    match = NAME_PREFIX_RE.match(str(name or ""))
    return PREFIX_ARM[match.group(1)] if match else None


def strip_arm_prefix(name: str) -> str:
    """去掉名字开头的 ``L-`` / ``R-``（没有则原样返回）。"""
    return NAME_PREFIX_RE.sub("", str(name or ""), count=1)


def match_pose_pattern(pattern: "re.Pattern[str] | str",
                       name: str) -> "re.Match[str] | None":
    """用起手式正则匹配动作名，兼容迁移前写的、不含 ``(?:[LR]-)?`` 的自定义正则。

    先按完整名字（含 ``R-``/``L-``）匹配，不中再按去掉前缀的名字匹配；
    两者都不中返回 None。捕获组语义不变（第 1 组 = 档位距离）。
    """
    regex = re.compile(pattern) if isinstance(pattern, str) else pattern
    text = str(name or "")
    match = regex.match(text)
    if match is not None:
        return match
    stripped = strip_arm_prefix(text)
    if stripped == text:
        return None
    return regex.match(stripped)


def prefixed_name(arm: str, name: str) -> str:
    """给名字加上该臂的前缀；已带同臂前缀则原样返回，带异臂前缀抛错。

    录制 / 保存入口统一走这里：用户输入「起手点测试」在右臂下落盘为
    「R-起手点测试」；用户手动输入了「R-起手点测试」也不会变成「R-R-…」。
    """
    prefix = arm_prefix(arm)
    text = str(name or "").strip()
    named_arm = arm_from_name(text)
    if named_arm is None:
        return f"{prefix}{text}"
    if named_arm != normalize_arm(arm):
        raise ArmMismatch(
            f"名字「{text}」的前缀属于{ARM_LABELS[named_arm]}，"
            f"当前是{ARM_LABELS[normalize_arm(arm)]}，不能这样命名")
    return text


def arm_asset_name(arm: str, base_name: str) -> str:
    """代码里固定引用的公共位点名按臂取名：(right_arm, 起手点测试) → R-起手点测试。"""
    return prefixed_name(arm, base_name)


def asset_arm(data: Any) -> str | None:
    """文件声明的归属（``arm`` 字段）；缺失 / 非法 / 与名字前缀矛盾 → None。"""
    if not isinstance(data, dict):
        return None
    arm = normalize_arm(data.get("arm"))
    if arm is None:
        return None
    named_arm = arm_from_name(str(data.get("name") or ""))
    if named_arm is not None and named_arm != arm:
        return None
    return arm


def joints_arm(joint_names: Iterable[str]) -> str | None:
    """按关节名前缀推断臂；混用 / 无法判断返回 None。"""
    found: set[str] = set()
    for joint in joint_names or []:
        text = str(joint)
        for prefix, arm in JOINT_PREFIX_ARM.items():
            if text.startswith(prefix):
                found.add(arm)
                break
        else:
            return None
    return found.pop() if len(found) == 1 else None


def belongs_to(data: Any, arm: str) -> bool:
    """文件是否归属该臂（严格：无标记 = 不属于任何臂）。"""
    target = normalize_arm(arm)
    return target is not None and asset_arm(data) == target


def check_asset_arm(data: Any, arm: str, label: str = "文件") -> str:
    """执行前校验：归属该臂则返回臂，否则抛 ArmMismatch（含可读原因）。

    附带关节名交叉校验：``named_joints`` / ``trajectory.joint_names`` /
    ``waypoints[*].named_joints`` 里出现异臂关节名同样拒绝——即便有人手改了
    ``arm`` 字段，右臂关节角也进不了左臂。
    """
    target = normalize_arm(arm)
    if target is None:
        raise ArmMismatch(f"当前臂 {arm!r} 非法，拒绝使用{label}")
    name = str((data or {}).get("name") or "") if isinstance(data, dict) else ""
    declared = asset_arm(data)
    if declared is None:
        raw = (data or {}).get("arm") if isinstance(data, dict) else None
        if raw is None:
            reason = "没有 arm 归属字段（旧文件请先运行 tools/migrate_arm_ownership.py 补标记）"
        elif normalize_arm(raw) is None:
            reason = f"arm 字段取值非法 {raw!r}"
        else:
            reason = "arm 字段与名字前缀矛盾"
        raise ArmMismatch(f"{label}「{name}」无有效归属：{reason}，已拒绝使用")
    if declared != target:
        raise ArmMismatch(
            f"{label}「{name}」属于{ARM_LABELS[declared]}，"
            f"当前是{ARM_LABELS[target]}，绝不能混用，已拒绝")
    for joints in _joint_name_sources(data):
        inferred = joints_arm(joints)
        if inferred is not None and inferred != target:
            raise ArmMismatch(
                f"{label}「{name}」的关节名是{ARM_LABELS[inferred]}的，"
                f"与声明的归属 {ARM_LABELS[target]} 矛盾，已拒绝")
    return declared


def _joint_name_sources(data: Any) -> list[list[str]]:
    sources: list[list[str]] = []
    if not isinstance(data, dict):
        return sources
    named = data.get("named_joints")
    if isinstance(named, dict):
        sources.append([str(k) for k in named])
    trajectory = data.get("trajectory")
    if isinstance(trajectory, dict) and isinstance(
            trajectory.get("joint_names"), list):
        sources.append([str(k) for k in trajectory["joint_names"]])
    waypoints = data.get("waypoints")
    if isinstance(waypoints, list):
        for item in waypoints:
            if isinstance(item, dict) and isinstance(
                    item.get("named_joints"), dict):
                sources.append([str(k) for k in item["named_joints"]])
    return sources


def filter_by_arm(items: Iterable[dict[str, Any]], arm: str) -> list[dict[str, Any]]:
    """列表接口用：只留归属该臂的条目（无标记的一律不显示）。"""
    return [item for item in items if belongs_to(item, arm)]


def stamp(data: dict[str, Any], arm: str) -> dict[str, Any]:
    """落盘前写入归属：``arm`` 字段 + 名字前缀（就地修改并返回）。"""
    clean = normalize_arm(arm)
    if clean is None:
        raise ValueError(f"未知臂 {arm!r}，拒绝落盘无归属文件")
    data["arm"] = clean
    if "name" in data:
        data["name"] = prefixed_name(clean, str(data["name"]))
    return data
