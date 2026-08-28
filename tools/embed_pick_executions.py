#!/usr/bin/env python3
"""Backfill central reach diagnostics into portable pick-history records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pick_execution_archive import backfill_executions


def load_reach_logs(log_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not log_dir.is_dir():
        return records
    for path in sorted(log_dir.glob("reach_*.jsonl")):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError:
            continue
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把18001执行诊断写入对应 pick_history 记录目录"
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=ROOT / "data" / "pick_history",
    )
    parser.add_argument(
        "--reach-log-dir",
        type=Path,
        default=ROOT / "logs" / "reach",
    )
    args = parser.parse_args()

    executions = load_reach_logs(args.reach_log_dir.resolve())
    written, unmatched = backfill_executions(
        args.history_dir.resolve(),
        executions,
    )
    print(
        f"[历史记录] 执行诊断回填完成：新增 {written} 条，"
        f"无法关联 {unmatched} 条"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
