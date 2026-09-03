"""Dexterous-hand endpoints for the 18001 viewer.

模型/状态只读展示 + 手位执行：起手点测试可选姿态库（data/hand_poses，
18003 配置页维护）的手位下发；手走 18089 HTTP，与手臂 DDS 互不阻塞，
所以手臂运动过程中也可以随时调手。
"""
from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse

from core import hand_poses
from core.hand_runtime import hand_asset_path, hand_command, hand_snapshot

from .state import router


@router.get("/hand")
def dexterous_hand_state() -> dict:
    return hand_snapshot()


@router.get("/hand/poses")
def dexterous_hand_poses() -> dict:
    """姿态库列表（18003 保存的命名手位）。"""
    return {"ok": True, "poses": hand_poses.list_poses()}


@router.post("/hand/pose")
def dexterous_hand_pose(body: dict):
    """执行一个手位：{file} 从姿态库取，或直接给 {positions}。

    手臂运动期间也可调用（HTTP 通道独立）；被其他控制源占用时 18089
    返回 409，这里透传错误。
    """
    filename = str(body.get("file") or "")
    name = None
    if filename:
        try:
            pose = hand_poses.load_pose(filename)
        except FileNotFoundError:
            return JSONResponse(
                {"ok": False, "error": f"姿态文件不存在: {filename}"},
                status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=422)
        positions = pose["positions"]
        name = pose.get("name")
    else:
        try:
            positions = hand_poses.validate_positions(body.get("positions"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)},
                                status_code=422)
    duration_ms = max(50, min(int(body.get("duration_ms") or 500), 5000))
    result = hand_command(positions, duration_ms)
    if not result.get("ok"):
        status = 409 if result.get("enabled") else 400
        return JSONResponse(
            {"ok": False, "error": result.get("error"), "name": name},
            status_code=status)
    return {"ok": True, "name": name, "positions": positions,
            "hand": {key: result.get(key)
                     for key in ("device_id", "side", "hand_name")}}


@router.get("/hand/assets/{asset_path:path}")
def dexterous_hand_asset(asset_path: str) -> FileResponse:
    try:
        path = hand_asset_path(asset_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="灵巧手模型文件不存在") from exc
    return FileResponse(path)
