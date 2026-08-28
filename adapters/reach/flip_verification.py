"""Flip evidence hooks used by the manual 18001 execution path."""

from __future__ import annotations

import time
from typing import Any

from api.flip_evidence import save_flip_evidence, valid_record_name
from api.yolo_client import YoloClient

from .state import state


SCENES = ("就地", "远方")
YOLO_ATTEMPTS = 3
YOLO_RETRY_WAIT_S = 0.6
VERIFY_SETTLE_S = 1.5


def _opposite(scene: str | None) -> str | None:
    if scene == "就地":
        return "远方"
    if scene == "远方":
        return "就地"
    return None


def _scene_with_retries(
    yolo: YoloClient,
    *,
    include_wrist: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    last: dict[str, Any] | None = None
    for attempt in range(YOLO_ATTEMPTS):
        last = yolo.scene(
            include_image=True,
            include_wrist=include_wrist,
        )
        if last.get("ok") and last.get("scene") in SCENES:
            return last, last
        if attempt + 1 < YOLO_ATTEMPTS:
            time.sleep(YOLO_RETRY_WAIT_S)
    return None, last


def capture_manual_before(spec: dict[str, Any]) -> dict[str, Any]:
    """Capture head + right wrist immediately before a manual sidestep."""
    record = str(spec.get("record") or "")
    if not valid_record_name(record):
        return {"ok": False, "error": "缺少有效的 7005 选点记录名"}
    hint = str(spec.get("flip_from") or "")
    if hint not in SCENES:
        hint = ""
    yolo = YoloClient(state.yolo_base)
    recognized, last = _scene_with_retries(yolo, include_wrist=True)
    result = recognized or last
    if not result or not result.get("ok"):
        flip_to = _opposite(hint)
        return {
            "ok": False,
            "record": record,
            "flip_from": hint or None,
            "flip_to": flip_to,
            "error": (result or {}).get("error") or "拨动前相机抓帧失败",
        }
    flip_from = result.get("scene") if result.get("scene") in SCENES else hint
    flip_to = _opposite(flip_from)
    if not flip_from or not flip_to:
        return {
            "ok": False,
            "record": record,
            "error": "拨动前 YOLO 未识别到「就地/远方」，无法确定目标状态",
        }
    try:
        saved = save_flip_evidence(
            record,
            "before",
            result,
            flip_from=flip_from,
            flip_to=flip_to,
        )
    except Exception as exc:
        return {"ok": False, "record": record, "error": str(exc)}
    return {
        "ok": True,
        "record": record,
        "flip_from": flip_from,
        "flip_to": flip_to,
        **saved,
    }


def verify_manual_after(context: dict[str, Any]) -> dict[str, Any]:
    """Use the same head-camera YOLO decision rule as SwitchFlow."""
    record = str(context.get("record") or "")
    flip_from = str(context.get("flip_from") or "")
    flip_to = str(context.get("flip_to") or "")
    if (
        not valid_record_name(record)
        or flip_from not in SCENES
        or flip_to != _opposite(flip_from)
    ):
        return {"ok": False, "error": "拨动复核上下文无效"}

    yolo = YoloClient(state.yolo_base)
    got, last = _scene_with_retries(yolo)
    if got is None:
        return {
            "ok": False,
            "record": record,
            "error": (last or {}).get("error") or "拨动后 YOLO 无结论",
        }
    if got["scene"] == flip_to:
        final = got
        success = True
    else:
        time.sleep(VERIFY_SETTLE_S)
        again, _last_again = _scene_with_retries(yolo)
        final = again or got
        success = bool(again and again["scene"] == flip_to)
    try:
        saved = save_flip_evidence(
            record,
            "after",
            final,
            flip_from=flip_from,
            flip_to=flip_to,
            success=success,
        )
    except Exception as exc:
        return {"ok": False, "record": record, "error": str(exc)}
    return {
        "ok": True,
        "record": record,
        "scene": final.get("scene"),
        "success": success,
        **saved,
    }
