"""Read-only waypoint RGB-D acquisition and its per-waypoint archive.

Pose samples bracket a fresh camera frame. This is a stationary software
snapshot, not hardware timestamp synchronization; preserve both samples.
"""
from __future__ import annotations

import io
import json
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from .state import _read_joints, _read_torso, _torso_rotation, state


def pose_sample():
    if state.exec_running or state.align_running:
        raise ValueError("请在手臂和机器人停止运动后录制 RGBD 点位")
    started = time.time_ns()
    q = np.asarray(_read_joints(), dtype=float)
    if q.shape != (len(state.joint_names),) or not np.isfinite(q).all():
        raise ValueError("真机关节数据无效")
    named = dict(zip(state.joint_names, q.tolist()))
    torso = _read_torso()
    full_joints = dict(named)
    if torso:
        full_joints.update({name if name.endswith("_joint") else name + "_joint": float(v)
                            for name, v in zip(torso.get("waist_names", []), torso.get("waist_rad", []))})
    transforms = state.robot_model.forward_kinematics(full_joints)
    wrist_link = state.wrist_link or state.robot_model.end_link(state.chain_id)
    root_base, root_wrist = transforms[state.base_link], transforms[wrist_link]
    base_wrist = np.linalg.inv(root_base) @ root_wrist
    wrist_tcp = np.eye(4)
    wrist_tcp[:3, 3] = state.p_tool
    base_tcp = base_wrist @ wrist_tcp
    pink_world = None
    rt = state.pink_runtime
    if rt is not None and rt.world_frame.anchored:
        try:
            _, fb = rt.update_world()
            if rt.sampler.age_ms() <= 200:
                pink_world = {"world_T_root": fb.world_T_root.tolist(),
                              "anchor_count": rt.world_frame.anchor_count}
        except Exception:
            pass  # Legacy RGBD recording does not require the optional PINK runtime.
    return {
        "sample_started_unix_ns": started, "sample_finished_unix_ns": time.time_ns(),
        "recorded_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "robot": state.robot_id, "arm": state.chain_id,
        "named_joints": named, "torso": torso,
        "base_link": state.base_link, "wrist_link": wrist_link,
        "T_root_from_base": root_base.tolist(),
        "T_base_from_wrist": base_wrist.tolist(), "T_base_from_tcp": base_tcp.tolist(),
        "tcp": {"p_tool_wrist_m": list(state.p_tool), "selection": deepcopy(state.tcp_selection),
                "orientation_definition": "wrist_link_axes"},
        "calibration": {"ready": bool(state.handeye_ready), "metadata": deepcopy(state.calib_meta),
                        "T_base_from_camera": None if state.T_cam2torso is None else state.T_cam2torso.tolist(),
                        "T_wrist_from_hand": deepcopy(state.T_wrist2hand)},
        "active_combo": deepcopy(state.active_combo),
        "pink_world": pink_world,
    }


def paired_pose(before, after):
    for field in ("robot", "arm", "base_link", "wrist_link", "tcp", "calibration", "active_combo"):
        if before[field] != after[field]:
            raise ValueError("采集期间 TCP 或激活配置发生变化，请重新录制")
    world_before, world_after = before.get("pink_world"), after.get("pink_world")
    if world_before and world_after:
        a, b = (np.asarray(w["world_T_root"]) for w in (world_before, world_after))
        if (world_before["anchor_count"] != world_after["anchor_count"]
                or np.linalg.norm(a[:3, 3] - b[:3, 3]) > .005):
            raise ValueError("采集期间机身或世界系发生变化，请静止后重新录制")
    joint_delta = float(np.degrees(max(abs(before["named_joints"][n] - v)
                                      for n, v in after["named_joints"].items())))
    t0, t1 = (np.asarray(p["T_base_from_tcp"]) for p in (before, after))
    position_delta = float(np.linalg.norm(t0[:3, 3] - t1[:3, 3]) * 1000)
    rotation_delta = float(np.degrees(Rotation.from_matrix(t0[:3, :3].T @ t1[:3, :3]).magnitude()))
    torso_delta = None
    if before["torso"] and after["torso"]:
        r0, r1 = (_torso_rotation(p["torso"]) for p in (before, after))
        if r0 is not None and r1 is not None:
            torso_delta = float(np.degrees(Rotation.from_matrix(r0.T @ r1).magnitude()))
    if joint_delta > 1 or position_delta > 5 or rotation_delta > 1 or (torso_delta or 0) > 1:
        raise ValueError("采集期间位姿变化过大，请保持手臂和机身静止后重新录制")
    return {**after, "before": before, "synchronization": {
        "method": "stationary_pose_samples_bracketing_fresh_rgbd",
        "hardware_synchronized": False, "selected_sample": "after",
        "window_ms": (after["sample_finished_unix_ns"] - before["sample_started_unix_ns"]) / 1e6,
        "max_joint_delta_deg": joint_delta, "tcp_position_delta_mm": position_delta,
        "tcp_rotation_delta_deg": rotation_delta, "torso_rotation_delta_deg": torso_delta,
    }}


