"""Relocalize recorded RGBD TCP poses against the same physical cabinet panel.

Only the endpoint is constrained. Planning never commands hardware; /execute
accepts the resulting path using a short-lived, server-issued proof.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import time
import uuid
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from fastapi.responses import JSONResponse

from . import orientation as o
from .cabinet_assist import parse_assist
from .state import _read_torso, _torso_rotation, router, state
from .waypoint_rgbd import capture_bundle

PLAN_TTL_S = 90.
ASSETS = ("capture.json", "robot_pose.json", "depth_mm.npy", "rgb.jpg", "manifest.json")


def transform(value):
    t = np.asarray(value, dtype=float)
    if t.shape != (4, 4) or not np.isfinite(t).all() or not np.allclose(t[3], [0, 0, 0, 1]):
        raise ValueError("点位变换矩阵无效")
    o.rotation(t[:3, :3])
    return t


def compatible_pose(pose):
    if (pose.get("robot") != state.robot_id or pose.get("arm") != state.chain_id
            or pose.get("base_link") != state.base_link
            or pose.get("wrist_link") != state.robot_model.end_link(state.chain_id)):
        raise ValueError("点位与当前机器人、手臂或末端连杆不匹配")
    tcp = pose.get("tcp", {})
    p = np.asarray(tcp.get("p_tool_wrist_m"), dtype=float)
    if (tcp.get("orientation_definition") != "wrist_link_axes" or p.shape != (3,)
            or not np.allclose(p, state.p_tool, atol=1e-8, rtol=0)):
        raise ValueError("点位 TCP 与当前 TCP 不一致，请选择录制时的 TCP")
    cal = pose.get("calibration", {})
    if not cal.get("ready") or not state.handeye_ready:
        raise ValueError("点位或当前手眼标定未就绪")
    transform(cal.get("T_base_from_camera"))
    if state.T_wrist2hand is None or not np.allclose(
            transform(cal.get("T_wrist_from_hand")), transform(state.T_wrist2hand), atol=1e-8):
        raise ValueError("手部安装标定已变化，请重新录制点位")
    for key in ("hand_id", "mount_profile_id", "camera_role"):
        if pose.get("active_combo", {}).get(key) != (state.active_combo or {}).get(key):
            raise ValueError("点位手型号、安装方案或相机与当前配置不匹配")
    transform(pose.get("T_base_from_tcp"))
    if not pose.get("synchronization"):
        raise ValueError("点位缺少 RGBD 与机械臂位姿的同步记录")


def archive(file):
    if not isinstance(file, str) or Path(file).name != file or not file.endswith(".json"):
        raise ValueError("点位文件名非法")
    if state.waypoints_dir is None:
        raise ValueError("点位目录不可用")
    root = Path(state.waypoints_dir).resolve()
    path = root / file
    directory = root / Path(file).stem / "rgbd"
    paths = [path, *(directory / name for name in ASSETS)]
    if any(not p.is_file() or not p.resolve().is_relative_to(root) for p in paths):
        raise ValueError("点位缺少完整 RGBD 记录，请重新录制点位 + RGBD")
    waypoint = json.loads(path.read_text())
    if waypoint.get("rgbd", {}).get("directory") != f"{Path(file).stem}/rgbd":
        raise ValueError("点位 RGBD 目录不匹配")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("waypoint_file") != file or manifest.get("arm") != state.chain_id:
        raise ValueError("RGBD 清单与点位或手臂不匹配")
    pose = json.loads((directory / "robot_pose.json").read_text())
    compatible_pose(pose)
    digest = hashlib.sha256()
    for p in paths:
        digest.update(p.name.encode())
        digest.update(p.read_bytes())
    return waypoint, pose, directory, digest.hexdigest()


def panel_frame(bundle, config):
    from api.cabinet_frame import build_cabinet_frame
    from api.pointcloud_core import build_pointcloud
    if not config or config.get("method") != "method2_panel_edges":
        raise ValueError("A/B 点位需要按面板矩形边建立柜面坐标系，请使用方法二录制")
    capture, depth = bundle["capture"], bundle["depth"]
    bgr = cv2.imdecode(np.frombuffer(bundle["jpeg"], np.uint8), cv2.IMREAD_COLOR)
    if bgr is None or bgr.shape[:2] != depth.shape:
        raise ValueError("RGBD 图像尺寸不一致")
    # Knob classes are irrelevant. The panel must be the same physical panel.
    cloud = build_pointcloud(depth, bgr, capture["intrinsics"], [],
        stride=int(config["params"].get("stride", 3)),
        z_min_m=float(capture["z_min_m"]), z_max_m=float(capture["z_max_m"]),
        max_points=350_000, dense_box_sampling=False,
        distortion=np.asarray(capture["distortion"]))
    frame = build_cabinet_frame(config, cloud.positions, cloud.pixels, depth.shape,
        depth_mm=depth, intrinsics=capture["intrinsics"], boxes=capture["boxes"])
    rectangle = frame["panel_rectangle"]
    camera_panel = np.eye(4)
    camera_panel[:3, :3] = np.column_stack([frame[f"{axis}_axis_camera"] for axis in "xyz"])
    # Never use origin_camera_m: camera projection is not a fixed cabinet origin.
    camera_panel[:3, 3] = rectangle["center_camera_m"]
    transform(camera_panel)
    size = np.array([rectangle["long_length_m"], rectangle["short_length_m"]])
    if not np.isfinite(size).all() or np.any(size <= 0) or frame["rms_m"] > .004:
        raise ValueError("柜面定位质量不足，请露出完整面板后重试")
    return camera_panel, size, frame


@lru_cache(maxsize=16)
def _saved_geometry(directory_string, digest):
    directory = Path(directory_string)
    capture = json.loads((directory / "capture.json").read_text())
    pose = json.loads((directory / "robot_pose.json").read_text())
    if capture["source"]["robot_pose"] != pose:
        raise ValueError("RGBD 与机械臂位姿不是同一次记录")
    config = capture["recording"]["cabinet_frame_config"]
    bundle = {"capture": capture, "depth": np.load(directory / "depth_mm.npy", allow_pickle=False),
              "jpeg": (directory / "rgb.jpg").read_bytes()}
    camera_panel, size, frame = panel_frame(bundle, config)
    base_panel = transform(pose["calibration"]["T_base_from_camera"]) @ camera_panel
    panel_tcp = np.linalg.inv(base_panel) @ transform(pose["T_base_from_tcp"])
    return panel_tcp, size, config, frame


def configuration_key():
    return o.reference_key({"robot": state.robot_id, "arm": state.chain_id,
        "tcp": list(state.p_tool), "ready": state.handeye_ready,
        "camera": None if state.T_cam2torso is None else np.asarray(state.T_cam2torso).tolist(),
        "hand": None if state.T_wrist2hand is None else np.asarray(state.T_wrist2hand).tolist(),
        "combo": state.active_combo, "calibration": state.calib_meta})


def validate_plan(plan, backend):
    if plan["control_revision"] != state.cabinet_plan_revision:
        raise ValueError("规划期间已急停或释放手臂，请重新规划")
    if time.time() - plan["created_at_unix"] > PLAN_TTL_S:
        raise ValueError("柜面点位规划已过期，请重新拍摄并规划")
    if configuration_key() != plan["configuration_key"]:
        raise ValueError("TCP 或标定配置已变化，请重新规划")
    if state.pick_revision != plan["pick_revision"]:
        raise ValueError("规划后选点已变化，请重新规划")
    if archive(plan["reference_id"])[3] != plan["reference_key"]:
        raise ValueError("点位记录已变化，请重新规划")
    if backend == "pink":
        rt = state.pink_runtime
        if (rt is None or not rt.world_frame.anchored or plan["world_T_root_ref"] is None
                or rt.world_frame.anchor_count != plan["anchor_count"]):
            raise ValueError("PINK 世界系未锚定或已变化，请锚定后重新规划")
    else:
        before, now = plan.get("torso"), _read_torso()
        if before is not None:
            a, b = _torso_rotation(before), _torso_rotation(now or {})
            if a is None or b is None or o.angle_deg(a, b) > 1.:
                raise ValueError("定位后躯干姿态已变化，请保持机身静止并重新规划")


@router.get("/cabinet_waypoints")
def list_waypoints():
    entries = []
    if state.waypoints_dir:
        for path in sorted(Path(state.waypoints_dir).glob("*.json")):
            try:
                raw = json.loads(path.read_text())
                if not raw.get("rgbd") or raw.get("arm") != state.chain_id:
                    continue
                item = {"file": path.name, "name": raw.get("name", path.stem), "compatible": True}
                try:
                    archive(path.name)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    item.update(compatible=False, error=str(exc))
                entries.append(item)
            except (OSError, ValueError):
                continue
    return {"ok": True, "waypoints": entries}


def solve(args):
    ctx = mp.get_context("fork")
    rx, tx = ctx.Pipe(duplex=False)
    process = ctx.Process(target=o._worker, args=(tx, args), daemon=True)
    process.start()
    tx.close()
    try:
        if not rx.poll(30.):
            raise ValueError("点位规划超时，请缩短移动距离")
        ok, result = rx.recv()
        if not ok:
            raise ValueError(result)
        return result
    finally:
        rx.close()
        process.join(timeout=.5)
        if process.is_alive():
            process.kill()
            process.join(timeout=1.)


@router.post("/plan_cabinet_waypoint")
def plan_waypoint(body: dict):
    if not state.cabinet_plan_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "正在定位和规划，请稍候"}, 409)
    control_revision = state.cabinet_plan_revision
    try:
        if state.exec_running or state.align_running:
            raise ValueError("请等待当前运动结束后规划")
        assist = parse_assist(body.get("cabinet_assist"))
        arrival = ({"arrival_tolerance": o.parse_arrival_tolerance(body["arrival_tolerance"])}
                   if "arrival_tolerance" in body else {})
        offsets = {}
        for axis in "xyz":
            raw_offset = body.get(f"cabinet_{axis}_offset_mm", 0.)
            if isinstance(raw_offset, bool) or not isinstance(raw_offset, (int, float)):
                raise ValueError(f"柜面 {axis.upper()} 偏移必须是数字，单位 mm")
            value = float(raw_offset)
            if not np.isfinite(value) or abs(value) > 100.:
                raise ValueError(f"柜面 {axis.upper()} 偏移必须在 −100 到 +100 mm 之间")
            offsets[f"cabinet_{axis}_offset_mm"] = value
        offset_xyz_mm = np.array(list(offsets.values()))
        file = body.get("file")
        waypoint, pose, directory, digest = archive(file)
        saved, size, config, _ = _saved_geometry(str(directory), digest)
        key, revision = configuration_key(), state.pick_revision
        bundle = capture_bundle()  # Fresh RGBD + bracketed, measured current pose.
        current = bundle["pose"]
        compatible_pose(current)
        if time.time_ns() - current["sample_finished_unix_ns"] > 10_000_000_000:
            raise ValueError("RGBD 位姿已过期，请重新采集")
        camera_panel, current_size, frame = panel_frame(bundle, config)
        if np.any(np.abs(current_size / size - 1) > .15):
            raise ValueError("当前面板尺寸与记录不一致，请对准同一面板后重试")
        # IK uses the URDF with zero waist, while camera extrinsic is torso-relative.
        root_base = state.robot_model.forward_kinematics({})[state.base_link]
        root_panel = root_base @ transform(current["calibration"]["T_base_from_camera"]) @ camera_panel
        # Shift the destination along the cabinet axes, not the robot or TCP
        # axes. Copy cached geometry: per-direction offsets must never accumulate.
        target_panel = saved.copy()
        target_panel[:3, 3] += offset_xyz_mm / 1000.
        goal = root_panel @ target_panel
        q_start = current["named_joints"]
        world = current.get("pink_world") or {}
        world_root = transform(world["world_T_root"]) if world.get("world_T_root") is not None else None
        proof = {"id": uuid.uuid4().hex, "reference_kind": "cabinet_waypoint", "scope": "goal",
            "reference_id": file, "name": waypoint["name"], "reference_key": digest,
            "configuration_key": key, "pick_revision": revision, "created_at_unix": time.time(),
            "control_revision": control_revision,
            **offsets,
            **arrival,
            "cabinet_assist": assist, "cabinet_x_axis_root": root_panel[:3, 0].tolist(),
            "target_root": goal[:3, 3].tolist(), "R_root_tcp": goal[:3, :3].tolist(),
            "p_tool": list(state.p_tool), "torso": current.get("torso"),
            "world_T_root_ref": None if world_root is None else world_root.tolist(),
            "R_world_tcp": None if world_root is None else (world_root[:3, :3] @ goal[:3, :3]).tolist(),
            "anchor_count": world.get("anchor_count"), "tolerance_deg": o.TOLERANCE_DEG,
            "capture_id": bundle["capture"]["capture_id"], "panel_fit_rms_mm": frame["rms_m"] * 1000}
        backend = body.get("motion_backend") or state.motion_backend or "legacy"
        if backend not in ("legacy", "legacy_timed", "pink"):
            raise ValueError("未知执行后端")
        validate_plan(proof, backend)
        actual = root_base @ transform(current["T_base_from_tcp"])
        position_error = float(np.linalg.norm(actual[:3, 3] - goal[:3, 3]) * 1000)
        rotation_error = o.angle_deg(actual[:3, :3], goal[:3, :3])
        limits = arrival.get("arrival_tolerance") or {"position_mm": 2., "orientation_deg": .35}
        if position_error <= limits["position_mm"] and rotation_error <= limits["orientation_deg"]:
            state.orientation_plan = None
            return {"ok": True, "already_at_target": True, "name": waypoint["name"],
                    **offsets,
                    **arrival,
                    "cabinet_assist": assist,
                    "goal_position_error_mm": position_error, "goal_orientation_error_deg": rotation_error}
        result = solve((q_start, goal[:3, 3], goal[:3, :3], root_panel[:3, :3],
                        "direct", 0., bool(body.get("check_collision", False)), (), arrival.get("arrival_tolerance")))
        validate_plan(proof, backend)
        q_list = [[w["named_joints"][n] for n in state.joint_names] for w in result["waypoints"]]
        proof["path_key"] = o.path_key(q_list)
        state.orientation_plan = proof
        return {**result, **offsets, **arrival, "cabinet_assist": assist,
            "orientation": deepcopy(proof), "cabinet": {
            "origin": "panel_rectangle_center", "capture_id": proof["capture_id"],
            "target_panel_tcp": target_panel.tolist(),
            **{f"{axis}_offset_mm": offsets[f"cabinet_{axis}_offset_mm"] for axis in "xyz"},
            "panel_fit_rms_mm": proof["panel_fit_rms_mm"], "target_file": file}}
    except (OSError, ValueError, KeyError, TypeError, EOFError, RuntimeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)
    finally:
        state.cabinet_plan_lock.release()
