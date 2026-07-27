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
import multiprocessing as mp
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
        self.loco_client = None            # 高层 loco RPC（原地转身用），懒创建
        self.loco_available = False        # 有 DDS（非 --no-robot）才可用
        # 一键对中（yaw 闭环伺服）
        self.align_thread: threading.Thread | None = None
        self.align_cancel = threading.Event()
        self.align_running = False
        self.align_message = ""
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
        self.obstacles: np.ndarray | None = None   # 扫描体素中心（URDF 根系）
        self.wall: np.ndarray | None = None        # 拟合墙面补全体素（含视野下方）
        self.wall_plane: dict | None = None        # 拟合墙面几何（前端画红色平面用）
        self.obstacle_voxel = 0.05
        self.target_exclusion_m = 0.15
        self.plane: dict | None = None             # 取点时拟合的目标表面平面（根系）
        self.ik_solver = None                      # 笛卡尔直线插补用的 IK 求解器
        self.waypoints_dir: Path | None = None     # 中间路点落盘目录（每个路点一个 json）
        self.sequences_dir: Path | None = None     # 动作序列落盘目录（路点文件名的有序列表）
        self.sidesteps_dir: Path | None = None     # 横移录制落盘目录（免 IK 回放）
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
              joints_reader=None, torso_reader=None, tool_out_mm: float = 0.0) -> None:
    """由 reach_server 调用。calib_path 是 handeye3d_result.json。

    tool_out_mm: 标定的 p_tool 点（当时选在手指上，离真正指尖还差一点）
    沿法兰盘法线向外的附加偏移。法兰盘平面 = 手掌安装面 = 腕系 y-z 平面，
    其法线严格为腕系 +x，"向外" = +x（远离法兰、指向指尖方向）。
    """
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
    p_tool = [float(v) for v in calib["p_tool_wrist_m"]]
    p_tool[0] += float(tool_out_mm) / 1000.0   # 沿法兰法线（腕系 +x）向外
    state.p_tool = p_tool
    state.calib_meta = {
        "path": str(calib_path),
        "base_link": base_link,
        "solved_at": calib.get("solved_at"),
        "rms_mm": calib.get("residual_mm", {}).get("rms"),
        "num_samples": calib.get("num_samples"),
        "tool_out_mm": float(tool_out_mm),
    }
    state.arm_factory = arm_factory
    state.provider_reader = joints_reader
    state.torso_reader = torso_reader
    state.loco_available = joints_reader is not None   # 有 DDS 连接才谈得上转身
    state.base_link = base_link
    state.joint_names = robot_model.joint_names(chain_id)
    state.robot_model = robot_model
    state.collision_checker = collision_checker
    state.ik_solver = ik_solver
    state.waypoints_dir = Path(__file__).resolve().parent.parent / "reach_waypoints"
    state.sequences_dir = Path(__file__).resolve().parent.parent / "reach_sequences"
    state.sidesteps_dir = Path(__file__).resolve().parent.parent / "reach_sidesteps"
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
def reach_status():
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
def reach_stream():
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
def reach_pick(body: dict):
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


# --------------- 垂直度观测 + 腰 yaw 点动（perp.html 调试页） ---------------


@router.get("/perpendicular")
def reach_perpendicular(dmin: float = 0.3, dmax: float = 1.0):
    """用 [dmin, dmax] 深度范围内的点拟合柜面平面，给出垂直度指标。

    深度不在该范围内的点视为异常（地面、远处背景、手臂等），不参与拟合。
    yaw_err_deg > 0 表示法线偏向画面右侧（柜面左边更远）；两个角都为 0
    即相机光轴与柜面严格垂直。
    """
    if not state.enabled:
        return JSONResponse({"ok": False, "error": "reach 未启用"}, status_code=409)
    out = _fit_view_plane(dmin, dmax)
    out["torso"] = _read_torso()
    out["turn_available"] = state.loco_available
    out["align"] = {"running": state.align_running, "message": state.align_message}
    return out


def _fit_view_plane(dmin: float, dmax: float) -> dict:
    """整幅深度图（限定深度范围）拟合平面，返回垂直度指标。失败时 ok=False。"""
    snap = state.camera.depth_snapshot()
    if snap is None:
        return {"ok": False, "error": "拿不到深度帧"}
    depth_mm, (fx, fy, cx, cy) = snap
    h, w = depth_mm.shape
    stride = max(1, int(round(max(h, w) / 240)))
    d = depth_mm[::stride, ::stride].astype(float) / 1000.0
    vs, us = np.mgrid[0:h:stride, 0:w:stride]
    measured = d > 0.05                       # 有回波的像素（0 = 无效）
    valid = measured & (d > dmin) & (d < dmax)
    n_meas = int(measured.sum())
    if valid.sum() < 200:
        return {"ok": False, "error": f"深度在 {dmin:.2f}~{dmax:.2f} m 内的点太少"
                                      f"（{int(valid.sum())} 个），请靠近/对准柜面"}
    pts = np.stack([(us[valid] - cx) * d[valid] / fx,
                    (vs[valid] - cy) * d[valid] / fy,
                    d[valid]], axis=1)

    def fit(p):
        c = p.mean(axis=0)
        q = p - c
        _, _, vt = np.linalg.svd(q, full_matrices=False)
        n = vt[2]
        return c, n, float(np.sqrt(np.mean((q @ n) ** 2)))

    # 两遍拟合：第一遍全量，第二遍剔除 3σ 残差外点（柜门把手、边缘飞点）
    center, n, rms = fit(pts)
    resid = np.abs((pts - center) @ n)
    inlier = resid < max(0.008, 3.0 * rms)
    if inlier.sum() >= 200:
        center, n, rms = fit(pts[inlier])
    if float(np.dot(n, -center)) < 0:
        n = -n                                # 法线指向相机一侧

    # 相机系：x 右、y 下、z 前。垂直时 n = (0,0,-1)
    yaw_err = math.degrees(math.atan2(float(n[0]), float(-n[2])))
    pitch_err = math.degrees(math.atan2(float(n[1]), float(-n[2])))
    tilt = math.degrees(math.acos(float(np.clip(-n[2], -1.0, 1.0))))
    n_root = (state.T_cam2root[:3, :3] @ n).tolist()

    return {
        "ok": True,
        "yaw_err_deg": yaw_err,
        "pitch_err_deg": pitch_err,
        "tilt_deg": tilt,
        "distance_m": float(abs(np.dot(n, center))),
        "normal_cam": n.tolist(),
        "normal_root": n_root,
        "rms_mm": rms * 1000.0,
        "points_used": int(inlier.sum()),
        "in_range_ratio": float(valid.sum()) / max(1, n_meas),
        "dmin": dmin,
        "dmax": dmax,
    }


