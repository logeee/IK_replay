#!/usr/bin/env python3
"""一次性迁移：给存量手臂资产补左右臂归属（arm 字段 + R-/L- 名字前缀）。

覆盖目录（都相对项目根）：
    data/waypoints    位点          data/sequences   起手式序列
    data/sidesteps    横移录制      data/hand_poses  灵巧手手位
    data/tcp_points   TCP 工作点（_default.json 只改引用不改名）

对每个文件：
1. 确定臂：已有合法 ``arm`` 字段 → 沿用（幂等）；否则按关节名前缀
   （right_*/left_*）、chain_id、recorded_combo.arm、side 推断；推断不出用
   ``--assume-arm``（缺省 right_arm——存量文件都是右臂录的）。多处线索互相
   矛盾 → 中止，不写任何东西。
2. 写 ``arm``、给 ``name`` 加前缀（没有 name 的横移文件按文件名补 name）、
   文件改名 ``<新name>_<时间戳>.json``。
3. 更新所有文件名引用：序列的 waypoints / trajectory.retarget.source_sequence，
   位点的 generated_from / copied_from，tcp_points/_default.json 的 key，
   gravity_calibration/waypoints/*.json 的 source_waypoint_file。
4. 注册表 config/capability_registry.json：认领名单按池里的改名映射更新
   （不在池里的按条目臂加前缀）；条目自配的旧内置正则换成带前缀的新版。

用法：
    python tools/migrate_arm_ownership.py --dry-run   # 只打印将要做什么
    python tools/migrate_arm_ownership.py             # 执行
所有服务（18000 / 18001 / 17001 / 18003）请先停掉再迁移，迁移后重启。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import arm_assets  # noqa: E402
from core import capability_registry as reg  # noqa: E402

STAMP_RE = re.compile(r"_(\d{8}_\d{6}(?:-\d+)?)\.json$")
ASSET_DIRS = ("waypoints", "sequences", "sidesteps", "hand_poses", "tcp_points")


class MigrationError(RuntimeError):
    pass


def _read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MigrationError(f"{path} 不是 JSON object")
    return data


def _write(path: Path, data: dict[str, Any], *, indent: int | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    path.write_text(text + ("\n" if indent else ""), encoding="utf-8")


def _detect_indent(path: Path) -> int | None:
    """保持原文件的缩进样式：横移是单行（None），序列 indent=1，其余 2。"""
    with path.open("r", encoding="utf-8") as stream:
        lines = stream.read(400).splitlines()
    if len(lines) < 2:
        return None
    second = lines[1]
    width = len(second) - len(second.lstrip(" "))
    return width or 2


def infer_arm(data: dict[str, Any], path: Path, assume: str) -> str:
    """推断归属；线索矛盾抛 MigrationError。"""
    declared = arm_assets.normalize_arm(data.get("arm"))
    hints: dict[str, str] = {}
    if declared:
        hints["arm"] = declared
    for joints in arm_assets._joint_name_sources(data):  # noqa: SLF001
        inferred = arm_assets.joints_arm(joints)
        if inferred:
            hints["joints"] = inferred
        elif joints:
            raise MigrationError(f"{path.name} 的关节名混用左右臂或无法识别：{joints}")
    chain = arm_assets.normalize_arm(data.get("chain_id"))
    if chain:
        hints["chain_id"] = chain
    combo = data.get("recorded_combo")
    if isinstance(combo, dict):
        combo_arm = arm_assets.normalize_arm(combo.get("arm"))
        if combo_arm:
            hints["recorded_combo"] = combo_arm
    side = str(data.get("side") or "").strip().lower()
    if side in ("left", "right"):
        hints["side"] = f"{side}_arm"
    named = arm_assets.arm_from_name(str(data.get("name") or ""))
    if named:
        hints["name_prefix"] = named
    values = set(hints.values())
    if len(values) > 1:
        raise MigrationError(f"{path.name} 的归属线索互相矛盾：{hints}")
    return values.pop() if values else assume


def new_file_name(old: str, old_name: str, new_name: str) -> str:
    """文件名跟着 name 改：<name>_<stamp>.json → <新name>_<stamp>.json。"""
    if old_name and old.startswith(f"{old_name}_"):
        return f"{new_name}{old[len(old_name):]}"
    match = STAMP_RE.search(old)
    if match:
        return f"{new_name}_{match.group(1)}.json"
    return f"{new_name}.json"


def plan_directory(directory: Path, assume: str) -> list[dict[str, Any]]:
    """扫描一个目录，产出每个文件的迁移计划（不落盘）。"""
    plans: list[dict[str, Any]] = []
    if not directory.is_dir():
        return plans
    for path in sorted(directory.glob("*.json")):
        if path.name == "_default.json":
            continue
        data = _read(path)
        arm = infer_arm(data, path, assume)
        old_name = str(data.get("name") or "").strip()
        base_name = old_name or path.stem   # 横移文件没有 name，用文件名
        new_name = arm_assets.prefixed_name(arm, base_name)
        new_file = new_file_name(path.name, old_name, new_name)
        changed = (arm_assets.normalize_arm(data.get("arm")) != arm
                   or old_name != new_name or path.name != new_file)
        plans.append({
            "path": path, "data": data, "arm": arm,
            "old_name": old_name, "new_name": new_name,
            "new_file": new_file, "changed": changed,
        })
    return plans


def _remap(value: Any, mapping: dict[str, str]) -> Any:
    return mapping.get(str(value), value) if isinstance(value, str) else value


def apply_references(plans_by_dir: dict[str, list[dict[str, Any]]],
                     file_map: dict[str, str]) -> None:
    """就地更新计划里各文件的文件名引用（尚未落盘）。"""
    for plan in plans_by_dir.get("sequences", []):
        data = plan["data"]
        if isinstance(data.get("waypoints"), list):
            data["waypoints"] = [_remap(v, file_map) for v in data["waypoints"]]
        retarget = (data.get("trajectory") or {}).get("retarget") \
            if isinstance(data.get("trajectory"), dict) else None
        if isinstance(retarget, dict) and "source_sequence" in retarget:
            retarget["source_sequence"] = _remap(retarget["source_sequence"], file_map)
    for plan in plans_by_dir.get("waypoints", []):
        data = plan["data"]
        for key in ("generated_from", "copied_from"):
            if key in data:
                data[key] = _remap(data[key], file_map)


def migrate_registry(registry_path: Path, name_map: dict[str, str],
                     dry_run: bool) -> list[str]:
    """注册表认领名单 / 正则跟随改名。返回变更说明。"""
    notes: list[str] = []
    if not registry_path.is_file():
        return notes
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    arm_by_cap = {c.get("id"): c.get("arm") for c in raw.get("capabilities") or []}
    for cap in raw.get("capabilities") or []:
        pattern = str((cap.get("assets") or {}).get("pose_pattern") or "")
        for direction, legacy in reg.LEGACY_POSE_PATTERNS.items():
            if pattern == legacy:
                cap["assets"]["pose_pattern"] = reg.BUILTIN_POSE_PATTERNS[direction]
                notes.append(f"能力 {cap.get('id')} 的起手式正则换成带臂前缀的新版")
    for claim in raw.get("sequence_claims") or []:
        cap_arm = arm_by_cap.get(claim.get("capability_id"))
        if not cap_arm:
            continue
        for key in ("names", "waypoint_names"):
            old_list = [str(n) for n in claim.get(key) or []]
            new_list: list[str] = []
            for name in old_list:
                mapped = name_map.get(name)
                if mapped is None:
                    mapped = arm_assets.prefixed_name(cap_arm, name)
                if arm_assets.arm_from_name(mapped) != cap_arm:
                    raise MigrationError(
                        f"认领「{name}」→「{mapped}」与条目 {claim.get('capability_id')} "
                        f"的臂 {cap_arm} 不符，请人工处理")
                if mapped not in new_list:
                    new_list.append(mapped)
            if new_list != old_list:
                claim[key] = sorted(new_list)
                notes.append(
                    f"条目 {claim.get('capability_id')} 的 {key} 更新 "
                    f"{len(old_list)} 项")
    if notes and not dry_run:
        reg.save_registry(raw, registry_path)   # 顺带做一遍校验
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--assume-arm", default="right_arm",
                        choices=list(arm_assets.ARMS),
                        help="推断不出归属的文件按此臂处理（默认 right_arm）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不改文件")
    args = parser.parse_args()
    root: Path = args.root.resolve()
    data_root = root / "data"

    plans_by_dir: dict[str, list[dict[str, Any]]] = {}
    try:
        for sub in ASSET_DIRS:
            plans_by_dir[sub] = plan_directory(data_root / sub, args.assume_arm)
    except MigrationError as exc:
        print(f"[迁移] ✘ 中止：{exc}")
        return 1

    # 文件名映射（跨目录唯一：各目录文件名互不重叠）与动作/位点名映射
    file_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    for sub, plans in plans_by_dir.items():
        targets = [p["new_file"] for p in plans]
        dupes = {t for t in targets if targets.count(t) > 1}
        if dupes:
            print(f"[迁移] ✘ 中止：{sub} 改名后文件名冲突 {sorted(dupes)}")
            return 1
        for plan in plans:
            if plan["path"].name != plan["new_file"]:
                file_map[plan["path"].name] = plan["new_file"]
            if plan["old_name"] and plan["old_name"] != plan["new_name"]:
                name_map[plan["old_name"]] = plan["new_name"]

    apply_references(plans_by_dir, file_map)

    total_changed = 0
    for sub, plans in plans_by_dir.items():
        for plan in plans:
            if not plan["changed"]:
                continue
            total_changed += 1
            print(f"[迁移] {sub}/{plan['path'].name}"
                  f" → {plan['new_file']}  arm={plan['arm']}")
    print(f"[迁移] 资产文件需改动 {total_changed} 个；文件名引用映射 {len(file_map)} 条")

    # tcp_points/_default.json：默认点引用文件名
    defaults_path = data_root / "tcp_points" / "_default.json"
    defaults_changed = False
    defaults: dict[str, Any] = {}
    if defaults_path.is_file():
        defaults = _read(defaults_path)
        for entry in defaults.values():
            if isinstance(entry, dict) and entry.get("kind") == "custom":
                mapped = file_map.get(str(entry.get("key")))
                if mapped:
                    entry["key"] = mapped
                    defaults_changed = True
    if defaults_changed:
        print("[迁移] tcp_points/_default.json 默认点引用更新")

    # gravity_calibration/waypoints：导入来源引用（只是溯源标记，顺手更新）
    gravity_updates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((data_root / "gravity_calibration" / "waypoints").glob("*.json")):
        try:
            payload = _read(path)
        except (MigrationError, ValueError, OSError):
            continue
        mapped = file_map.get(str(payload.get("source_waypoint_file") or ""))
        if mapped:
            payload["source_waypoint_file"] = mapped
            gravity_updates.append((path, payload))
    if gravity_updates:
        print(f"[迁移] gravity_calibration/waypoints 溯源引用更新 {len(gravity_updates)} 个")

    try:
        notes = migrate_registry(root / "config" / "capability_registry.json",
                                 name_map, dry_run=True)
    except MigrationError as exc:
        print(f"[迁移] ✘ 中止：{exc}")
        return 1
    for note in notes:
        print(f"[迁移] 注册表：{note}")

    if args.dry_run:
        print("[迁移] --dry-run：未改动任何文件")
        return 0

    # ---- 落盘：先写内容再改名（同目录内 os.replace，原子） ----
    for sub, plans in plans_by_dir.items():
        for plan in plans:
            if not plan["changed"]:
                continue
            data = plan["data"]
            data["arm"] = plan["arm"]
            data["name"] = plan["new_name"]
            path: Path = plan["path"]
            _write(path, data, indent=_detect_indent(path))
            target = path.with_name(plan["new_file"])
            if target != path:
                if target.exists():
                    print(f"[迁移] ✘ 目标已存在，跳过改名：{target.name}")
                    continue
                path.replace(target)
    if defaults_changed:
        _write(defaults_path, defaults, indent=2)
    for path, payload in gravity_updates:
        _write(path, payload, indent=_detect_indent(path))
    migrate_registry(root / "config" / "capability_registry.json", name_map,
                     dry_run=False)
    print("[迁移] ✔ 完成。请重启 18000 / 18001 / 17001 / 18003。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
