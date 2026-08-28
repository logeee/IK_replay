"""Keep execution diagnostics inside each portable pick-history record."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any


EXECUTIONS_FILENAME = "executions.jsonl"
_RECORD_NAME_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")
_append_lock = threading.Lock()


def _record_directories(history_dir: Path) -> list[Path]:
    if not history_dir.is_dir():
        return []
    return [
        path
        for path in history_dir.iterdir()
        if path.is_dir() and _RECORD_NAME_RE.fullmatch(path.name)
    ]


def _capture_record_map(history_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for record_dir in _record_directories(history_dir):
        try:
            meta = json.loads(
                (record_dir / "meta.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        capture_id = str(meta.get("capture_id") or "")
        if capture_id:
            result[capture_id] = record_dir.name
    return result


def resolve_record_name(
    history_dir: Path,
    execution: dict[str, Any],
    capture_records: dict[str, str] | None = None,
) -> str | None:
    """Resolve an execution to a record by explicit name, then capture ID."""
    context = execution.get("pick_context") or {}
    record = str(context.get("record") or "")
    if (
        _RECORD_NAME_RE.fullmatch(record)
        and (history_dir / record).is_dir()
    ):
        return record

    capture_id = str(context.get("capture_id") or "")
    if not capture_id:
        return None
    mapping = (
        capture_records
        if capture_records is not None
        else _capture_record_map(history_dir)
    )
    return mapping.get(capture_id)


def load_record_executions(record_dir: Path) -> list[dict[str, Any]]:
    """Load valid JSON objects embedded in one pick-history directory."""
    path = record_dir / EXECUTIONS_FILENAME
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
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
        return []
    return records


def load_embedded_executions(history_dir: Path) -> list[dict[str, Any]]:
    """Load execution diagnostics from every self-contained pick record."""
    records: list[dict[str, Any]] = []
    for record_dir in _record_directories(history_dir):
        records.extend(load_record_executions(record_dir))
    return records


def append_execution(
    history_dir: Path,
    execution: dict[str, Any],
) -> Path | None:
    """Append one execution to its matching pick record, without duplicates."""
    record = resolve_record_name(history_dir, execution)
    if record is None:
        return None
    path = history_dir / record / EXECUTIONS_FILENAME
    execution_id = str(execution.get("id") or "")
    encoded = json.dumps(execution, ensure_ascii=False)
    with _append_lock:
        if execution_id and any(
            str(item.get("id") or "") == execution_id
            for item in load_record_executions(path.parent)
        ):
            return path
        with path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    return path


def backfill_executions(
    history_dir: Path,
    executions: Iterable[dict[str, Any]],
) -> tuple[int, int]:
    """Embed old central-log records. Return (written, unmatched)."""
    capture_records = _capture_record_map(history_dir)
    known_ids = {
        record_dir.name: {
            str(item.get("id") or "")
            for item in load_record_executions(record_dir)
            if item.get("id")
        }
        for record_dir in _record_directories(history_dir)
    }
    written = 0
    unmatched = 0
    for execution in executions:
        record = resolve_record_name(history_dir, execution, capture_records)
        if record is None:
            unmatched += 1
            continue
        execution_id = str(execution.get("id") or "")
        if execution_id and execution_id in known_ids.setdefault(record, set()):
            continue
        path = history_dir / record / EXECUTIONS_FILENAME
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(execution, ensure_ascii=False) + "\n")
        if execution_id:
            known_ids[record].add(execution_id)
        written += 1
    return written, unmatched