TURN_RATE_DEG_S = 6.0      # 原地转身角速度
TURN_MAX_DEG = 10.0        # 单次点动上限
# 按住键盘连续转身：每次心跳发一个这么长的速度脉冲，前端 ~0.3s 心跳一次，
# 脉冲之间相互覆盖 → 连续转；心跳断了（松键/断网/页面崩）固件转完残余
# 脉冲即自动停 —— 等价于摇杆的"松手即停"死人开关。
TURN_HOLD_PULSE_S = 0.8
TURN_HOLD_RATE_DEG_S = 12.0        # 按住模式默认转速（前端可传 rate_deg_s 覆盖）
TURN_HOLD_RATE_RANGE = (2.0, 30.0)  # 前端可调范围；点动/对中仍用上面验证过的 6°/s


def _get_loco_client():
    """高层 loco RPC 客户端（懒创建）。DDS 在服务启动时已由只读订阅初始化。"""
    if state.loco_client is None:
        from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient

        c = LocoClient()
        if hasattr(c, "SetTimeout"):
            c.SetTimeout(3.0)
        c.Init()
        state.loco_client = c
    return state.loco_client


@router.post("/turn")
def reach_turn(body: dict):
    """原地转身点动（真机！全身动作）。Body: {"delta_deg": ±2} 或 {"stop": true}。

    H2 的 rt/arm_sdk 混合通道只覆盖双臂（15~28），腰电机指令会被固件忽略
    （已真机验证）；直接发 rt/lowcmd 又必须释放本体运控、机器人会失去平衡。
    所以对准柜面的 yaw 调整走高层 SetVelocity：让本体运控自己用腿原地转，
    平衡由它负责，与 arm_sdk 手臂控制可以共存（官方 VR 遥操即此组合）。
    注意：转身会带动整条手臂，请先把手收回再调。
    """
    if not state.loco_available:
        return JSONResponse({"ok": False, "error": "无 DDS 连接（--no-robot 模式）"},
                            status_code=409)
    if state.align_running and not body.get("stop"):
        return JSONResponse({"ok": False, "error": "一键对中进行中，先停止它"},
                            status_code=409)
    try:
        loco = _get_loco_client()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"loco 客户端初始化失败: {exc}"},
                            status_code=502)

    if body.get("stop"):
        try:
            loco.StopMove()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"停止失败: {exc}"}, status_code=502)
        return {"ok": True, "stopped": True}

    # 按住模式：{"hold_dir": 1|-1}，正=左转。每次调用发一个短速度脉冲，
    # 由前端心跳维持连续性（见 TURN_HOLD_PULSE_S 注释）。
    hold_dir = body.get("hold_dir")
    if hold_dir is not None:
        try:
            direction = 1.0 if float(hold_dir) > 0 else -1.0
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "hold_dir 需为 ±1"},
                                status_code=400)
        try:
            rate = float(body.get("rate_deg_s") or TURN_HOLD_RATE_DEG_S)
        except (TypeError, ValueError):
            rate = TURN_HOLD_RATE_DEG_S
        rate = float(np.clip(rate, *TURN_HOLD_RATE_RANGE))
        omega = math.radians(rate) * direction
        try:
            code = loco.SetVelocity(0.0, 0.0, omega, TURN_HOLD_PULSE_S)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"SetVelocity 失败: {exc}"},
                                status_code=502)
        if code not in (0, None, RPC_TIMEOUT_CODE):
            return JSONResponse({"ok": False, "error": f"SetVelocity 返回码 {code}"},
                                status_code=502)
        return {"ok": True, "hold_dir": int(direction),
                "omega_deg_s": math.degrees(omega), "pulse_s": TURN_HOLD_PULSE_S,
                **({"warning": "RPC 应答超时，指令可能已执行"}
                   if code == RPC_TIMEOUT_CODE else {})}

    try:
        delta = float(body["delta_deg"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "需要 delta_deg（度，正=左转）"},
                            status_code=400)
    delta = float(np.clip(delta, -TURN_MAX_DEG, TURN_MAX_DEG))
    if abs(delta) < 0.05:
        return {"ok": True, "delta_deg": 0.0, "duration_s": 0.0}
    omega = math.radians(TURN_RATE_DEG_S) * (1.0 if delta > 0 else -1.0)
    duration = abs(math.radians(delta)) / abs(omega)
    try:
        code = loco.SetVelocity(0.0, 0.0, omega, duration)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"SetVelocity 失败: {exc}"},
                            status_code=502)
    if code == RPC_TIMEOUT_CODE:
        # 应答超时 ≠ 没执行：运控忙时常见，指令多半已生效
        return {"ok": True, "delta_deg": delta, "omega_deg_s": math.degrees(omega),
                "duration_s": duration, "warning": "RPC 应答超时，指令可能已执行"}
    if code not in (0, None):
        return JSONResponse({"ok": False, "error": f"SetVelocity 返回码 {code}"},
                            status_code=502)
    return {"ok": True, "delta_deg": delta,
            "omega_deg_s": math.degrees(omega), "duration_s": duration}


# --------------- 一键对中（yaw 闭环伺服） ---------------

