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
import math
import threading
import time
from datetime import datetime
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
        self.torso_reader = None           # 只读腰关节 + IMU（躯干姿态诊断）
        self.base_link = "torso_link"
        self.pick_torso: dict | None = None        # 取点时刻的躯干姿态
        self.pick_target_torso: list[float] | None = None
        self.pick_target_root: list[float] | None = None
        self.pick_pixel: list[int] | None = None
        self.torso_diag: dict | None = None        # 最近一次执行的躯干漂移诊断
        self.log_dir: Path | None = None           # 每段执行落一行 JSONL
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.joint_names: list[str] = []
        self.arm_lock = threading.Lock()
        self.robot_model = None
        self.collision_checker = None
        self.obstacles: np.ndarray | None = None   # 体素中心（URDF 根系）
        self.obstacle_voxel = 0.05
        self.target_exclusion_m = 0.15
        self.plane: dict | None = None             # 取点时拟合的目标表面平面（根系）
        self.ik_solver = None                      # 笛卡尔直线插补用的 IK 求解器
        self.waypoints_dir: Path | None = None     # 中间路点落盘目录（每个路点一个 json）
        # 执行线程状态
        self.exec_lock = threading.Lock()
        self.exec_thread: threading.Thread | None = None
        self.exec_cancel = threading.Event()
        self.exec_progress = 0.0
        self.exec_message = "空闲"
        self.exec_running = False
        self.exec_phase = "idle"           # traj/converge/settle/push_hold/release


state = ReachState()


def configure(*, camera, robot_model, robot_id: str, chain_id: str, calib_path: Path,
              collision_checker=None, ik_solver=None, arm_factory=None,
              joints_reader=None, torso_reader=None) -> None:
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
    state.torso_reader = torso_reader
    state.base_link = base_link
    state.joint_names = robot_model.joint_names(chain_id)
    state.robot_model = robot_model
    state.collision_checker = collision_checker
    state.ik_solver = ik_solver
    state.waypoints_dir = Path(__file__).resolve().parent.parent / "reach_waypoints"
    state.log_dir = Path(__file__).resolve().parent.parent / "reach_logs"
    state.enabled = True


def _read_joints():
    """接管后读控制器（同一份订阅），未接管读只读 provider。"""
    if state.controller is not None:
        return state.controller.read_measured()
    if state.provider_reader is not None:
        return state.provider_reader()
    raise RuntimeError("没有机器人状态源（mock 模式或 DDS 未连接）")


# --------------- 躯干姿态诊断 ---------------
#
# 手臂 IK 全程在 torso_link 系下解算，目标点也是取点瞬间换算进 torso 系的。
# 但开关长在世界里不动：只要躯干在"取点"到"到位"之间转了，同一个 torso 系
# 坐标就不再指向那个开关了。本体控制器在运动模式下会为了平衡而动腰/踝，
# 手臂抬起来时躯干可能后仰几度——这时手臂关节角完全到位，指尖照样偏。
# 这组函数只负责如实测出"躯干转了多少、折算到指尖是多少毫米"，不做补偿。


def _read_torso() -> dict | None:
    if state.controller is not None and hasattr(state.controller, "read_torso_state"):
        return state.controller.read_torso_state()
    if state.torso_reader is not None:
        return state.torso_reader()
    return None


def _torso_rotation(torso: dict) -> np.ndarray | None:
    """躯干在世界系下的姿态 R = R_world←pelvis(IMU) · R_pelvis←torso(腰关节 FK)。"""
    try:
        waist = {f"{name}_joint": float(value)
                 for name, value in zip(torso["waist_names"], torso["waist_rad"])}
        R_pelvis_torso = state.robot_model.forward_kinematics(waist)[state.base_link][:3, :3]
        w, x, y, z = [float(v) for v in torso["imu_quat"]]
        norm = (w * w + x * x + y * y + z * z) ** 0.5
        if norm < 1e-9:
            return None
        w, x, y, z = w / norm, x / norm, y / norm, z / norm
        R_world_pelvis = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ])
        return R_world_pelvis @ R_pelvis_torso
    except Exception:
        return None


