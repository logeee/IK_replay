"""运行时状态与注入配置：ReachState、configure()，以及最底层的关节/躯干读取。"""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter

router = APIRouter(prefix="/api/reach")


class ReachState:
    """由 reach_server 注入的运行时状态。"""

    def __init__(self):
        self.enabled = False
        self.camera = None                 # hand_eye_3D 的 CameraBase
        self.wrist_camera = None           # teleimager 右腕 JPEG，只用于拨动前核验
        self.yolo_base = "http://127.0.0.1:7004"
        self.last_flip_verification: dict[str, Any] | None = None
        self.robot_id = "h2"
        self.chain_id = "right_arm"
        self.T_cam2root: np.ndarray | None = None   # URDF 根 <- 彩色相机
        self.T_cam2torso: np.ndarray | None = None  # torso_link <- 彩色相机
        self.p_tool: list[float] | None = None      # 指尖在腕系的位置（TCP 偏移）
        self.p_tool_by_marker: dict[str, list[float]] = {}  # 各手部标记点在腕系的位置
        self.tool_reference_marker: str | None = None
        self.wrist_link: str | None = None
        self.calib_meta: dict[str, Any] = {}
        self.gravity_profile: dict[str, Any] = {}
        self.handeye_ready = False
        self.camera_only = False
        self.robot_only = False
        self.controller = None             # H2ArmController，仅在前端"接管"后创建
        self.arm_factory = None            # 无参函数 -> H2ArmController；None = 无法真机执行
        self.provider_reader = None        # 只读 lowstate 关节读取（未接管时用）
        self.torso_reader = None           # 只读腰关节 + IMU（躯干姿态诊断）
        self.motors_reader = None          # 只读任意全身电机角度（按序号）
        self.loco_client = None            # 高层 loco RPC（原地转身用），懒创建
        self.loco_available = False        # 有 DDS（非 --no-robot）才可用
        self.hand_raised_ui = False        # 前端人工标注"已抬手"，随转身/对中日志落盘
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
        self.pick_context: dict[str, Any] | None = None
        self.pick_revision = 0                     # 每次取点递增，供跨浏览器同步
        self.pick_revision_lock = threading.Lock()
        self.torso_diag: dict | None = None        # 最近一次执行的躯干漂移诊断
        self.log_dir: Path | None = None           # 每段执行落一行 JSONL
        self.pick_history_dir: Path | None = None  # 与选点记录一同保存执行诊断
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
        self.exec_phase = "idle"           # traj/converge/settle/trim/push_hold/release
        self.settle_trim = "off"           # off/discrete/continuous：到位后落点修正模式
        self.last_settle_trim: dict | None = None   # 最近一次执行的修正结果（含偏置）
        self.execution_history: deque[dict[str, Any]] = deque(maxlen=30)
        self.execution_history_lock = threading.Lock()


state = ReachState()