ALIGN_TOL_STRICT_DEG = 0.35  # 手臂收回时的收敛阈值
ALIGN_TOL_FALLBACK_DEG = 0.4  # 步数用尽时的兜底：残差在此内按"基本对中"收尾
ALIGN_TOL_RAISED_DEG = 2.8   # 手臂前伸时：运控持续配平、读数呼吸式波动，追不到 0.8
ARM_RAISED_TCP_X = 0.25      # TCP 前伸超过这个距离（米，根系）视为"手抬起来了"
ALIGN_MAX_STEPS = 15
# 脉冲幅度只用真机验证过能可靠执行的两档（人手点按收敛就是这么干的）。
# 连续伺服两次翻车的教训：转动中相机测量滞后必穿靶；腰编码器做代理，
# 停车后运控又会在腰/腿之间重分配旋转，读数对不上相机。所以：
# 测量只在静止时做，动作只用定长脉冲，简单且和人手一样快。
PULSE_BIG_DEG = 2.0        # |偏差| ≥ 1.5° 用大脉冲
PULSE_SMALL_DEG = 0.5      # 其余用小脉冲
PULSE_BIG_BELOW = 1.5
RPC_TIMEOUT_CODE = 3104    # unitree rpc：应答超时（指令多半已执行，不算失败）


def _arm_raised() -> bool:
    """手臂是否前伸（TCP 在根系向前超过阈值）。读不到关节时按未抬处理。"""
    try:
        tcp = _tcp_position([float(v) for v in _read_joints()])
        return tcp is not None and tcp[0] > ARM_RAISED_TCP_X
    except Exception:
        return False
ALIGN_SETTLE_S = 1.0       # 每步转完后等运控稳定再测


def _align_log(entry: dict) -> None:
    """对中过程逐步落盘：reach_logs/align_<日期>.jsonl，事后分析用。"""
    try:
        state.log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(timespec="milliseconds"),
                 "session": state.session_id, **entry}
        torso = _read_torso()
        if torso and torso.get("waist_rad"):
            entry["waist_deg"] = [round(math.degrees(v), 3)
                                  for v in torso["waist_rad"]]
        path = state.log_dir / f"align_{datetime.now():%Y%m%d}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _align_loop(tol: float, dmin: float, dmax: float) -> None:
    """人手同款打法：静止测偏差 → 定长脉冲（0.5° 或 2°）→ 等稳 → 再测。

    修正方向：真机实测 yaw_err > 0（法线偏画面右）时左转（正角）是对的。
    保留自适应反号兜底：某步之后偏差反而变大就翻方向。
    每步的测量与动作都写入 reach_logs/align_<日期>.jsonl。
    """
    loco = _get_loco_client()
    sign = 1.0
    prev_err: float | None = None
    _align_log({"event": "start", "tol_deg": tol, "dmin": dmin, "dmax": dmax})
    try:
        for step in range(1, ALIGN_MAX_STEPS + 1):
            if state.align_cancel.is_set():
                state.align_message = "已中止"
                _align_log({"event": "cancelled", "step": step})
                return
            fitres = _fit_view_plane(dmin, dmax)
            if not fitres.get("ok"):
                state.align_message = f"对中失败：{fitres.get('error')}"
                _align_log({"event": "fit_fail", "step": step,
                            "error": fitres.get("error")})
                return
            err = float(fitres["yaw_err_deg"])
            _align_log({"event": "measure", "step": step,
                        "yaw_err_deg": round(err, 3),
                        "pitch_err_deg": round(float(fitres["pitch_err_deg"]), 3),
                        "points": fitres.get("points_used")})
            if abs(err) <= tol:
                state.align_message = f"对中完成：yaw 偏差 {err:+.2f}°（{step - 1} 步）"
                _align_log({"event": "done", "step": step, "yaw_err_deg": round(err, 3)})
                return
            if prev_err is not None and abs(err) > abs(prev_err) + 0.3:
                sign = -sign     # 上一步把偏差转大了 → 方向反了
                _align_log({"event": "sign_flip", "step": step})
            prev_err = err

            size = PULSE_BIG_DEG if abs(err) >= PULSE_BIG_BELOW else PULSE_SMALL_DEG
            delta = size * (1.0 if sign * err > 0 else -1.0)
            state.align_message = f"第 {step} 步：偏差 {err:+.2f}° → 脉冲 {delta:+.1f}°"
            omega = math.radians(TURN_RATE_DEG_S) * (1.0 if delta > 0 else -1.0)
            duration = abs(math.radians(delta)) / abs(omega)
            code = loco.SetVelocity(0.0, 0.0, omega, duration)
            _align_log({"event": "pulse", "step": step, "delta_deg": delta,
                        "duration_s": round(duration, 3), "rpc_code": code})
            if code == RPC_TIMEOUT_CODE:
                # 应答超时 ≠ 没执行：运控忙（如手臂前伸配平）时常见，
                # 指令多半已生效，照常等稳再测，让闭环自己判断
                state.align_message += "（RPC 应答超时，按已执行继续）"
            elif code not in (0, None):
                state.align_message = f"对中失败：SetVelocity 返回码 {code}"
                return
            if state.align_cancel.wait(duration + ALIGN_SETTLE_S):
                state.align_message = "已中止"
                _align_log({"event": "cancelled", "step": step})
                return
        if prev_err is not None and abs(prev_err) <= max(ALIGN_TOL_FALLBACK_DEG, tol):
            state.align_message = (f"基本对中：偏差 {prev_err:+.2f}°"
                                   f"（未达 {tol}°，但已在兜底 "
                                   f"{ALIGN_TOL_FALLBACK_DEG}° 内）")
            _align_log({"event": "done_fallback", "yaw_err_deg": round(prev_err, 3)})
        else:
            state.align_message = (f"未收敛：{ALIGN_MAX_STEPS} 步后偏差仍 "
                                   f"{prev_err:+.2f}°（阈值 {tol}°）")
            _align_log({"event": "give_up",
                        "yaw_err_deg": None if prev_err is None else round(prev_err, 3)})
    except Exception as exc:
        state.align_message = f"对中异常：{exc}"
        _align_log({"event": "exception", "error": str(exc)})
    finally:
        try:
            loco.StopMove()
        except Exception:
            pass
        state.align_running = False


