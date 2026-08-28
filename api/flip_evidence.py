"""Shared persistence for automatic and 18001 manual flip evidence."""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PICK_HISTORY_DIR = ROOT / "data" / "pick_history"
_RECORD_NAME_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")


def valid_record_name(record: str) -> bool:
    return bool(_RECORD_NAME_RE.fullmatch(str(record)))


def save_flip_evidence(
    record: str,
    stage: str,
    result: dict[str, Any],
    *,
    flip_from: str,
    flip_to: str,
    success: bool | None = None,
    round_no: int | None = None,
    history_dir: Path = PICK_HISTORY_DIR,
) -> dict[str, Any]:
    """Write one evidence stage into an existing 7005 pick record."""
    if stage not in {"before", "after"}:
        raise ValueError(f"未知拨动证据阶段: {stage!r}")
    if not valid_record_name(record):
        raise ValueError(f"非法选点记录名: {record!r}")
    record_dir = history_dir / record
    if not record_dir.is_dir():
        raise FileNotFoundError(f"选点记录不存在: {record}")

    jpeg_b64 = result.get("jpeg_b64")
    if jpeg_b64:
        (record_dir / f"flip_{stage}.jpg").write_bytes(
            base64.b64decode(jpeg_b64)
        )
    wrist_jpeg_b64 = (
        result.get("wrist_jpeg_b64") if stage == "before" else None
    )
    if wrist_jpeg_b64:
        (record_dir / "flip_before_wrist.jpg").write_bytes(
            base64.b64decode(wrist_jpeg_b64)
        )

    result_path = record_dir / "flip_result.json"
    data: dict[str, Any] = {}
    if result_path.is_file():
        loaded = json.loads(result_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scene": result.get("scene"),
        "conf": result.get("conf"),
        "boxes": result.get("boxes"),
        "has_image": bool(jpeg_b64),
    }
    if stage == "before":
        entry["has_wrist_image"] = bool(wrist_jpeg_b64)
        if result.get("wrist_error"):
            entry["wrist_error"] = str(result["wrist_error"])
    if success is not None:
        entry["success"] = bool(success)
    data[stage] = entry
    data.update(
        {
            "flip_from": flip_from,
            "flip_to": flip_to,
            "round": round_no,
        }
    )
    result_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "head_saved": bool(jpeg_b64),
        "wrist_saved": bool(wrist_jpeg_b64),
        "path": str(result_path),
    }