def _torso_drift(before: dict | None, after: dict | None) -> dict | None:
    """取点时 vs 到位时的躯干姿态差，并折算成目标点在 torso 系里跑了多远。"""
    if not before or not after:
        return None
    diag: dict[str, Any] = {
        "waist_delta_deg": [round(math.degrees(b - a), 2) for a, b in
                            zip(before["waist_rad"], after["waist_rad"])],
        "waist_names": list(after.get("waist_names", [])),
        "imu_rpy_delta_deg": [round(math.degrees(b - a), 2) for a, b in
                              zip(before["imu_rpy"], after["imu_rpy"])],
        "imu_rpy_deg": [round(math.degrees(v), 2) for v in after["imu_rpy"]],
    }
    R0, R1 = _torso_rotation(before), _torso_rotation(after)
    if R0 is None or R1 is None:
        return diag
    dR = R1.T @ R0          # 取点时的 torso 坐标 → 现在的 torso 坐标
    cos = (float(np.trace(dR)) - 1.0) / 2.0
    diag["torso_rotation_deg"] = round(math.degrees(math.acos(max(-1.0, min(1.0, cos)))), 2)
    if state.pick_target_torso is not None:
        p = np.asarray(state.pick_target_torso, dtype=float)
        diag["target_shift_mm"] = round(float(np.linalg.norm(dR @ p - p)) * 1000.0, 1)
    return diag


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

    approach_offset_m：沿被点表面的法线、朝机器人方向后退的距离
    （即垂直于障碍物平面的间隙），默认 0.015；负值 = 指令位置压入表面，
    接触后位置误差消不掉，电机持续出力（掰开关用）。
    平面拟合失败时退化为沿相机视线后退。
    """
    if not state.enabled:
        return JSONResponse({"ok": False, "error": "reach 未启用"}, status_code=409)
    try:
        u, v = int(body["u"]), int(body["v"])
        offset = float(body.get("approach_offset_m", 0.015))
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

    # 拟合目标表面平面（接近偏移沿它的法线退；横移的"左"方向也以它定义）
    state.plane = _fit_surface_plane(p_cam)
    if state.plane is not None:
        # 沿表面法线（指向机器人一侧）退 offset：垂直于障碍物平面的真实间隙，
        # 不受视线斜射角影响，也没有沿面的横向偏移
        n_cam = np.asarray(state.plane["normal_cam"], dtype=float)
        p_cam_goal = p_cam + offset * n_cam
        offset_mode = "plane_normal"
    else:
        p_cam_goal = p_cam * (1.0 - offset / dist)  # 兜底：沿视线退
        offset_mode = "camera_ray"

    def to_frame(T, p):
        return (T[:3, :3] @ p + T[:3, 3]).tolist()

    p_root_surface = to_frame(state.T_cam2root, p_cam)
    # 目标附近的环境障碍豁免：指尖要贴近表面，目标周围一小块不算障碍
    if state.collision_checker is not None:
        state.collision_checker.set_environment_exclusions(
            [(p_root_surface, state.target_exclusion_m)])

    # 记下此刻的躯干姿态：目标从这一刻起被"冻结"在 torso 系里，
    # 之后躯干只要转了，同一坐标就不再对准那个开关（执行完会给出偏差）
    state.pick_target_torso = to_frame(state.T_cam2torso, p_cam_goal)
    state.pick_target_root = to_frame(state.T_cam2root, p_cam_goal)
    state.pick_pixel = [u, v]
    state.pick_torso = _read_torso()
    state.torso_diag = None

    return {
        "ok": True,
        "pixel": [u, v],
        "depth_mm": result["depth_mm"],
        "p_camera": p_cam.tolist(),
        "approach_offset_m": offset,
        "offset_mode": offset_mode,
        "p_torso_surface": to_frame(state.T_cam2torso, p_cam),
        "p_torso": state.pick_target_torso,
        "p_root": to_frame(state.T_cam2root, p_cam_goal),
        "p_root_surface": p_root_surface,
        "plane": state.plane,
    }


def _fit_surface_plane(p_cam_surface: np.ndarray, radius: float = 0.12) -> dict | None:
    """在被点表面点周围拟合平面（SVD 最小二乘），返回根系下的
    法线（指向机器人）、"左"方向（面向平面时的左，嵌在平面内）等。
    拟合失败（点太少/平面水平）返回 None。"""
    snap = state.camera.depth_snapshot()
    if snap is None:
        return None
    depth_mm, (fx, fy, cx, cy) = snap
    h, w = depth_mm.shape
    stride = max(1, int(round(max(h, w) / 320)))
    d = depth_mm[::stride, ::stride].astype(float) / 1000.0
    vs, us = np.mgrid[0:h:stride, 0:w:stride]
    valid = (d > 0.15) & (d < 3.0)
    pts = np.stack([(us[valid] - cx) * d[valid] / fx,
                    (vs[valid] - cy) * d[valid] / fy,
                    d[valid]], axis=1)
    near = pts[np.linalg.norm(pts - p_cam_surface, axis=1) < radius]
    if len(near) < 50:
        return None

    center = near.mean(axis=0)
    q = near - center
    _, _, vt = np.linalg.svd(q, full_matrices=False)
    n = vt[2]
    rms = float(np.sqrt(np.mean((q @ n) ** 2)))
    if float(np.dot(n, -center)) < 0:
        n = -n  # 法线指向相机（即机器人一侧）

    R = state.T_cam2root[:3, :3]
    n_root = R @ n
    center_root = R @ center + state.T_cam2root[:3, 3]
    facing = -n_root                      # 机器人 → 平面
    up = np.array([0.0, 0.0, 1.0])
    left = np.cross(up, facing)           # 面向平面时的左手方向
    norm = float(np.linalg.norm(left))
    if norm < 1e-3:
        return None                       # 平面接近水平，"左"无定义
    left /= norm
    left -= float(np.dot(left, n_root)) * n_root   # 嵌入平面内
    left /= float(np.linalg.norm(left))
    return {
        "center_root": center_root.tolist(),
        "normal_root": n_root.tolist(),
        "normal_cam": n.tolist(),   # 相机系法线（指向机器人一侧），接近偏移沿它退
        "left_root": left.tolist(),
        "rms_mm": rms * 1000.0,
        "points": int(len(near)),
        "radius_m": radius,
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


# --------------- 笛卡尔直线插补（沿面横移用） ---------------


@router.post("/plan_cartesian")
async def reach_plan_cartesian(body: dict):
    """指尖沿直线平移的轨迹：把总位移切成小步，逐步 IK（前一步做种子），
    TCP 全程钉在直线上——不会像关节空间插值那样中途下沉再抬起。

    Body: {"start_joints": named, "direction_root": [x,y,z], "distance_m": float,
           "step_m": 0.01}
    """
    if state.ik_solver is None:
        return JSONResponse({"ok": False, "error": "IK 求解器未注入"}, status_code=409)
    from core.types import IKRequest, Pose

    try:
        start_named = {str(k): float(v) for k, v in dict(body["start_joints"]).items()}
        direction = np.asarray(body["direction_root"], dtype=float).reshape(3)
        distance = float(body["distance_m"])
        step = abs(float(body.get("step_m", 0.01)))
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"参数非法: {exc}"}, status_code=400)
    norm = float(np.linalg.norm(direction))
    if norm < 1e-9 or abs(distance) < 1e-6:
        return JSONResponse({"ok": False, "error": "方向/距离为零"}, status_code=400)
    direction /= norm

    model = state.robot_model
    tcp_offset = Pose(xyz=list(state.p_tool))
    p0 = np.asarray(model.tcp_pose(start_named, state.chain_id, tcp_offset).xyz)
    n_steps = max(2, int(np.ceil(abs(distance) / step)))

    waypoints = [{"index": 0, "named_joints": start_named,
                  "tcp_pose": {"xyz": p0.tolist(), "rpy": [0.0, 0.0, 0.0]}}]
    q_prev = start_named
    max_err = 0.0
    for i in range(1, n_steps + 1):
        target = p0 + direction * distance * (i / n_steps)
        res = state.ik_solver.solve(IKRequest(
            chain_id=state.chain_id,
            current_joints=q_prev,
            target_pose=Pose(xyz=target.tolist()),
            tcp_offset=tcp_offset,
            base_link=model.base_link(state.chain_id),
            end_link=model.end_link(state.chain_id),
            joint_names=state.joint_names,
            seed=q_prev,
            solver_options={"solve_orientation": False, "tolerance_mm": 3.0},
        ))
        if not res.success:
            return JSONResponse(
                {"ok": False,
                 "error": f"第 {i}/{n_steps} 步 IK 未收敛（{res.error_mm:.1f} mm），"
                          "直线可能超出可达范围"},
                status_code=422)
        max_err = max(max_err, float(res.error_mm))
        q_prev = res.named_target_joints
        waypoints.append({"index": i, "named_joints": q_prev,
                          "tcp_pose": res.tcp_pose.to_dict()})

    collision = None
    if state.collision_checker is not None:
        checks = state.collision_checker.check_trajectory(
            waypoints, state.chain_id, tcp_offset)
        collision = state.collision_checker.summarize_checks(checks)
        for wp, check in zip(waypoints, checks):
            wp["collision"] = {"status": check["status"],
                               "status_label": check["status_label"],
                               "min_distance_mm": check["min_distance_mm"]}

    return {"ok": True, "waypoints": waypoints, "collision": collision,
            "max_ik_error_mm": max_err, "steps": n_steps}


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
        "torso_diag": state.torso_diag,
    }


@router.get("/diagnostics")
async def reach_diagnostics():
    """现场排查用：重力前馈在出多大力、躯干相对取点时刻漂了多少。"""
    ctl = state.controller
    arm: dict[str, Any] = {"armed": ctl is not None}
    if ctl is not None:
        st = ctl.status()
        arm.update({k: st.get(k) for k in
                    ("kp", "kd", "kp_wrist", "kd_wrist", "grav_alpha", "payload_kg",
                     "grav_in_float", "use_imu_gravity", "tau_grav_nm", "tau_push_nm",
                     "joint_names", "cmd_rad", "measured_rad", "desired_rad")})
        if st.get("cmd_rad") and st.get("measured_rad"):
            gap = np.asarray(st["cmd_rad"]) - np.asarray(st["measured_rad"])
            # 跟随误差就是"下垂"的直接度量：重力前馈生效后应当从几度掉到零点几度
            arm["follow_error_deg"] = [round(math.degrees(v), 2) for v in gap]
            arm["follow_error_max_deg"] = round(math.degrees(float(np.max(np.abs(gap)))), 2)
    now = _read_torso()
    return {
        "arm": arm,
        "torso_now": now,
        "torso_at_pick": state.pick_torso,
        "torso_drift": _torso_drift(state.pick_torso, now),
        "last_exec_drift": state.torso_diag,
    }


@router.get("/exec_status")
async def reach_exec_status():
    return _exec_status()


def _position_jacobian(named: dict[str, float]) -> np.ndarray:
    """TCP 位置对该链关节的数值雅可比（3×n，根系）。"""
    from core.types import Pose

    tcp_offset = Pose(xyz=list(state.p_tool))
    model = state.robot_model
    p0 = np.asarray(model.tcp_pose(named, state.chain_id, tcp_offset).xyz)
    J = np.zeros((3, len(state.joint_names)))
    eps = 1e-4
    for k, name in enumerate(state.joint_names):
        q = dict(named)
        q[name] = q.get(name, 0.0) + eps
        J[:, k] = (np.asarray(model.tcp_pose(q, state.chain_id, tcp_offset).xyz) - p0) / eps
    return J


@router.post("/execute")
async def reach_execute(body: dict):
    """执行已规划的关节轨迹（真机运动！前端须先经人确认）。

    Body: {"waypoints": [named_joints, ...], "duration": float,
           "max_speed_rad_s": float?, "label": str?,
           "push": {"direction_root": [x,y,z], "force_n": float}?}

    label（可选）：段名，只用于 reach_logs 里区分主轨迹/横移/收回。

    max_speed_rad_s（可选）：本次执行的关节限速档（默认 0.2，收回段等
    低精度动作可以给 0.4 提速），不会超过 --arm-max-speed 天花板。

    push（可选）：执行期间在 TCP 上沿指定方向叠加前馈力（τ=JᵀF）。
    纯位置控制的侧向刚度很低（~300 N/m），贴着旋钮也使不上力；
    有了前馈力矩，接触后能持续出力把旋钮拨过去。
    """
    if state.controller is None:
        return JSONResponse(
            {"ok": False, "error": "手臂未接管，请先在页面上点「接管手臂」"},
            status_code=409)
    waypoints = body.get("waypoints") or []
    duration = float(body.get("duration") or 4.0)
    speed = float(np.clip(float(body.get("max_speed_rad_s") or 0.2), 0.05, 0.5))
    label = str(body.get("label") or "reach")[:32]
    if len(waypoints) < 2:
        return JSONResponse({"ok": False, "error": "轨迹至少要有 2 个路点"}, status_code=400)
    try:
        q_list = [np.asarray([float(wp[name]) for name in state.joint_names], dtype=float)
                  for wp in waypoints]
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"路点关节缺失/非法: {exc}"}, status_code=400)

    push_tau = None
    push = body.get("push")
    if push:
        try:
            direction = np.asarray(push["direction_root"], dtype=float).reshape(3)
            direction /= max(float(np.linalg.norm(direction)), 1e-9)
            force = min(abs(float(push["force_n"])), 40.0)
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": f"push 参数非法: {exc}"}, status_code=400)
        if force > 1e-3:
            J = _position_jacobian(dict(zip(state.joint_names, q_list[0])))
            push_tau = J.T @ (direction * force)

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
            target=_exec_loop, args=(q_list, duration, push_tau, speed, label), daemon=True)
        state.exec_thread.start()
    return {"ok": True, **_exec_status()}


def _tcp_position(q) -> list[float] | None:
    """一组关节角对应的指尖（TCP）位置，根系，米。"""
    try:
        from core.types import Pose

        named = dict(zip(state.joint_names, [float(v) for v in q]))
        pose = state.robot_model.tcp_pose(named, state.chain_id, Pose(xyz=list(state.p_tool)))
        return [float(v) for v in pose.xyz]
    except Exception:
        return None


def _log_exec(kind: str, result: str, q_target, *, sag=None,
              duration=None, speed=None, pushing: bool = False, push_tau=None,
              trace=None) -> None:
    """每段真机动作落一行 JSONL：reach_logs/reach_YYYYMMDD.jsonl。

    调参靠的是横向对比（改了 α / payload / kp 之后到底好了多少），
    而页面上的实时数字每次重新取点就被冲掉了，留不下证据。这里把一次
    执行的"参数—误差—躯干姿态"三件套整段存下来，事后能直接拉出来比。
    误差拆成三段，各自对应完全不同的病因：
      ik_mm      规划本身的残差（IK 没收敛到点上）
      track_mm   指令关节角 vs 实测关节角（下垂/摩擦，重力前馈治的就是它）
      total_mm   取点目标 vs 实际指尖（前两者叠加，加上躯干漂移和标定误差）
    """
    if state.log_dir is None:
        return
    try:
        ctl = state.controller
        st = ctl.status() if ctl is not None else {}
        target = np.asarray(q_target, dtype=float)
        measured = np.asarray(st.get("measured_rad") or [], dtype=float)
        cmd = np.asarray(st.get("cmd_rad") or [], dtype=float)
        deg = np.degrees

        rec: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session": state.session_id,
            "segment": kind,
            "result": result,
            "params": {
                "duration_s": duration,
                "max_speed_rad_s": speed,
                "pushing": pushing,
                "grav_alpha": st.get("grav_alpha"),
                "payload_kg": st.get("payload_kg"),
                "kp": st.get("kp"), "kd": st.get("kd"),
                "kp_wrist": st.get("kp_wrist"), "kd_wrist": st.get("kd_wrist"),
                "use_imu_gravity": st.get("use_imu_gravity"),
            },
            "joint_names": state.joint_names,
            "target_rad": target.tolist(),
            "cmd_rad": cmd.tolist(),
            "measured_rad": measured.tolist(),
            "tau_grav_nm": st.get("tau_grav_nm"),
            # 记的是本段申请的峰值推力，不是当前值：日志在撤力之后才写，
            # 那时 status 里的推力已经归零了，记下来全是 0 没有意义
            "tau_push_peak_nm": (None if push_tau is None
                                 else [round(float(v), 3) for v in np.asarray(push_tau)]),
            "settle_residual_rad": sag,
        }
        if measured.size == target.size and measured.size:
            rec["reach_error_deg"] = [round(v, 3) for v in deg(target - measured)]
            rec["reach_error_max_deg"] = round(float(np.max(np.abs(deg(target - measured)))), 3)
        if cmd.size == measured.size and cmd.size:
            # 跟随误差 = 指令 - 实测，纯 PD 下这就是重力压出来的下垂量
            rec["follow_error_deg"] = [round(v, 3) for v in deg(cmd - measured)]
            rec["follow_error_max_deg"] = round(float(np.max(np.abs(deg(cmd - measured)))), 3)

        tcp_target = _tcp_position(target)
        tcp_actual = _tcp_position(measured) if measured.size == target.size else None
        rec["tcp"] = {
            "pick_target_root": state.pick_target_root,
            "planned_root": tcp_target,
            "actual_root": tcp_actual,
        }

        def mm(a, b):
            if a is None or b is None:
                return None
            return round(float(np.linalg.norm(np.asarray(a) - np.asarray(b))) * 1000.0, 1)

        torso_end = _read_torso()
        rec["tcp"]["ik_mm"] = mm(state.pick_target_root, tcp_target)
        rec["tcp"]["track_mm"] = mm(tcp_target, tcp_actual)
        rec["tcp"]["total_mm"] = mm(state.pick_target_root, tcp_actual)
        # 验收指标：对"按结束时躯干姿态折算的真实开关位置"的误差。
        # 开了重瞄后 total_mm 会一直 ≈ 漂移量（手臂故意不去旧目标了），
        # 看这个才知道到底打没打中开关。
        try:
            R0 = _torso_rotation(state.pick_torso) if state.pick_torso else None
            R1 = _torso_rotation(torso_end) if torso_end else None
            if (R0 is not None and R1 is not None
                    and state.pick_target_torso and state.pick_target_root):
                p0 = np.asarray(state.pick_target_torso, dtype=float)
                drifted = (np.asarray(state.pick_target_root, dtype=float)
                           + ((R1.T @ R0) @ p0 - p0))
                rec["tcp"]["drifted_target_root"] = [round(float(v), 4) for v in drifted]
                rec["tcp"]["total_vs_drifted_mm"] = mm(drifted.tolist(), tcp_actual)
        except Exception:
            pass

        rec["pick"] = {"pixel": state.pick_pixel, "target_torso": state.pick_target_torso}
        rec["torso_at_pick"] = state.pick_torso
        rec["torso_at_end"] = torso_end
        rec["torso_drift"] = state.torso_diag
        # 快照拷贝：采样线程可能还在往里 append
        rec["torso_trace"] = [dict(s) for s in list(trace)] if trace else None

        state.log_dir.mkdir(parents=True, exist_ok=True)
        path = state.log_dir / f"reach_{datetime.now():%Y%m%d}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass    # 记日志永远不能把执行搞挂


def _finish_torso_diag() -> str:
    """执行结束时结算躯干漂移，写进 state 供页面展示，并返回一句话摘要。"""
    diag = _torso_drift(state.pick_torso, _read_torso())
    state.torso_diag = diag
    if not diag:
        return ""
    shift = diag.get("target_shift_mm")
    if shift is None:
        return ""
    note = f"；躯干较取点时转了 {diag.get('torso_rotation_deg', 0):.1f}°，目标漂移 {shift:.0f} mm"
    return note + ("（超过 10mm，够不着多半是躯干在动而不是手臂没到位）"
                   if shift > 10 else "")


def _start_torso_trace(ctl) -> tuple[list, threading.Event]:
    """执行期间 5Hz 采样躯干姿态 + 手臂跟随误差，带阶段标签。

    用来钉死"身体后仰到底发生在哪个阶段"：是轨迹回放时（手臂大幅抡出去，
    平衡控制器在配平）还是收尾阶段（手臂只挪一两度）。每段动作的两个
    快照分不清这件事，只有时间序列能。
    """
    samples: list[dict] = []
    stop = threading.Event()

    def run() -> None:
        t0 = time.monotonic()
        while not stop.is_set() and len(samples) < 600:
            row: dict[str, Any] = {"t": round(time.monotonic() - t0, 2),
                                   "phase": state.exec_phase}
            torso = _read_torso()
            if torso:
                row["waist_deg"] = [round(math.degrees(v), 3) for v in torso["waist_rad"]]
                row["imu_rpy_deg"] = [round(math.degrees(v), 3) for v in torso["imu_rpy"]]
            try:
                st = ctl.status()
                gap = np.asarray(st["cmd_rad"]) - np.asarray(st["measured_rad"])
                row["follow_sp_deg"] = round(math.degrees(float(gap[0])), 3)   # 肩俯仰
                row["follow_max_deg"] = round(math.degrees(float(np.max(np.abs(gap)))), 3)
            except Exception:
                pass
            samples.append(row)
            stop.wait(0.2)

    threading.Thread(target=run, name="reach-torso-trace", daemon=True).start()
    return samples, stop


def _exec_loop(q_list: list[np.ndarray], duration: float,
               push_tau: np.ndarray | None = None, speed: float = 0.2,
               label: str = "reach") -> None:
    ctl = state.controller
    trace, trace_stop = _start_torso_trace(ctl)
    log = dict(duration=duration, speed=speed, pushing=push_tau is not None,
               push_tau=push_tau, trace=trace)
    try:
        state.exec_phase = "traj"
        ctl.enable_jog()
        # 分段限速：普通段默认 0.2 慢而稳；带推力的快拨段放行到 0.4；
        # 调用方也可以按段指定（如收回段 0.4），都不超 --arm-max-speed 天花板
        if hasattr(ctl, "set_max_speed"):
            ctl.set_max_speed(max(0.4, speed) if push_tau is not None else speed)
        n = len(q_list)
        # 时长下限：限速滑动（矢量同步）跑完全程所需时间。短于它路点节拍会
        # 一直超前于指令，falling-behind 的关节仍会扭曲路径，所以自动拉长。
        travel = sum(float(np.max(np.abs(b - a))) for a, b in zip(q_list, q_list[1:]))
        min_duration = travel / max(ctl.max_speed, 1e-6) * 1.1
        if duration < min_duration:
            duration = min_duration
            state.exec_message = f"时长过短，按限速拉长到 {duration:.1f}s"
        dt = duration / max(n - 1, 1)
        t0 = time.monotonic()
        for i, q in enumerate(q_list):
            if state.exec_cancel.is_set():
                ctl.disable_jog()
                state.exec_message = "已中止（保持当前位置）"
                _log_exec(label, "cancelled", q_list[-1], **log)
                return
            ctl.set_target(q)
            if push_tau is not None:
                # 前馈力矩渐入（前 30% 路点线性加满），避免突加力矩的抖动
                scale = min(1.0, (i + 1) / max(1, round(0.3 * n)))
                ctl.set_tau_ff(push_tau * scale)
            state.exec_progress = i / (n - 1)
            target_t = t0 + (i + 1) * dt
            while True:
                remaining = target_t - time.monotonic()
                if remaining <= 0 or state.exec_cancel.is_set():
                    break
                time.sleep(min(remaining, 0.05))
        # 最终目标已下发；等限速滑动真正到位再冻结（时长偏短时控制器会滞后）
        state.exec_message = "收敛中"
        state.exec_phase = "converge"
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not state.exec_cancel.is_set():
            status = ctl.status()
            gap = float(np.max(np.abs(
                np.asarray(status["desired_rad"]) - np.asarray(status["cmd_rad"]))))
            if gap < 1e-3:
                break
            time.sleep(0.1)

        # 推力模式：位置到不到位没有意义（被旋钮/表面顶着），收敛后持续
        # 顶 1.5s 把旋钮拨到底，然后撤力刚性保持。
        if push_tau is not None:
            if not state.exec_cancel.is_set():
                state.exec_message = "持续出力中"
                state.exec_phase = "push_hold"
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline and not state.exec_cancel.is_set():
                    time.sleep(0.05)
            # 撤力渐出：顶着的手臂/身体像压紧的弹簧，力矩瞬间清零会"啪"地
            # 向反方向回弹。这里力矩 0.65s 线性泄掉，同时把位置指令收回到
            # 实测位（限速滑动本身是平滑的），存的形变缓慢释放。
            state.exec_message = "撤力中"
            state.exec_phase = "release"
            try:
                ctl.set_target(ctl.read_measured())
            except Exception:
                pass
            for s in np.linspace(1.0, 0.0, 13):
                if state.exec_cancel.is_set():
                    break
                ctl.set_tau_ff(push_tau * float(s))
                time.sleep(0.05)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not state.exec_cancel.is_set():
                status = ctl.status()
                if float(np.max(np.abs(np.asarray(status["desired_rad"])
                                       - np.asarray(status["cmd_rad"])))) < 1e-3:
                    break
                time.sleep(0.05)
            ctl.disable_jog()   # 兜底清零主动出力
            state.exec_progress = 1.0
            cancelled = state.exec_cancel.is_set()
            state.exec_message = ("已中止（保持当前位置）" if cancelled
                                  else f"完成（推力段结束，已撤力保持{_finish_torso_diag()}）")
            _log_exec(label, "cancelled" if cancelled else "done", q_list[-1], **log)
            return

        sag = None
        target = q_list[-1]
        if not state.exec_cancel.is_set():
            state.exec_phase = "settle"
            # 躯干漂移不再在这里主动补偿（曾有"重瞄"逻辑：到位后按躯干姿态
            # 变化重解 IK 再挪 2~3cm，观感是到位后突然跳一下）。现在漂移交给
            # 分段模式的「再次选点」用当前相机实测修正，这里只测量、写日志。
            # 外环积分同样已移除：重力下垂由重力前馈扛。
            # 这里只等指令送达、给电机 ~0.3s 贴上来，然后测一次落点残差写日志。
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not state.exec_cancel.is_set():
                status = ctl.status()
                delivered = float(np.max(np.abs(
                    np.asarray(status["desired_rad"]) - np.asarray(status["cmd_rad"])))) < 1e-3
                if delivered:
                    break
                time.sleep(0.08)
            if not state.exec_cancel.is_set():
                time.sleep(0.3)
                status = ctl.status()
                measured = np.asarray(status["measured_rad"] or ctl.read_measured().tolist())
                sag = float(np.max(np.abs(target - measured)))

        ctl.disable_jog()
        state.exec_progress = 1.0
        sag_note = f"，落点残差 {sag:.3f} rad" if sag is not None else ""
        cancelled = state.exec_cancel.is_set()
        state.exec_message = ("已中止（保持当前位置）" if cancelled
                              else f"完成（刚性保持{sag_note}{_finish_torso_diag()}）")
        _log_exec(label, "cancelled" if cancelled else "done", target,
                  sag=sag, **log)
    except Exception as exc:
        try:
            ctl.stop()
        except Exception:
            pass
        state.exec_message = f"执行出错已停止: {exc}"
        _log_exec(label, f"error: {exc}", q_list[-1], **log)
    finally:
        trace_stop.set()
        state.exec_phase = "idle"
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