@router.post("/align_yaw")
def reach_align_yaw(body: dict):
    """一键对中（真机！）。Body: {"start": true, "tol_deg"?, "dmin"?, "dmax"?}
    或 {"stop": true}。闭环转身直到相机光轴与柜面法线的 yaw 偏差进入阈值。"""
    if body.get("stop"):
        state.align_cancel.set()
        try:
            if state.loco_client is not None:
                state.loco_client.StopMove()
        except Exception:
            pass
        return {"ok": True, "stopped": True}

    if not state.loco_available:
        return JSONResponse({"ok": False, "error": "无 DDS 连接（--no-robot 模式）"},
                            status_code=409)
    if state.align_running:
        return JSONResponse({"ok": False, "error": "对中已在进行中"}, status_code=409)
    if state.exec_running:
        return JSONResponse({"ok": False, "error": "手臂轨迹执行中，禁止转身"},
                            status_code=409)
    try:
        _get_loco_client()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"loco 客户端初始化失败: {exc}"},
                            status_code=502)

    if "tol_deg" in body:
        tol = float(body["tol_deg"])
        tol_note = f"指定阈值 {tol}°"
    elif _arm_raised():
        tol = ALIGN_TOL_RAISED_DEG
        tol_note = f"手臂前伸，阈值放宽到 {tol}°"
    else:
        tol = ALIGN_TOL_STRICT_DEG
        tol_note = f"手臂收回，严格阈值 {tol}°"
    dmin = float(body.get("dmin", 0.3))
    dmax = float(body.get("dmax", 1.0))
    state.align_cancel = threading.Event()
    state.align_running = True
    state.align_message = f"对中开始（{tol_note}）…"
    state.align_thread = threading.Thread(
        target=_align_loop, args=(tol, dmin, dmax), name="reach-align", daemon=True)
    state.align_thread.start()
    return {"ok": True, "started": True, "tol_deg": tol}


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
def reach_scan_obstacles(body: dict | None = None):
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

    # 拟合竖直墙面并向下补全：相机只能看到柜面上半部分，视野之下没有
    # 体素，手在低处照样会撞。柜面理论上竖直 → 在水平投影上 RANSAC 拟合
    # 直线（对旁边的杂物鲁棒），从地面补到扫描顶部，一起进碰撞环境。
    wall, wall_plane = _fit_wall_voxels(pts_root, voxel, _ground_z())

    state.obstacles = centers
    state.wall = wall
    state.wall_plane = wall_plane
    state.obstacle_voxel = voxel
    if wall_plane is not None:
        # 拟合成功：碰撞环境只用这面【解析平面】（半空间，零膨胀）。
        # 体素球表示会把几毫米厚的柜面加厚成 ~7.5cm 的球层（球半径
        # 0.75*voxel + 相邻球重叠），近柜规划基本无路可走；平面距离
        # 精确到毫米。体素 centers/wall 仅留作前端可视化。
        state.collision_checker.set_environment([], radius=voxel * 0.75)
        cz = wall_plane["center"][2]
        ext = 0.10   # 柜面可能比相机视野宽：矩形边界各外扩 10cm
        state.collision_checker.set_environment_planes([{
            "point": wall_plane["center"],
            "normal": wall_plane["normal"],
            "dir": wall_plane["dir"],
            "u_range": [wall_plane["u_range"][0] - ext,
                        wall_plane["u_range"][1] + ext],
            "v_range": [wall_plane["z_range"][0] - cz,
                        wall_plane["z_range"][1] - cz + ext],
        }])
    else:
        # 兜底：没拟合出主导墙面（没对着柜子/杂物太多）退回体素球
        state.collision_checker.set_environment(centers, radius=voxel * 0.75)
        state.collision_checker.set_environment_planes([])
    return {"ok": True, "count": int(len(centers)), "voxel_m": voxel,
            "wall_count": 0 if wall is None else int(len(wall)),
            "plane_only": wall_plane is not None,
            "raw_points": int(len(pts_root))}


def _ground_z() -> float:
    """地面在根系（骨盆）下方的高度：全零姿态最低连杆 z 再留 5cm 余量。"""
    try:
        transforms = state.robot_model.forward_kinematics({})
        return float(min(T[2, 3] for T in transforms.values())) - 0.05
    except Exception:
        return -0.9   # H2 骨盆离地约 0.8m 的兜底值


def _fit_wall_voxels(pts_root: np.ndarray, voxel: float, z_floor: float
                     ) -> tuple[np.ndarray | None, dict | None]:
    """从扫描点拟合竖直墙面，返回 (补全体素中心, 平面几何)；失败 (None, None)。

    做法：点云投影到水平面（x,y），RANSAC 拟合直线（= 竖直平面的迹线），
    内点的横向范围决定墙宽，z 从地面（z_floor，根系为骨盆、地面在负半轴）
    一直铺到扫描最高点。
    """
    if len(pts_root) < 80:
        return None, None
    xy = pts_root[:, :2]
    rng = np.random.default_rng(0)
    best_inliers = None
    n = len(xy)
    for _ in range(200):
        i, j = rng.integers(0, n, size=2)
        d = xy[j] - xy[i]
        norm = float(np.hypot(*d))
        if norm < 0.05:
            continue
        # 直线法向（水平面内）
        nvec = np.array([-d[1], d[0]]) / norm
        dist = np.abs((xy - xy[i]) @ nvec)
        inliers = dist < 0.03
        if best_inliers is None or inliers.sum() > best_inliers.sum():
            best_inliers = inliers
    if best_inliers is None or best_inliers.sum() < max(60, 0.3 * n):
        return None, None   # 没有占主导的竖直面（可能没对着柜子）

    pin = pts_root[best_inliers]
    # 内点最小二乘精修：直线方向 = xy 协方差主轴
    center_xy = pin[:, :2].mean(axis=0)
    q = pin[:, :2] - center_xy
    _, _, vt = np.linalg.svd(q, full_matrices=False)
    dir_xy = vt[0] / np.linalg.norm(vt[0])
    n_xy = np.array([-dir_xy[1], dir_xy[0]])   # 水平法向
    # 法线指向机器人一侧（根原点在法线负侧 → 翻号）
    if float(np.dot(n_xy, -center_xy)) < 0:
        n_xy = -n_xy

    t = q @ dir_xy                       # 沿墙横向坐标
    t_lo, t_hi = float(t.min()), float(t.max())
    z_top = float(pin[:, 2].max())
    if z_top <= z_floor + 0.1:
        return None, None

    ts = np.arange(t_lo, t_hi + voxel / 2, voxel)
    zs = np.arange(z_floor + voxel / 2, z_top, voxel)
    if not len(ts) or not len(zs):
        return None, None
    grid_t, grid_z = np.meshgrid(ts, zs)
    wall = np.empty((grid_t.size, 3))
    wall[:, 0] = center_xy[0] + grid_t.ravel() * dir_xy[0]
    wall[:, 1] = center_xy[1] + grid_t.ravel() * dir_xy[1]
    wall[:, 2] = grid_z.ravel()
    plane = {
        "center": [float(center_xy[0]), float(center_xy[1]),
                   float((z_top + z_floor) / 2)],
        "normal": [float(n_xy[0]), float(n_xy[1]), 0.0],
        "dir": [float(dir_xy[0]), float(dir_xy[1]), 0.0],
        "width_m": float(t_hi - t_lo),
        "height_m": float(z_top - z_floor),
        # 碰撞用的矩形边界：u 沿 dir（相对 center），z 为绝对高度
        "u_range": [float(t_lo), float(t_hi)],
        "z_range": [float(z_floor), float(z_top)],
    }
    return wall, plane


