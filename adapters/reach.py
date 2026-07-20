"""Reach adapter：点击相机画面 → 3D 目标 → IK 预演 → 确认后真机执行。

按 README 的约定以"可选 adapter"形式挂在离线 API 外围：
不改动核心求解/规划代码，reach_server.py 启动时注入相机、
手眼标定结果和（可选的）H2 手臂控制器。不启用时主应用行为不变。

坐标链：
  像素(u,v) --深度反投影--> P_camera --T_cam2base--> P_torso
  --T_root_torso(全零关节)--> P_root（IK/查看器使用的 URDF 根坐标系）

由于 IK 链的 base 是 torso_link、查看器/求解器都在"腰部关节为 0"的
模型上工作，上述换算与真机腰部实际姿态无关（解出的只是手臂关节角）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter(prefix="/api/reach")


class ReachState:
    """由 reach_server 注入的运行时状态。"""

    def __init__(self):
        self.enabled = False
        self.camera = None                 # hand_eye_3D 的 CameraBase
        self.robot_id = "h2"
        self.chain_id = "right_arm"
        self.T_cam2root: np.ndarray | None = None   # URDF 根 <- 彩色相机
        self.T_cam2torso: np.ndarray | None = None  # torso_link <- 彩色相机
        self.p_tool: list[float] | None = None      # 指尖在腕系的位置（TCP 偏移）
        self.calib_meta: dict[str, Any] = {}
        self.controller = None             # H2ArmController，仅在前端"接管"后创建
        self.arm_factory = None            # 无参函数 -> H2ArmController；None = 无法真机执行
        self.provider_reader = None        # 只读 lowstate 关节读取（未接管时用）
        self.joint_names: list[str] = []
        self.arm_lock = threading.Lock()
        self.robot_model = None
        self.collision_checker = None
        self.obstacles: np.ndarray | None = None   # 体素中心（URDF 根系）
        self.obstacle_voxel = 0.05
        self.target_exclusion_m = 0.15
        self.waypoints_dir: Path | None = None     # 中间路点落盘目录（每个路点一个 json）
        # 执行线程状态
        self.exec_lock = threading.Lock()
        self.exec_thread: threading.Thread | None = None
        self.exec_cancel = threading.Event()
        self.exec_progress = 0.0
        self.exec_message = "空闲"
        self.exec_running = False


state = ReachState()


def configure(*, camera, robot_model, robot_id: str, chain_id: str, calib_path: Path,
              collision_checker=None, arm_factory=None, joints_reader=None) -> None:
    """由 reach_server 调用。calib_path 是 handeye3d_result.json。"""
    calib = json.loads(Path(calib_path).read_text())
    T_cam2torso = np.asarray(calib["T_cam2base"], dtype=float).reshape(4, 4)
    base_link = calib.get("base_link", "torso_link")

    # 全零关节下 URDF 根 <- base_link（腰 0 假设，与查看器/IK 一致）
    transforms = robot_model.forward_kinematics({})
    if base_link not in transforms:
        raise ValueError(f"标定的 base_link {base_link!r} 不在 URDF 中")
    T_root_torso = transforms[base_link]

    state.camera = camera
    state.robot_id = robot_id
    state.chain_id = chain_id
    state.T_cam2torso = T_cam2torso
    state.T_cam2root = T_root_torso @ T_cam2torso
    state.p_tool = [float(v) for v in calib["p_tool_wrist_m"]]
    state.calib_meta = {
        "path": str(calib_path),
        "base_link": base_link,
        "solved_at": calib.get("solved_at"),
        "rms_mm": calib.get("residual_mm", {}).get("rms"),
        "num_samples": calib.get("num_samples"),
    }
    state.arm_factory = arm_factory
    state.provider_reader = joints_reader
    state.joint_names = robot_model.joint_names(chain_id)
    state.robot_model = robot_model
    state.collision_checker = collision_checker
    state.waypoints_dir = Path(__file__).resolve().parent.parent / "reach_waypoints"
    state.enabled = True


def _read_joints():
    """接管后读控制器（同一份订阅），未接管读只读 provider。"""
    if state.controller is not None:
        return state.controller.read_measured()
    if state.provider_reader is not None:
        return state.provider_reader()
    raise RuntimeError("没有机器人状态源（mock 模式或 DDS 未连接）")


# --------------- 状态 / 视频流 ---------------


@router.get("/status")
async def reach_status():
    if not state.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "robot": state.robot_id,
        "chain_id": state.chain_id,
        "camera": state.camera.info(),
        "calib": state.calib_meta,
        "p_tool": state.p_tool,
        "T_cam2root": state.T_cam2root.tolist(),
        "arm_supported": state.arm_factory is not None,   # 有真机执行能力
        "armed": state.controller is not None,            # 前端已接管手臂
        "hand_move": bool(state.controller and state.controller.status()["float"]),
        "joints_available": (state.controller is not None
                             or state.provider_reader is not None),
        "exec": _exec_status(),
    }


@router.get("/stream")
async def reach_stream():
    def gen():
        while True:
            data = state.camera.get_jpeg()
            if data is None:
                time.sleep(0.2)
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                   + data + b"\r\n")
            time.sleep(0.05)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-cache"})


# --------------- 取点 ---------------


@router.post("/pick")
async def reach_pick(body: dict):
    """Body: {"u": int, "v": int, "approach_offset_m": float?}

    approach_offset_m：沿相机视线往回退的距离（指尖停在表面前方），默认 0.03。
    """
    if not state.enabled:
        return JSONResponse({"ok": False, "error": "reach 未启用"}, status_code=409)
    try:
        u, v = int(body["u"]), int(body["v"])
        offset = float(body.get("approach_offset_m", 0.03))
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "需要整数 u、v"}, status_code=400)

    result = state.camera.pick(u, v)
    if not result.get("ok"):
        return JSONResponse(result, status_code=502)

    p_cam = np.asarray(result["p_camera"], dtype=float)
    dist = float(np.linalg.norm(p_cam))
    if dist <= offset + 0.05:
        return JSONResponse(
            {"ok": False, "error": f"目标离相机太近（{dist:.2f} m），无法应用接近偏移"},
            status_code=400)
    p_cam_goal = p_cam * (1.0 - offset / dist)  # 沿视线退 offset

    def to_frame(T, p):
        return (T[:3, :3] @ p + T[:3, 3]).tolist()

    p_root_surface = to_frame(state.T_cam2root, p_cam)
    # 目标附近的环境障碍豁免：指尖要贴近表面，目标周围一小块不算障碍
    if state.collision_checker is not None:
        state.collision_checker.set_environment_exclusions(
            [(p_root_surface, state.target_exclusion_m)])

    return {
        "ok": True,
        "pixel": [u, v],
        "depth_mm": result["depth_mm"],
        "p_camera": p_cam.tolist(),
        "approach_offset_m": offset,
        "p_torso_surface": to_frame(state.T_cam2torso, p_cam),
        "p_torso": to_frame(state.T_cam2torso, p_cam_goal),
        "p_root": to_frame(state.T_cam2root, p_cam_goal),
        "p_root_surface": p_root_surface,
    }


# --------------- 环境障碍物（深度相机扫描） ---------------


def _self_filter(points_root: np.ndarray, margin: float) -> np.ndarray:
    """剔除属于机器人自身（手臂/躯干/头）的点，避免自己把自己当障碍。"""
    checker = state.collision_checker
    try:
        q = [float(v) for v in _read_joints()]
        joints = dict(zip(state.joint_names, q))
    except Exception:
        joints = {}
    from core.types import Pose

    transforms = state.robot_model.forward_kinematics(joints)
    # 用标定的 p_tool 当 TCP，让 hand 胶囊/tcp 球覆盖到真实指尖，过滤更完整
    tcp_pose = state.robot_model.tcp_pose(
        joints, state.chain_id, Pose(xyz=list(state.p_tool)))
    shapes = checker._build_shapes(transforms, state.chain_id, tcp_pose)

    keep = np.ones(len(points_root), dtype=bool)
    for shape in shapes:
        d = shape.data
        if shape.kind == "sphere":
            dist = np.linalg.norm(points_root - np.asarray(d["center"]), axis=1) - d["radius"]
        elif shape.kind == "capsule":
            a, b = np.asarray(d["a"]), np.asarray(d["b"])
            ab = b - a
            denom = float(np.dot(ab, ab))
            t = (np.clip((points_root - a) @ ab / denom, 0.0, 1.0)
                 if denom > 1e-12 else np.zeros(len(points_root)))
            dist = np.linalg.norm(points_root - (a + t[:, None] * ab), axis=1) - d["radius"]
        elif shape.kind == "box":
            R = np.asarray(d["rotation"])
            local = (points_root - np.asarray(d["center"])) @ R
            outside = np.maximum(np.abs(local) - np.asarray(d["half_extents"]), 0.0)
            dist = np.linalg.norm(outside, axis=1)
        else:
            continue
        keep &= dist > margin
    return points_root[keep]


@router.post("/scan_obstacles")
async def reach_scan_obstacles(body: dict | None = None):
    """扫一帧深度图 → 躯干系体素障碍物，注入碰撞检查。

    Body(可选): {"voxel_m": 0.05, "max_range_m": 1.5, "self_margin_m": 0.10}
    建议扫描时把手臂放低（移出电柜方向视野），残留的手臂点会被自体过滤兜底。
    """
    if state.collision_checker is None:
        return JSONResponse({"ok": False, "error": "碰撞检查器未注入"}, status_code=409)
    body = body or {}
    voxel = float(body.get("voxel_m", 0.05))
    max_range = float(body.get("max_range_m", 1.5))
    self_margin = float(body.get("self_margin_m", 0.10))

    snap = state.camera.depth_snapshot()
    if snap is None:
        return JSONResponse({"ok": False, "error": "还没有深度帧"}, status_code=502)
    depth_mm, (fx, fy, cx, cy) = snap
    h, w = depth_mm.shape

    stride = max(1, int(round(max(h, w) / 240)))  # 采样到 ~240 列，够 5cm 体素用
    d = depth_mm[::stride, ::stride].astype(float) / 1000.0
    vs, us = np.mgrid[0:h:stride, 0:w:stride]
    valid = (d > 0.15) & (d < max_range)
    z = d[valid]
    u = us[valid].astype(float)
    v = vs[valid].astype(float)
    pts_cam = np.stack([(u - cx) * z / fx, (v - cy) * z / fy, z], axis=1)

    pts_root = pts_cam @ state.T_cam2root[:3, :3].T + state.T_cam2root[:3, 3]
    pts_root = _self_filter(pts_root, self_margin)
    if not len(pts_root):
        return JSONResponse({"ok": False, "error": "过滤后没有剩余点（全是自身/超范围？）"},
                            status_code=400)

    # 体素化去重
    idx = np.unique(np.floor(pts_root / voxel).astype(np.int64), axis=0)
    centers = (idx + 0.5) * voxel

    state.obstacles = centers
    state.obstacle_voxel = voxel
    state.collision_checker.set_environment(centers, radius=voxel * 0.75)
    return {"ok": True, "count": int(len(centers)), "voxel_m": voxel,
            "raw_points": int(len(pts_root))}


@router.post("/clear_obstacles")
async def reach_clear_obstacles():
    if state.collision_checker is not None:
        state.collision_checker.clear_environment()
    state.obstacles = None
    return {"ok": True, "count": 0}


@router.get("/obstacles")
async def reach_obstacles():
    return {
        "count": 0 if state.obstacles is None else int(len(state.obstacles)),
        "voxel_m": state.obstacle_voxel,
        "centers": [] if state.obstacles is None else state.obstacles.tolist(),
    }


# --------------- 中间路点（录制 / 落盘 / 复用） ---------------


def _safe_waypoint_file(filename: str) -> Path | None:
    """防路径穿越：只允许目录内的 *.json 纯文件名。"""
    if not filename.endswith(".json") or "/" in filename or "\\" in filename or ".." in filename:
        return None
    return state.waypoints_dir / filename


def _load_waypoints() -> list[dict]:
    if not state.waypoints_dir.is_dir():
        return []
    items = []
    for path in sorted(state.waypoints_dir.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text())
            data["file"] = path.name
            items.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return items


@router.get("/waypoints")
async def reach_waypoints():
    return {"waypoints": _load_waypoints()}


@router.post("/waypoints")
async def reach_record_waypoint(body: dict):
    """把真机当前关节角录制为命名路点，每个路点单独落盘为
    reach_waypoints/<名字>_<时间戳>.json。Body: {"name": str}

    典型流程：接管手臂 → 卸力 → 人手摆到中间位 → 恢复保持 → 录制。
    """
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "路点需要名字"}, status_code=400)
    if "/" in name or "\\" in name or ".." in name:
        return JSONResponse({"ok": False, "error": "名字不能包含路径分隔符"}, status_code=400)
    try:
        q = [float(v) for v in _read_joints()]
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"读不到真机关节: {exc}"}, status_code=503)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    item = {
        "name": name,
        "chain_id": state.chain_id,
        "named_joints": dict(zip(state.joint_names, q)),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state.waypoints_dir.mkdir(parents=True, exist_ok=True)
    path = state.waypoints_dir / f"{name}_{stamp}.json"
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2))
    item["file"] = path.name
    return {"ok": True, "waypoint": item}


@router.delete("/waypoints/{filename}")
async def reach_delete_waypoint(filename: str):
    path = _safe_waypoint_file(filename)
    if path is None:
        return JSONResponse({"ok": False, "error": "非法文件名"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": f"没有文件 {filename!r}"}, status_code=404)
    path.unlink()
    return {"ok": True}


@router.post("/hand_move")
async def reach_hand_move(body: dict | None = None):
    """卸力开关（摆中间位用）。Body: {"on": bool}

    on=true: kp=0 低阻尼，人可拖动手臂（会下坠，务必扶住！）
    on=false: 在人放置的位置重新抓取并刚性保持。
    """
    if state.controller is None:
        return JSONResponse({"ok": False, "error": "手臂未接管"}, status_code=409)
    if state.exec_running:
        return JSONResponse({"ok": False, "error": "轨迹执行中不能卸力"}, status_code=409)
    on = bool((body or {}).get("on"))
    if on:
        if not state.controller.enter_hand_move():
            return JSONResponse({"ok": False, "error": "点动模式中不能卸力，请先停止"},
                                status_code=409)
        return {"ok": True, "hand_move": True, "message": "已卸力，请扶住手臂后再拖动"}
    state.controller.stop()  # 退出卸力：从人放置的位置抓取保持
    return {"ok": True, "hand_move": False, "message": "已恢复刚性保持（当前位置）"}


# --------------- 真机关节 / 执行 ---------------


@router.get("/joints")
async def reach_joints():
    """当前真机该链关节角（named dict），作为 IK 起点。"""
    try:
        q = [float(v) for v in _read_joints()]
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return {"ok": True, "named_joints": dict(zip(state.joint_names, q))}


# --------------- 接管 / 释放手臂（由前端操作） ---------------


@router.post("/arm")
async def reach_arm():
    """接管手臂：创建控制器、发布 rt/arm_sdk、在当前姿态刚性保持。

    真机会被立即接管！前端须先经人确认，且确保没有其他控制程序。
    """
    if state.arm_factory is None:
        return JSONResponse(
            {"ok": False, "error": "本次启动不支持真机执行（mock 模式或 DDS 不可用）"},
            status_code=409)
    with state.arm_lock:
        if state.controller is not None:
            return {"ok": True, "armed": True, "message": "已处于接管状态"}
        try:
            controller = state.arm_factory()
            controller.start()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"接管失败: {exc}"}, status_code=502)
        state.controller = controller
    return {"ok": True, "armed": True, "message": "已接管，手臂在当前姿态刚性保持"}


@router.post("/disarm")
async def reach_disarm():
    """释放手臂：权重渐出、交还本体控制器。调用前请扶住手臂。"""
    with state.arm_lock:
        if state.controller is None:
            return {"ok": True, "armed": False, "message": "本来就未接管"}
        if state.exec_running:
            return JSONResponse(
                {"ok": False, "error": "轨迹执行中不能释放，请先急停"}, status_code=409)
        controller, state.controller = state.controller, None
    controller.shutdown()
    return {"ok": True, "armed": False, "message": "已释放，控制权交还本体控制器"}


def _exec_status() -> dict:
    return {
        "running": state.exec_running,
        "progress": state.exec_progress,
        "message": state.exec_message,
    }


@router.get("/exec_status")
async def reach_exec_status():
    return _exec_status()


@router.post("/execute")
async def reach_execute(body: dict):
    """执行已规划的关节轨迹（真机运动！前端须先经人确认）。

    Body: {"waypoints": [named_joints, ...], "duration": float}
    """
    if state.controller is None:
        return JSONResponse(
            {"ok": False, "error": "手臂未接管，请先在页面上点「接管手臂」"},
            status_code=409)
    waypoints = body.get("waypoints") or []
    duration = float(body.get("duration") or 4.0)
    if len(waypoints) < 2:
        return JSONResponse({"ok": False, "error": "轨迹至少要有 2 个路点"}, status_code=400)
    try:
        q_list = [np.asarray([float(wp[name]) for name in state.joint_names], dtype=float)
                  for wp in waypoints]
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"路点关节缺失/非法: {exc}"}, status_code=400)

    with state.exec_lock:
        if state.exec_running:
            return JSONResponse({"ok": False, "error": "已有轨迹在执行中"}, status_code=409)

        # 起点必须贴近真机当前姿态，防止规划时用的起点已经过期
        measured = state.controller.read_measured()
        start_gap = float(np.max(np.abs(q_list[0] - measured)))
        if start_gap > 0.15:
            return JSONResponse(
                {"ok": False,
                 "error": f"轨迹起点与真机当前姿态差 {start_gap:.3f} rad（>0.15），"
                          "手臂可能已被移动，请重新取点规划"},
                status_code=409)

        state.exec_cancel.clear()
        state.exec_running = True
        state.exec_progress = 0.0
        state.exec_message = "执行中"
        state.exec_thread = threading.Thread(
            target=_exec_loop, args=(q_list, duration), daemon=True)
        state.exec_thread.start()
    return {"ok": True, **_exec_status()}


def _exec_loop(q_list: list[np.ndarray], duration: float) -> None:
    ctl = state.controller
    try:
        ctl.enable_jog()
        n = len(q_list)
        dt = duration / max(n - 1, 1)
        t0 = time.monotonic()
        for i, q in enumerate(q_list):
            if state.exec_cancel.is_set():
                ctl.disable_jog()
                state.exec_message = "已中止（保持当前位置）"
                return
            ctl.set_target(q)
            state.exec_progress = i / (n - 1)
            target_t = t0 + (i + 1) * dt
            while True:
                remaining = target_t - time.monotonic()
                if remaining <= 0 or state.exec_cancel.is_set():
                    break
                time.sleep(min(remaining, 0.05))
        # 最终目标已下发；等限速滑动真正到位再冻结（时长偏短时控制器会滞后）
        state.exec_message = "收敛中"
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not state.exec_cancel.is_set():
            status = ctl.status()
            gap = float(np.max(np.abs(
                np.asarray(status["desired_rad"]) - np.asarray(status["cmd_rad"]))))
            if gap < 1e-3:
                break
            time.sleep(0.1)
        ctl.disable_jog()
        state.exec_progress = 1.0
        # 实测残差 = 指令目标 - 电机实测。明显偏大（>0.05 rad）通常是 kp 太低被重力压住
        sag_note = ""
        try:
            time.sleep(0.3)
            measured = np.asarray(ctl.read_measured())
            desired = np.asarray(ctl.status()["desired_rad"])
            sag = float(np.max(np.abs(desired - measured)))
            sag_note = f"，实测残差 {sag:.3f} rad" + ("（偏大，考虑调高 --arm-kp）" if sag > 0.05 else "")
        except Exception:
            pass
        state.exec_message = ("已中止（保持当前位置）" if state.exec_cancel.is_set()
                              else f"完成（刚性保持在目标位{sag_note}）")
    except Exception as exc:
        try:
            ctl.stop()
        except Exception:
            pass
        state.exec_message = f"执行出错已停止: {exc}"
    finally:
        state.exec_running = False


@router.post("/stop")
async def reach_stop():
    """急停：中止执行线程并冻结在当前指令位。"""
    if state.controller is None:
        return JSONResponse({"ok": False, "error": "手臂未接管"}, status_code=409)
    state.exec_cancel.set()
    state.controller.stop()
    state.exec_message = "已急停（刚性保持）"
    return {"ok": True, **_exec_status()}
