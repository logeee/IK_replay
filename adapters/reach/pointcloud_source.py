"""Read-only, same-frame RGB-D source for the optional point-cloud viewer."""

from __future__ import annotations

import io
import json
import re

import numpy as np
from fastapi.responses import JSONResponse, Response

from .state import router, state


@router.get("/rgbd_snapshot")
def reach_rgbd_snapshot(
    include_robot_pose: bool = False,
    pick_capture_id: str | None = None,
):
    """Return one ZMQ message as a compressed NPZ payload.

    The JPEG and aligned depth are copied from the same subscriber update.
    It is available in camera-only mode before hand-eye calibration exists.
    When ``pick_capture_id`` is supplied and PINK is available, the same
    snapshot operation also stores the current ``world_T_root`` under that
    7005 capture id, before any point-cloud inference starts.
    """
    if pick_capture_id is not None:
        pick_capture_id = str(pick_capture_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", pick_capture_id):
            return JSONResponse(
                {"ok": False, "error": "pick_capture_id 格式非法"},
                status_code=400,
            )
    snapshot_reader = getattr(state.camera, "rgbd_snapshot", None)
    if snapshot_reader is None:
        return JSONResponse(
            {"ok": False, "error": "当前相机源不支持同帧 RGB-D 快照"},
            status_code=409,
        )
    try:
        if include_robot_pose:
            from .waypoint_rgbd import pose_sample, paired_pose
            before = pose_sample()
            snapshot = snapshot_reader(fresh=True)
            pose = paired_pose(before, pose_sample()) if snapshot is not None else None
        else:
            snapshot = snapshot_reader()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"RGBD 位姿采集失败: {exc}"}, status_code=409)
    if snapshot is None:
        return JSONResponse(
            {"ok": False, "error": "还没有新鲜的 RGB-D 帧"},
            status_code=503,
        )

    metadata = dict(snapshot.get("metadata") or {})
    metadata["handeye_ready"] = bool(state.handeye_ready)
    if include_robot_pose:
        metadata["robot_pose"] = pose
    if pick_capture_id is not None:
        binding = {
            "capture_id": pick_capture_id,
            "source_frame_id": str(metadata.get("frame_id") or ""),
            "bound": False,
        }
        runtime = state.pink_runtime
        if runtime is None:
            binding["error"] = "PINK 运行时不可用"
        else:
            try:
                world_root = runtime.capture_pick_frame(
                    capture_id=pick_capture_id,
                    source_frame_id=binding["source_frame_id"],
                )
                if world_root is None:
                    binding["error"] = "PINK 世界系尚未锚定"
                else:
                    binding.update({
                        "bound": True,
                        "anchor_count": runtime.pick_world_frame_anchor,
                    })
            except Exception as exc:
                binding["error"] = str(exc)
        metadata["pink_pick_world_frame"] = binding
    payload = io.BytesIO()
    np.savez_compressed(
        payload,
        jpeg=np.frombuffer(snapshot["jpeg"], dtype=np.uint8),
        depth_mm=np.asarray(snapshot["depth_mm"], dtype=np.float32),
        intrinsics=np.asarray(snapshot["intrinsics"], dtype=np.float64),
        distortion=np.asarray(snapshot.get("distortion", []), dtype=np.float64),
        metadata_json=np.frombuffer(
            json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
            dtype=np.uint8,
        ),
        T_cam2root=(
            np.empty((0, 0), dtype=np.float64)
            if state.T_cam2root is None
            else np.asarray(state.T_cam2root, dtype=np.float64)
        ),
    )
    frame_id = metadata.get("frame_id", "")
    return Response(
        content=payload.getvalue(),
        media_type="application/x-npz",
        headers={
            "Cache-Control": "no-store",
            "X-RGBD-Frame-Id": str(frame_id),
        },
    )
