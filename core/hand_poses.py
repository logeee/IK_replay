"""灵巧手姿态库：命名保存 / 加载 / 删除（data/hand_poses/*.json）。

18003 手配置页在此保存姿态，18001 起手点测试选择手位时从此读取。
positions 是 18089 hand_web 的归一化关节位置（6 个 0~1 浮点，0=张开）。
文件名 <名字>_<时间戳>.json，与路点 / 动作序列的落盘惯例一致。

左右臂归属（core/arm_assets.py）：每个手位带 ``arm`` 字段、名字以 R-/L-
开头；列表 / 加载按臂过滤与校验，左臂的手拿不到右臂录的手位。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from core import arm_assets

ROOT = Path(__file__).resolve().parents[1]
POSES_DIR = ROOT / "data" / "hand_poses"
POSITION_COUNT = 6


def _poses_dir(directory: Path | None = None) -> Path:
    return Path(directory) if directory is not None else POSES_DIR


def validate_positions(value: Any) -> list[float]:
    """18089 归一化关节位置：恰好 6 个 [0,1] 浮点。"""
    if not isinstance(value, (list, tuple)) or len(value) != POSITION_COUNT:
        raise ValueError(f"positions 必须是 {POSITION_COUNT} 个数的数组")
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ValueError("positions 含非数值") from exc
    if not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in result):
        raise ValueError("positions 每项必须在 0~1 之间（0=张开）")
    return result


def safe_pose_path(filename: str,
                   directory: Path | None = None) -> Path | None:
    """防路径穿越：只允许目录内的 *.json 纯文件名。"""
    if (not filename.endswith(".json") or "/" in filename
            or "\\" in filename or ".." in filename):
        return None
    return _poses_dir(directory) / filename


def list_poses(directory: Path | None = None, *,
               arm: str | None = None) -> list[dict[str, Any]]:
    """姿态列表；arm 给定时只回该臂的（无归属标记 / 异臂的不显示）。"""
    base = _poses_dir(directory)
    if not base.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        if arm is not None and not arm_assets.belongs_to(data, arm):
            continue
        data["file"] = path.name
        items.append(data)
    return items


def load_pose(filename: str,
              directory: Path | None = None, *,
              arm: str | None = None) -> dict[str, Any]:
    """加载手位；arm 给定时校验归属，不一致抛 arm_assets.ArmMismatch。"""
    path = safe_pose_path(filename, directory)
    if path is None or not path.is_file():
        raise FileNotFoundError(filename)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{filename} 不是 JSON object")
    if arm is not None:
        arm_assets.check_asset_arm(data, arm, "手位")
    data["positions"] = validate_positions(data.get("positions"))
    data["file"] = path.name
    return data


def save_pose(
    name: str,
    positions: Any,
    *,
    device_id: str,
    side: str,
    arm: str,
    combo: dict[str, Any] | None = None,
    directory: Path | None = None,
) -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("姿态名不能为空")
    if any(ch in name for ch in "/\\"):
        raise ValueError("姿态名不能含路径分隔符")
    item: dict[str, Any] = {
        "name": name,
        "device_id": str(device_id),
        "side": str(side),
        "positions": validate_positions(positions),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 臂归属：arm 字段 + 名字前缀（文件名随名字，自然带前缀）
    arm_assets.stamp(item, arm)
    name = item["name"]
    if combo:
        item["recorded_combo"] = {
            "arm": combo.get("arm"),
            "hand_id": combo.get("hand_id"),
        }
    base = _poses_dir(directory)
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = base / f"{name}_{stamp}.json"
    counter = 1
    while path.exists():
        counter += 1
        path = base / f"{name}_{stamp}-{counter}.json"
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    item["file"] = path.name
    return item


def delete_pose(filename: str, directory: Path | None = None, *,
                arm: str | None = None) -> bool:
    """删除手位；arm 给定时只允许删该臂的（异臂抛 ArmMismatch）。"""
    path = safe_pose_path(filename, directory)
    if path is None or not path.is_file():
        return False
    if arm is not None:
        data = json.loads(path.read_text(encoding="utf-8"))
        arm_assets.check_asset_arm(data, arm, "手位")
    path.unlink()
    return True
