"""Read-only dexterous-hand model/state endpoints for the 18001 viewer."""
from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import FileResponse

from core.hand_runtime import hand_asset_path, hand_snapshot

from .state import router


@router.get("/hand")
def dexterous_hand_state() -> dict:
    return hand_snapshot()


@router.get("/hand/assets/{asset_path:path}")
def dexterous_hand_asset(asset_path: str) -> FileResponse:
    try:
        path = hand_asset_path(asset_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="灵巧手模型文件不存在") from exc
    return FileResponse(path)
