#!/usr/bin/env python3
"""Back up and lower every recorded left trajectory endpoint safely."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.gravity_calibration import (  # noqa: E402
    REGULAR_WAYPOINTS_DIR,
    SEQUENCES_DIR,
    _retarget_sequence,
)


LEFT_SEQUENCE_PATTERN = re.compile(
    r"^(?P<distance>\d+\.\d+)-左-起手式_(?P<timestamp>\d{8}_\d{6})\.json$"
)


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} 不是 JSON 对象")
    return value


def _discover_pairs() -> list[tuple[Path, Path, dict, dict, str]]:
    pairs: list[tuple[Path, Path, dict, dict, str]] = []
    for sequence_path in sorted(SEQUENCES_DIR.glob("*-左-起手式_*.json")):
        match = LEFT_SEQUENCE_PATTERN.fullmatch(sequence_path.name)
        if match is None:
            continue
        sequence = _read_json(sequence_path)
        waypoints = sequence.get("waypoints")
        if not isinstance(waypoints, list) or len(waypoints) < 2:
            raise RuntimeError(f"{sequence_path.name} 缺少终点路点引用")
        endpoint_path = REGULAR_WAYPOINTS_DIR / str(waypoints[-1])
        if not endpoint_path.is_file():
            raise RuntimeError(
                f"{sequence_path.name} 引用的终点不存在：{endpoint_path.name}"
            )
        endpoint = _read_json(endpoint_path)
        pairs.append(
            (sequence_path, endpoint_path, sequence, endpoint, match["timestamp"])
        )
    if not pairs:
        raise RuntimeError("没有找到“X.XX-左-起手式”轨迹")
    return pairs


def _backup(
    pairs: list[tuple[Path, Path, dict, dict, str]],
    down_m: float,
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ROOT / "data" / "backups" / f"left_tcp_before_down_{stamp}"
    sequence_backup = backup_dir / "sequences"
    waypoint_backup = backup_dir / "waypoints"
    sequence_backup.mkdir(parents=True)
    waypoint_backup.mkdir(parents=True)
    for sequence_path, endpoint_path, *_ in pairs:
        shutil.copy2(sequence_path, sequence_backup / sequence_path.name)
        shutil.copy2(endpoint_path, waypoint_backup / endpoint_path.name)
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "operation": "smooth_endpoint_tcp_down",
        "down_m": down_m,
        "axis": "root_-Z",
        "sequence_files": [pair[0].name for pair in pairs],
        "waypoint_files": [pair[1].name for pair in pairs],
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return backup_dir


def _restore(
    pairs: list[tuple[Path, Path, dict, dict, str]],
    backup_dir: Path,
) -> None:
    for sequence_path, endpoint_path, *_ in pairs:
        shutil.copy2(backup_dir / "sequences" / sequence_path.name, sequence_path)
        shutil.copy2(backup_dir / "waypoints" / endpoint_path.name, endpoint_path)


def lower_all(down_m: float) -> dict:
    if not 0.0 < down_m <= 0.20:
        raise RuntimeError("下降量必须在 0～0.20m 之间")
    pairs = _discover_pairs()
    backup_dir = _backup(pairs, down_m)
    results = []
    try:
        for index, (
            sequence_path,
            _endpoint_path,
            sequence,
            endpoint,
            timestamp,
        ) in enumerate(pairs, start=1):
            print(f"[{index}/{len(pairs)}] 重新生成 {sequence['name']} ...", flush=True)
            result = _retarget_sequence(
                sequence_path.name,
                str(sequence["name"]),
                0.0,
                offset_root_m=[0.0, 0.0, -down_m],
                endpoint_name=str(endpoint["name"]),
                output_timestamp=timestamp,
            )
            results.append(result)
    except BaseException:
        print("生成失败，正在恢复全部原文件 ...", flush=True)
        _restore(pairs, backup_dir)
        raise
    return {
        "count": len(results),
        "down_m": down_m,
        "backup_dir": str(backup_dir),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="备份全部左侧轨迹，并让终点TCP沿根坐标系-Z平滑下降"
    )
    parser.add_argument("--down-m", type=float, default=0.10)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="确认写入；未提供时只列出待处理文件",
    )
    args = parser.parse_args()
    pairs = _discover_pairs()
    print(f"找到 {len(pairs)} 条左侧轨迹：")
    for sequence_path, endpoint_path, *_ in pairs:
        print(f"  {sequence_path.name}  →  {endpoint_path.name}")
    if not args.apply:
        print("未写入。确认后请加 --apply。")
        return 0
    result = lower_all(args.down_m)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