@router.post("/clear_obstacles")
def reach_clear_obstacles():
    if state.collision_checker is not None:
        state.collision_checker.clear_environment()
    state.obstacles = None
    state.wall = None
    state.wall_plane = None
    return {"ok": True, "count": 0}


@router.get("/obstacles")
def reach_obstacles():
    return {
        "count": 0 if state.obstacles is None else int(len(state.obstacles)),
        "voxel_m": state.obstacle_voxel,
        "centers": [] if state.obstacles is None else state.obstacles.tolist(),
        "wall_count": 0 if state.wall is None else int(len(state.wall)),
        "wall_centers": [] if state.wall is None else state.wall.tolist(),
        "wall_plane": state.wall_plane,
    }


# --------------- 笛卡尔直线插补（沿面横移用） ---------------


def _cartesian_line(start_named: dict, p_from: np.ndarray, p_to: np.ndarray,
                    step: float) -> tuple[list[dict], dict, float]:
    """TCP 从 p_from 直线走到 p_to：切小步逐步 IK（前一步做种子）。

    返回 (路点列表[不含起点], 终点关节, 最大 IK 误差 mm)；IK 不收敛抛 ValueError。
    """
    from core.types import IKRequest, Pose

    model = state.robot_model
    tcp_offset = Pose(xyz=list(state.p_tool))
    total = float(np.linalg.norm(p_to - p_from))
    if total < 1e-6:
        return [], start_named, 0.0
    n_steps = max(2, int(np.ceil(total / max(step, 1e-4))))
    out: list[dict] = []
    q_prev = start_named
    max_err = 0.0
    for i in range(1, n_steps + 1):
        target = p_from + (p_to - p_from) * (i / n_steps)
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
            raise ValueError(f"第 {i}/{n_steps} 步 IK 未收敛（{res.error_mm:.1f} mm），"
                             "直线可能超出可达范围")
        max_err = max(max_err, float(res.error_mm))
        q_prev = res.named_target_joints
        out.append({"named_joints": q_prev, "tcp_pose": res.tcp_pose.to_dict()})
    return out, q_prev, max_err


def _attach_collision(waypoints: list[dict], check: bool) -> dict | None:
    """按需给路点标注逐帧碰撞并返回摘要。"""
    if not check or state.collision_checker is None:
        return None
    from core.types import Pose
    tcp_offset = Pose(xyz=list(state.p_tool))
    checks = state.collision_checker.check_trajectory(
        waypoints, state.chain_id, tcp_offset)
    collision = state.collision_checker.summarize_checks(checks)
    for wp, chk in zip(waypoints, checks):
        wp["collision"] = {"status": chk["status"],
                           "status_label": chk["status_label"],
                           "min_distance_mm": chk["min_distance_mm"]}
    return collision


@router.post("/plan_cartesian")
def reach_plan_cartesian(body: dict):
    """指尖沿直线平移的轨迹：把总位移切成小步，逐步 IK（前一步做种子），
    TCP 全程钉在直线上——不会像关节空间插值那样中途下沉再抬起。

    Body: {"start_joints": named, "direction_root": [x,y,z], "distance_m": float,
           "step_m": 0.01}
    """
    if state.ik_solver is None:
        return JSONResponse({"ok": False, "error": "IK 求解器未注入"}, status_code=409)
    from core.types import Pose

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

    tcp_offset = Pose(xyz=list(state.p_tool))
    p0 = np.asarray(state.robot_model.tcp_pose(start_named, state.chain_id, tcp_offset).xyz)
    waypoints = [{"index": 0, "named_joints": start_named,
                  "tcp_pose": {"xyz": p0.tolist(), "rpy": [0.0, 0.0, 0.0]}}]
    try:
        seg, _, max_err = _cartesian_line(start_named, p0, p0 + direction * distance, step)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    waypoints.extend(seg)
    for i, wp in enumerate(waypoints):
        wp["index"] = i

    collision = _attach_collision(waypoints, bool(body.get("check_collision", True)))
    return {"ok": True, "waypoints": waypoints, "collision": collision,
            "max_ik_error_mm": max_err, "steps": len(waypoints) - 1}


