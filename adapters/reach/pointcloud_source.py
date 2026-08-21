"""Read-only, same-frame RGB-D source for the optional point-cloud viewer."""

from __future__ import annotations

import io
import json

import numpy as np
from fastapi.responses import JSONResponse, Response

from .state import router, state


@router.get("/rgbd_snapshot")
def reach_rgbd_snapshot():
    """Return one ZMQ message as a compressed NPZ payload.

    The JPEG and aligned depth are copied from the same subscriber update.
    This endpoint is intentionally read-only and is available in camera-only
    mode before hand-eye calibration exists.
    """
    snapshot_reader = getattr(state.camera, "rgbd_snapshot", None)
    if snapshot_reader is None:
        return JSONResponse(
            {"ok": False, "error": "当前相机源不支持同帧 RGB-D 快照"},
            status_code=409,
        )
    snapshot = snapshot_reader()
    if snapshot is None:
        return JSONResponse(
            {"ok": False, "error": "还没有新鲜的 RGB-D 帧"},
            status_code=503,
        )

    metadata = dict(snapshot.get("metadata") or {})
    metadata["handeye_ready"] = bool(state.handeye_ready)
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