def configure(*, camera, wrist_camera=None, robot_model, robot_id: str, chain_id: str,
              calib_path: Path | None, camera_only: bool = False,
              robot_only: bool = False,
              collision_checker=None, ik_solver=None, arm_factory=None,
              joints_reader=None, torso_reader=None, motors_reader=None,
              tool_out_mm: float = 0.0,
              yolo_base: str = "http://127.0.0.1:7004",
              gravity_profile: dict[str, Any] | None = None,
              settle_trim: str = "off") -> None:
    """由 reach_server 调用。calib_path 是 handeye3d_result.json。

    camera_only=True 时不加载手眼标定，只开放相机流与相机系深度观测；
    机器人坐标相关接口由 reach_server 的保护层禁用。

    tool_out_mm: 标定的 p_tool 点（当时选在手指上，离真正指尖还差一点）
    沿法兰盘法线向外的附加偏移。法兰盘平面 = 手掌安装面 = 腕系 y-z 平面，
    其法线严格为腕系 +x，"向外" = +x（远离法兰、指向指尖方向）。
    """
    calib = None
    T_cam2torso = None
    T_cam2root = None
    p_tool = None
    p_tool_by_marker: dict[str, list[float]] = {}
    tool_reference_marker = None
    calib_reference_marker = None
    tcp_definition: dict[str, Any] = {"type": "calibration_reference"}
    wrist_link = None
    base_link = "torso_link"
    if not camera_only:
        if calib_path is None:
            raise ValueError("非相机预览模式必须提供手眼标定")
        calib = json.loads(Path(calib_path).read_text())
        T_cam2torso = np.asarray(calib["T_cam2base"], dtype=float).reshape(4, 4)
        base_link = calib.get("base_link", "torso_link")

        # 全零关节下 URDF 根 <- base_link（腰 0 假设，与查看器/IK 一致）
        transforms = robot_model.forward_kinematics({})
        if base_link not in transforms:
            raise ValueError(f"标定的 base_link {base_link!r} 不在 URDF 中")
        T_root_torso = transforms[base_link]
        T_cam2root = T_root_torso @ T_cam2torso
        raw_markers = calib.get("p_tool_wrist_m_by_marker", {})
        if isinstance(raw_markers, dict):
            for marker_id, xyz in raw_markers.items():
                if not isinstance(xyz, (list, tuple)) or len(xyz) != 3:
                    raise ValueError(
                        f"手部关键点 {marker_id!r} 必须是腕系下的 3 维坐标"
                    )
                point = [float(v) for v in xyz]
                if not all(np.isfinite(point)):
                    raise ValueError(f"手部关键点 {marker_id!r} 包含非有限数值")
                p_tool_by_marker[str(marker_id)] = point
        calib_reference_marker = calib.get(
            "p_tool_reference_marker", calib.get("p_tool_reference")
        )
        if "red" in p_tool_by_marker and "blue" in p_tool_by_marker:
            p_tool = (
                (np.asarray(p_tool_by_marker["red"], dtype=float)
                 + np.asarray(p_tool_by_marker["blue"], dtype=float))
                * 0.5
            ).tolist()
            tool_reference_marker = None
            tcp_definition = {"type": "marker_midpoint", "markers": ["red", "blue"]}
        else:
            p_tool = [float(v) for v in calib["p_tool_wrist_m"]]
            tool_reference_marker = calib_reference_marker
        p_tool[0] += float(tool_out_mm) / 1000.0
        wrist_link = calib.get("wrist_link", chain_id.replace("_arm", "_wrist_yaw_link"))

    state.camera = camera
    state.wrist_camera = wrist_camera
    state.yolo_base = yolo_base.rstrip("/")
    state.robot_id = robot_id
    state.chain_id = chain_id
    state.T_cam2torso = T_cam2torso
    state.T_cam2root = T_cam2root
    state.p_tool = p_tool
    state.p_tool_by_marker = p_tool_by_marker
    state.tool_reference_marker = tool_reference_marker
    state.wrist_link = wrist_link
    state.handeye_ready = not camera_only
    state.camera_only = camera_only
    state.robot_only = robot_only
    state.gravity_profile = dict(gravity_profile or {})
    if settle_trim not in ("off", "discrete", "continuous"):
        raise ValueError(f"settle_trim 只能是 off/discrete/continuous，收到 {settle_trim!r}")
    state.settle_trim = settle_trim
    if camera_only:
        state.calib_meta = {
            "ready": False,
            "mode": "camera_only",
            "message": "尚未加载手眼标定；仅开放相机预览和相机系深度观测",
        }
    else:
        state.calib_meta = {
            "ready": True,
            "path": str(calib_path),
            "base_link": base_link,
            "solved_at": calib.get("solved_at"),
            "rms_mm": calib.get("residual_mm", {}).get("rms"),
            "num_samples": calib.get("num_samples"),
            "tool_out_mm": float(tool_out_mm),
            "wrist_link": wrist_link,
            "marker_count": len(p_tool_by_marker),
            "tool_reference_marker": tool_reference_marker,
            "calib_reference_marker": calib_reference_marker,
            "tcp_definition": tcp_definition,
        }
    state.arm_factory = arm_factory
    state.provider_reader = joints_reader
    state.torso_reader = torso_reader
    state.motors_reader = motors_reader
    state.loco_available = joints_reader is not None   # 有 DDS 连接才谈得上转身
    state.base_link = base_link
    state.joint_names = robot_model.joint_names(chain_id)
    state.robot_model = robot_model
    state.collision_checker = collision_checker
    state.ik_solver = ik_solver
    project_root = Path(__file__).resolve().parents[2]
    state.waypoints_dir = project_root / "data" / "waypoints"
    state.sequences_dir = project_root / "data" / "sequences"
    state.sidesteps_dir = project_root / "data" / "sidesteps"
    state.log_dir = project_root / "logs" / "reach"
    state.pick_history_dir = project_root / "data" / "pick_history"
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