@router.post("/plan_axis_last")
def reach_plan_axis_last(body: dict):
    """「平移在先、进出在后」的取点主段规划（左侧规划）。

    把当前指尖到目标的位移按根系 ±x（机器人前后）分解成两段直线：
      · 往里伸（目标更靠前）：先在当前深度做竖直+水平平移对齐，最后一段
        纯 +x 往里伸——绝不提前探进去；
      · 往外拔（目标更靠后）：先纯 -x 拔出到目标深度，再做平移——
        平移永远发生在离面板较远的那个深度。
    两段都是笛卡尔直线（逐步 IK，TCP 钉在线上）。

    Body: {"start_joints": named, "target_root": [x,y,z],
           "step_m": 0.01, "check_collision": bool=true, "lift_m": 0.02}

    lift_m（中段抬高，仅往里伸时生效）：真实执行时实测位置拖在指令下方
    1~3cm（运动中的跟踪下垂），目标点又常贴着障碍下表面——平移段故意
    多抬 lift_m，进给段从【上前方】斜切进目标，真实路径全程高于目标高度，
    不会从下面钻进去刮底。终点精度不受影响（终点靠稳态收敛）。给 0 关闭。
    """
    if state.ik_solver is None:
        return JSONResponse({"ok": False, "error": "IK 求解器未注入"}, status_code=409)
    from core.types import Pose

    try:
        start_named = {str(k): float(v) for k, v in dict(body["start_joints"]).items()}
        p_target = np.asarray(body["target_root"], dtype=float).reshape(3)
        step = abs(float(body.get("step_m", 0.01)))
        lift = max(0.0, float(body.get("lift_m", 0.02)))
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"参数非法: {exc}"}, status_code=400)

    tcp_offset = Pose(xyz=list(state.p_tool))
    p0 = np.asarray(state.robot_model.tcp_pose(start_named, state.chain_id, tcp_offset).xyz)
    dx = float(p_target[0] - p0[0])
    if dx >= 0.0:
        # 往里伸：平移段（保持当前 x，高度多抬 lift），进给段从上前方斜切入目标
        p_mid = np.array([p0[0], p_target[1], p_target[2] + lift])
    else:
        # 往外拔：先退到目标深度（纯 -x），再平移
        p_mid = np.array([p_target[0], p0[1], p0[2]])

    # 逐步 IK 是纯 Python 计算，在服务进程里会和 50Hz 控制环/相机线程抢
    # GIL（同序列 RRT 的老问题：离线亚秒、在线好几秒）。fork 子进程独享
    # GIL，恢复离线速度；state/求解器由 fork 语义直接继承，零序列化成本。
    ctx = mp.get_context("fork")
    rx, tx = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_axis_last_worker, daemon=True,
                       args=(tx, start_named, p0.tolist(), p_mid.tolist(),
                             p_target.tolist(), dx, step,
                             bool(body.get("check_collision", True))))
    proc.start()
    tx.close()
    if rx.poll(30.0):
        plan_status, payload = rx.recv()
    else:
        plan_status, payload = "err", "规划子进程 30s 无响应"
    proc.join(timeout=1.0)
    if proc.is_alive():
        proc.kill()
    if plan_status != "ok":
        return JSONResponse({"ok": False, "error": str(payload)}, status_code=422)
    payload["mid_root"] = p_mid.tolist()
    return payload


def _axis_last_worker(conn, start_named, p0_list, p_mid_list, p_target_list,
                      dx, step, check_collision):
    """fork 子进程里跑左侧规划的逐步 IK + 碰撞标注（独享 GIL）。"""
    try:
        p0 = np.asarray(p0_list, dtype=float)
        p_mid = np.asarray(p_mid_list, dtype=float)
        p_target = np.asarray(p_target_list, dtype=float)
        waypoints = [{"named_joints": start_named,
                      "tcp_pose": {"xyz": p0.tolist(), "rpy": [0.0, 0.0, 0.0]}}]
        max_err = 0.0
        q = start_named
        for label, a, b in (("平移" if dx >= 0 else "拔出", p0, p_mid),
                            ("进给" if dx >= 0 else "平移", p_mid, p_target)):
            try:
                seg, q, err = _cartesian_line(q, a, b, step)
            except ValueError as exc:
                conn.send(("err", f"{label}段: {exc}"))
                return
            max_err = max(max_err, err)
            waypoints.extend(seg)
        for i, wp in enumerate(waypoints):
            wp["index"] = i
        if len(waypoints) < 2:
            conn.send(("err", "位移为零，无需规划"))
            return
        collision = _attach_collision(waypoints, check_collision)
        conn.send(("ok", {"ok": True, "waypoints": waypoints, "collision": collision,
                          "max_ik_error_mm": max_err,
                          "mode": "push_in" if dx >= 0 else "pull_out",
                          "steps": len(waypoints) - 1}))
    except Exception as exc:
        conn.send(("err", f"规划子进程异常: {exc}"))
    finally:
        conn.close()


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
def reach_waypoints():
    return {"waypoints": _load_waypoints()}


