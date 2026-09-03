"""TCP 工作点库：手坐标系取的命名点（data/tcp_points/*.json）。

18003 手配置页在三维手模型上按 T 取点、命名保存；18001 按激活组合的
手型号过滤选择，选中后热替换规划用的 state.p_tool（腕系）。

坐标系约定：
- 自定义点存 **手 URDF 根坐标系**（xyz_hand）。点刚性绑在手掌上，
  重新手眼标定后仍有效；用的时候经 T_wrist2hand 转到腕系。
- 标定指尖点（handeye3d_result.json 的 tcp_points_wrist_m）本来就是
  腕系坐标，这里只做只读透传，选择池里与自定义点并列。
- 每个手型号可记一个默认点（_default.json），18001 启动时自动应用。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POINTS_DIR = ROOT / "data" / "tcp_points"
DEFAULT_FILE = "_default.json"


def _points_dir(directory: Path | None = None) -> Path:
    return Path(directory) if directory is not None else POINTS_DIR


def validate_xyz(value: Any, field: str = "xyz_hand") -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{field} 必须是 3 维坐标")
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 含非数值") from exc
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{field} 含非有限数值")
    if any(abs(item) > 1.0 for item in result):
        raise ValueError(f"{field} 超出 ±1m，明显不在手上")
    return result


def safe_point_path(filename: str,
                    directory: Path | None = None) -> Path | None:
    """防路径穿越：只允许目录内的 *.json 纯文件名（保留 _default.json）。"""
    if (not filename.endswith(".json") or "/" in filename
            or "\\" in filename or ".." in filename
            or filename == DEFAULT_FILE):
        return None
    return _points_dir(directory) / filename


def list_points(hand_id: str | None = None,
                directory: Path | None = None) -> list[dict[str, Any]]:
    """自定义 TCP 点；hand_id 给定时只回该手型号的（18001 过滤用）。"""
    base = _points_dir(directory)
    if not base.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        if path.name == DEFAULT_FILE:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        if hand_id is not None and str(data.get("hand_id") or "") != hand_id:
            continue
        data["file"] = path.name
        items.append(data)
    return items


def load_point(filename: str,
               directory: Path | None = None) -> dict[str, Any]:
    path = safe_point_path(filename, directory)
    if path is None or not path.is_file():
        raise FileNotFoundError(filename)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{filename} 不是 JSON object")
    data["xyz_hand"] = validate_xyz(data.get("xyz_hand"))
    data["file"] = path.name
    return data


def save_point(
    name: str,
    xyz_hand: Any,
    *,
    hand_id: str,
    combo: dict[str, Any] | None = None,
    directory: Path | None = None,
) -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("TCP 点名不能为空")
    if any(ch in name for ch in "/\\"):
        raise ValueError("TCP 点名不能含路径分隔符")
    if not str(hand_id or "").strip():
        raise ValueError("TCP 点必须归属一个手型号")
    item: dict[str, Any] = {
        "name": name,
        "hand_id": str(hand_id),
        "xyz_hand": validate_xyz(xyz_hand),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if combo:
        item["recorded_combo"] = {
            "arm": combo.get("arm"),
            "hand_id": combo.get("hand_id"),
        }
    base = _points_dir(directory)
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


def update_point(
    filename: str,
    *,
    name: str | None = None,
    xyz_hand: Any | None = None,
    directory: Path | None = None,
) -> dict[str, Any]:
    """编辑既有点（改名/挪位置），保留 created_at、文件名不变。"""
    path = safe_point_path(filename, directory)
    if path is None or not path.is_file():
        raise FileNotFoundError(filename)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{filename} 不是 JSON object")
    if name is not None:
        name = str(name).strip()
        if not name:
            raise ValueError("TCP 点名不能为空")
        data["name"] = name
    if xyz_hand is not None:
        data["xyz_hand"] = validate_xyz(xyz_hand)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    data["file"] = path.name
    return data


def delete_point(filename: str, directory: Path | None = None) -> bool:
    path = safe_point_path(filename, directory)
    if path is None or not path.is_file():
        return False
    path.unlink()
    # 若默认点指向它，顺带清掉
    defaults = _load_defaults(directory)
    changed = False
    for hand_id in list(defaults):
        entry = defaults[hand_id]
        if entry.get("kind") == "custom" and entry.get("key") == filename:
            del defaults[hand_id]
            changed = True
    if changed:
        _write_defaults(defaults, directory)
    return True


# ---- 默认点（每个手型号一个，18001 启动时自动应用） ----

def _defaults_path(directory: Path | None = None) -> Path:
    return _points_dir(directory) / DEFAULT_FILE


def _load_defaults(directory: Path | None = None) -> dict[str, dict[str, str]]:
    path = _defaults_path(directory)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_defaults(defaults: dict[str, dict[str, str]],
                    directory: Path | None = None) -> None:
    base = _points_dir(directory)
    base.mkdir(parents=True, exist_ok=True)
    _defaults_path(directory).write_text(
        json.dumps(defaults, ensure_ascii=False, indent=2), encoding="utf-8")


def get_default(hand_id: str,
                directory: Path | None = None) -> dict[str, str] | None:
    """返回 {"kind": "custom"|"calib", "key": 文件名或标定点 id} 或 None。"""
    entry = _load_defaults(directory).get(str(hand_id))
    if (isinstance(entry, dict) and entry.get("kind") in ("custom", "calib")
            and str(entry.get("key") or "")):
        return {"kind": str(entry["kind"]), "key": str(entry["key"])}
    return None


def set_default(hand_id: str, kind: str, key: str,
                directory: Path | None = None) -> None:
    if kind not in ("custom", "calib"):
        raise ValueError(f"默认点 kind 只能是 custom/calib，收到 {kind!r}")
    if not str(key or "").strip():
        raise ValueError("默认点 key 不能为空")
    defaults = _load_defaults(directory)
    defaults[str(hand_id)] = {"kind": kind, "key": str(key)}
    _write_defaults(defaults, directory)


def clear_default(hand_id: str, directory: Path | None = None) -> None:
    defaults = _load_defaults(directory)
    if str(hand_id) in defaults:
        del defaults[str(hand_id)]
        _write_defaults(defaults, directory)


# ---- 标定指尖点（handeye3d_result.json，只读） ----

def calib_tcp_points(calibration: dict[str, Any]) -> list[dict[str, Any]]:
    """标定文件里的指尖特征点（腕系），透传 id/label/p_wrist_m。"""
    items: list[dict[str, Any]] = []
    for point in calibration.get("tcp_points_wrist_m") or []:
        if not isinstance(point, dict):
            continue
        xyz = point.get("p_wrist_m")
        if not (isinstance(xyz, list) and len(xyz) == 3):
            continue
        try:
            xyz = [float(value) for value in xyz]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in xyz):
            continue
        items.append({
            "id": str(point.get("id") or ""),
            "label": str(point.get("label") or point.get("id") or ""),
            "link": str(point.get("link") or ""),
            "p_wrist_m": xyz,
        })
    return [item for item in items if item["id"]]


def hand_to_wrist(T_wrist2hand: list[list[float]],
                  xyz_hand: list[float]) -> list[float]:
    """手根坐标系点 → 腕系（p_wrist = T_wrist2hand · [p_hand, 1]）。"""
    x, y, z = (float(v) for v in xyz_hand)
    return [
        float(T_wrist2hand[row][0] * x + T_wrist2hand[row][1] * y
              + T_wrist2hand[row][2] * z + T_wrist2hand[row][3])
        for row in range(3)
    ]


def wrist_to_hand(T_wrist2hand: list[list[float]],
                  xyz_wrist: list[float]) -> list[float]:
    """腕系点 → 手根坐标系（旋转正交，用转置求逆）。"""
    dx = float(xyz_wrist[0]) - float(T_wrist2hand[0][3])
    dy = float(xyz_wrist[1]) - float(T_wrist2hand[1][3])
    dz = float(xyz_wrist[2]) - float(T_wrist2hand[2][3])
    return [
        float(T_wrist2hand[0][col] * dx + T_wrist2hand[1][col] * dy
              + T_wrist2hand[2][col] * dz)
        for col in range(3)
    ]