def capture_bundle():
    from api.pointcloud_client import PointcloudClient
    content = PointcloudClient().recording_capture(state.chain_id)
    with np.load(io.BytesIO(content), allow_pickle=False) as archive:
        capture = json.loads(archive["capture_json"].tobytes().decode("utf-8"))
        pose = capture["source"]["robot_pose"]
        if capture.get("arm") != state.chain_id or pose.get("arm") != state.chain_id:
            raise ValueError("RGBD 采集返回的手臂与当前点位不一致")
        q = pose["named_joints"]
        if set(q) != set(state.joint_names) or not np.isfinite(list(q.values())).all():
            raise ValueError("RGBD 未包含有效的同次采集机械臂位姿")
        if not pose.get("synchronization"):
            raise ValueError("RGBD 缺少位姿同步记录，请更新采集服务")
        if pose.get("tcp", {}).get("p_tool_wrist_m") != list(state.p_tool):
            raise ValueError("采集期间 TCP 已切换，请重新录制")
        depth = archive["depth_mm"].copy()
        if depth.ndim != 2 or not np.isfinite(depth).all() or not np.any(depth > 0):
            raise ValueError("RGBD 深度数据无效")
        jpeg = archive["jpeg"].tobytes()
        if not jpeg.startswith(b"\xff\xd8"):
            raise ValueError("RGBD 彩色图像无效")
        return {"capture": capture, "pose": pose, "jpeg": jpeg, "depth": depth,
                "cloud": archive["cloud"].tobytes()}


def write_bundle(directory: Path, bundle, waypoint_file: str):
    """Called inside a temporary directory, before the waypoint is committed."""
    directory.mkdir()
    capture, pose = bundle["capture"], bundle["pose"]
    (directory / "rgb.jpg").write_bytes(bundle["jpeg"])
    np.save(directory / "depth_mm.npy", bundle["depth"], allow_pickle=False)
    (directory / "cloud.bin").write_bytes(bundle["cloud"])
    for filename, value in (("capture.json", capture), ("robot_pose.json", pose)):
        (directory / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    manifest = {
        "schema_version": 1, "waypoint_file": waypoint_file, "arm": pose["arm"],
        "capture_id": capture["capture_id"], "frame_id": capture["source"].get("frame_id"),
        "rgb": "rgb.jpg", "depth": "depth_mm.npy", "depth_unit": "mm",
        "depth_aligned_to": "rgb", "depth_shape": list(bundle["depth"].shape),
        "camera_intrinsics_fx_fy_cx_cy": capture["intrinsics"], "distortion": capture["distortion"],
        "robot_pose": "robot_pose.json", "capture": "capture.json", "pointcloud": "cloud.bin",
        "coordinate_status": "raw_capture_for_cabinet_localization",
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"schema_version": 1, "directory": f"{Path(waypoint_file).stem}/rgbd",
            "manifest": f"{Path(waypoint_file).stem}/rgbd/manifest.json",
            "capture_id": capture["capture_id"], "frame_id": manifest["frame_id"],
            "recorded_at": pose["recorded_at"], "depth_unit": "mm"}