@router.post("/waypoints")
def reach_record_waypoint(body: dict):
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
def reach_delete_waypoint(filename: str):
    path = _safe_waypoint_file(filename)
    if path is None:
        return JSONResponse({"ok": False, "error": "非法文件名"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": f"没有文件 {filename!r}"}, status_code=404)
    path.unlink()
    return {"ok": True}


# --------------- 动作序列（多个路点按序回放，一键调用，纯关节回放无 IK） ---------------


def _safe_sequence_file(filename: str) -> Path | None:
    if not filename.endswith(".json") or "/" in filename or "\\" in filename or ".." in filename:
        return None
    return state.sequences_dir / filename


@router.get("/sequences")
def reach_sequences():
    if state.sequences_dir is None or not state.sequences_dir.is_dir():
        return {"sequences": []}
    items = []
    for path in sorted(state.sequences_dir.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text())
            data["file"] = path.name
            items.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return {"sequences": items}


@router.post("/sequences")
def reach_save_sequence(body: dict):
    """把一组路点按顺序存为动作序列。Body: {"name": str, "waypoints": [路点文件名...]}

    序列只存路点文件名的引用；执行由前端逐段触发（关节空间插值回放，无 IK）。
    """
    name = str(body.get("name") or "").strip()
    files = body.get("waypoints") or []
    if not name:
        return JSONResponse({"ok": False, "error": "序列需要名字"}, status_code=400)
    if "/" in name or "\\" in name or ".." in name:
        return JSONResponse({"ok": False, "error": "名字不能包含路径分隔符"}, status_code=400)
    if not files:
        return JSONResponse({"ok": False, "error": "序列至少要有 1 个路点"}, status_code=400)
    for f in files:
        p = _safe_waypoint_file(str(f))
        if p is None or not p.is_file():
            return JSONResponse({"ok": False, "error": f"路点文件不存在: {f}"}, status_code=400)
    item = {
        "name": name,
        "chain_id": state.chain_id,
        "waypoints": [str(f) for f in files],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state.sequences_dir.mkdir(parents=True, exist_ok=True)
    path = state.sequences_dir / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2))
    item["file"] = path.name
    return {"ok": True, "sequence": item}


SEQ_REPLAY_DRIFT_RAD = 0.5   # 起点漂移超过它才重新规划（正常复用不会触发）


def _sequence_plan_worker(conn, q0_list, target_lists, target_names, margin):
    """fork 子进程里跑序列 RRT 规划（独享 GIL，不被控制环/相机线程拖慢）。

    fork 继承父进程内存：robot_model、collision_checker（含墙平面/豁免球）
    直接可用；对 checker 的改动只影响子进程副本。结果经管道回传：
    ("ok", frames) 或 ("err", 消息)。子进程不碰 DDS/相机。
    """
    try:
        from core.types import Pose
        from planners.rrt import densify, rrt_connect_path

        tcp_offset = Pose(xyz=list(state.p_tool))
        checker = state.collision_checker
        q0 = np.asarray(q0_list, dtype=float)
        targets = [np.asarray(t, dtype=float) for t in target_lists]

        if checker is not None:
            # 路点是人工验证过的，可能本来就贴着柜面——在其 TCP 周围开豁免球
            spheres = list(checker.environment_exclusions)
            for tgt in targets:
                tcp = _tcp_position(tgt.tolist())
                if tcp:
                    spheres.append((tcp, state.target_exclusion_m))
            checker.set_environment_exclusions(spheres)
            if checker.enabled:
                # 预检仅作日志：端点碰撞按误报豁免（见 rrt_connect_path）
                for name, tgt in [("当前姿态", q0)] + list(zip(target_names, targets)):
                    chk = checker.check_state(tgt.tolist(), state.chain_id, tcp_offset)
                    if chk["status"] != "collision":
                        continue
                    pair = chk.get("pair") or {}
                    depth = abs(float(chk.get("min_distance_mm") or 0.0))
                    print(f"[reach] 路点「{name}」模型碰撞已豁免: "
                          f"{pair.get('a', '?')} ↔ {pair.get('b', '?')}"
                          f"（嵌入 {depth:.0f}mm，实际到过的姿态视为误报）")

        full: list[np.ndarray] = [q0]
        for i, (name, tgt) in enumerate(zip(target_names, targets), 1):
            try:
                leg = rrt_connect_path(
                    state.robot_model, checker, state.chain_id,
                    full[-1], tgt, tcp_offset,
                    {"margin_m": margin, "timeout_s": 20.0})
            except ValueError as exc:
                conn.send(("err", f"第{i}段（→「{name}」）{exc}"))
                return
            full.extend(leg[1:])
        q_list = densify(full, frame_rad=0.04)
        conn.send(("ok", [[float(v) for v in q] for q in q_list]))
    except Exception as exc:  # noqa: BLE001 —— 子进程任何崩溃都回传给父进程
        conn.send(("err", f"规划子进程异常: {exc}"))
    finally:
        conn.close()


@router.post("/sequences/run")
def reach_run_sequence(body: dict):
    """一键执行已保存的动作序列。供无界面的自动化封装直接调用。

    执行方式（v2）：第一次运行用「直线优先、撞了才 RRT-Connect」逐段规划出
    无碰撞轨迹，并把完整轨迹帧录进序列文件（trajectory 字段）；之后运行直接
    回放录制轨迹——不算 RRT、不算 IK、不做碰撞检查，请求即执行。
    起点与录制起点漂移超过 0.5 rad（说明工况变了）才触发一次重新规划。

    Body: {"file": str, "joint_speed": float=0.35, "max_speed_rad_s": float=0.4,
           "replan": bool=false,    # replan=true 强制丢弃录制轨迹重规划
           "margin_m": float=0.01}  # 墙面退让：正=墙逼近(更保守)，负=墙后退
    首次规划（或 replan）不直接执行：把轨迹录进文件并回传 preview 帧给前端
    仿真回放，用户确认后再次调用走"录播"路径才真机执行。
    进度用 GET /exec_status 轮询，POST /stop 急停。
    """
    if state.controller is None:
        return JSONResponse({"ok": False, "error": "手臂未接管"}, status_code=409)
    path = _safe_sequence_file(str(body.get("file") or ""))
    if path is None or not path.is_file():
        return JSONResponse({"ok": False, "error": "序列文件不存在"}, status_code=404)
    try:
        seq = json.loads(path.read_text())
        targets = []
        target_names = []
        for fname in seq.get("waypoints") or []:
            wp_path = _safe_waypoint_file(str(fname))
            wp = json.loads(wp_path.read_text())
            targets.append(np.asarray([float(wp["named_joints"][n])
                                       for n in state.joint_names], dtype=float))
            target_names.append(str(wp.get("name") or fname))
        if not targets:
            raise ValueError("序列不含路点")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"序列/路点读取失败: {exc}"}, status_code=400)

    joint_speed = float(np.clip(float(body.get("joint_speed") or 0.35), 0.05, 0.5))
    speed = float(np.clip(float(body.get("max_speed_rad_s") or 0.4), 0.05, 0.5))
    margin = float(np.clip(float(body.get("margin_m", 0.01)), -0.05, 0.05))

    with state.exec_lock:
        if state.exec_running:
            return JSONResponse({"ok": False, "error": "已有轨迹在执行中"}, status_code=409)
        try:
            q0 = np.asarray(state.controller.read_measured(), dtype=float)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"读不到真机关节: {exc}"}, status_code=503)

        # ---- 优先回放录制轨迹（免 RRT/IK/碰撞检查，零计算耗时） ----
        planned = False
        q_list: list[np.ndarray] | None = None
        rec = seq.get("trajectory")
        if rec and not bool(body.get("replan")):
            frames = [np.asarray(f, dtype=float) for f in rec.get("frames") or []]
            if frames and frames[0].shape == q0.shape:
                drift = float(np.max(np.abs(frames[0] - q0)))
                if drift <= SEQ_REPLAY_DRIFT_RAD:
                    q_list = [q0] + frames   # 从当前实测平滑接入第一帧
                # 漂移太大 → 落到下面重新规划

        if q_list is None:
            # ---- 首次（或工况变了）：fork 子进程跑 RRT。规划是纯 Python
            # 计算，在服务进程里会和 50Hz 控制环/相机线程抢 GIL（离线 2s 的
            # 问题在线 6s 都解不完）；子进程独享 GIL，恢复 2s 级 ----
            ctx = mp.get_context("fork")
            rx, tx = ctx.Pipe(duplex=False)
            proc = ctx.Process(target=_sequence_plan_worker, daemon=True,
                               args=(tx, q0.tolist(),
                                     [t.tolist() for t in targets],
                                     target_names, margin))
            proc.start()
            tx.close()
            if rx.poll(60.0):
                plan_status, payload = rx.recv()
            else:
                plan_status, payload = "err", "规划子进程 60s 无响应"
            proc.join(timeout=1.0)
            if proc.is_alive():
                proc.kill()
            if plan_status != "ok":
                print(f"[reach] 序列规划失败: {payload}")
                return JSONResponse({"ok": False, "error": f"规划失败: {payload}"},
                                    status_code=422)
            q_list = [np.asarray(f, dtype=float) for f in payload]
            planned = True
            try:
                seq["trajectory"] = {
                    "frames": [[round(float(v), 5) for v in q] for q in q_list],
                    "joint_names": list(state.joint_names),
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                    "planner": "line-else-rrt",
                }
                path.write_text(json.dumps(seq, ensure_ascii=False, indent=1))
            except Exception as exc:
                print(f"[reach] 序列轨迹录制失败: {exc}")

        total_travel = sum(float(np.max(np.abs(b - a)))
                           for a, b in zip(q_list, q_list[1:]))
        duration = max(1.0, total_travel / joint_speed)

        if planned:
            # 刚规划出来的轨迹不直接执行：回传抽稀后的帧给前端仿真回放，
            # 用户看过确认后再按一次 ▶ ——那时轨迹已录进文件，走录播路径执行
            from core.types import Pose
            tcp_offset = Pose(xyz=list(state.p_tool))
            stride = max(1, len(q_list) // 150)
            picks = list(range(0, len(q_list), stride))
            if picks[-1] != len(q_list) - 1:
                picks.append(len(q_list) - 1)
            frames_preview = []
            for k in picks:
                joints = [float(v) for v in q_list[k]]
                frames_preview.append({
                    "named_joints": state.robot_model.named_chain_joints(
                        joints, state.chain_id),
                    "tcp_pose": state.robot_model.tcp_pose(
                        joints, state.chain_id, tcp_offset),
                    "link_poses": state.robot_model.link_poses(
                        joints, state.chain_id),
                })
            return {"ok": True, "preview": True, "planned": True,
                    "replayed": False, "frames": len(q_list),
                    "duration_s": round(duration, 2),
                    "preview_frames": frames_preview}
        label = f"序列:{str(seq.get('name') or path.stem)}"[:32]

        state.exec_cancel.clear()
        state.exec_running = True
        state.exec_progress = 0.0
        state.exec_message = "执行中"
        state.exec_thread = threading.Thread(
            target=_exec_loop, args=(q_list, duration, None, speed, label), daemon=True)
        state.exec_thread.start()
    return {"ok": True, "duration_s": round(duration, 2), "frames": len(q_list),
            "planned": planned, "replayed": not planned, **_exec_status()}


@router.delete("/sequences/{filename}")
def reach_delete_sequence(filename: str):
    path = _safe_sequence_file(filename)
    if path is None:
        return JSONResponse({"ok": False, "error": "非法文件名"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": f"没有文件 {filename!r}"}, status_code=404)
    path.unlink()
    return {"ok": True}


# --------------- 横移录制（免 IK 回放） ---------------
# 逐点 IK 一次 6cm 要 ~6s（纯 Python FK + 数值雅可比）。机器人正视电柜时
# 横移方向在根系里是常量、起点姿态也基本重复，所以把算好的整段路点存下来，
# 以后同工况直接按"当前起点 + 录制关节增量"回放，免掉全部 IK。
# 是否可回放由前端把关：距离一致 + 方向夹角 <10° + 起点关节偏差 <0.1 rad。


@router.get("/sidesteps")
def reach_list_sidesteps():
    if state.sidesteps_dir is None or not state.sidesteps_dir.is_dir():
        return {"sidesteps": []}
    items = []
    for path in sorted(state.sidesteps_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            data["file"] = path.name
            items.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return {"sidesteps": items}


@router.post("/sidesteps")
def reach_save_sidestep(body: dict):
    """保存一次横移规划（同距离覆盖旧的）。
    Body: {"step_cm": float, "direction_root": [3], "waypoints": [{named_joints, tcp_pose}...]}
    """
    try:
        step_cm = float(body["step_cm"])
        direction = [float(v) for v in body["direction_root"]]
        waypoints = list(body["waypoints"])
        if len(waypoints) < 2:
            raise ValueError("路点太少")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"参数非法: {exc}"}, status_code=400)
    item = {
        "step_cm": step_cm,
        "direction_root": direction,
        "waypoints": waypoints,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state.sidesteps_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{'L' if step_cm > 0 else 'R'}{abs(step_cm):.0f}cm"
    path = state.sidesteps_dir / f"sidestep_{tag}.json"
    path.write_text(json.dumps(item, ensure_ascii=False))
    item["file"] = path.name
    return {"ok": True, "sidestep": item}


@router.post("/hand_move")
def reach_hand_move(body: dict | None = None):
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
def reach_joints():
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
def reach_arm():
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
def reach_disarm():
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
def reach_diagnostics():
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
def reach_exec_status():
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
def reach_execute(body: dict):
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
def reach_stop():
    """急停：中止执行线程并冻结在当前指令位。"""
    if state.controller is None:
        return JSONResponse({"ok": False, "error": "手臂未接管"}, status_code=409)
    state.exec_cancel.set()
    state.controller.stop()
    state.exec_message = "已急停（刚性保持）"
    return {"ok": True, **_exec_status()}
