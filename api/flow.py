"""拨动开关全流程编排。

已部署的步骤（测平面/测距、腰部对齐、pick→规划→执行）直接走 reach_server；
起手式按距离和物理拨动方向自动选：向左拨使用「X.XX-起手式新」，向右拨
使用「X.XX-左-起手式」；选档 = 实测距离-0.03m 后四舍五入到最近档。重试轮插值
回所选序列配套的终点路点；普通起手式收尾直接插值到「起手点测试」，左-
起手式必须先回配套终点、再到「起手点测试」，最后才释放手臂。成功和失败
（含重试耗尽）都走对应的安全回落，避免手臂扫到柜面或停在柜面前被权重
渐出交还本体。
场景判断和拨后复核走 7004 YOLO 服务（python -m api.yolo_server）：
每处视觉判断连问 3 帧再下结论；配了 YOLO 却仍没结论时报 YOLO_FAILED
退出（手臂受控回落），不转人工——无人值守的自动化不能卡在等人上。
YOLO 识别的是开关的真实印刷状态（工厂柜实测：印刷相反时也能正确读出
「远方」）。任务 kind 决定起止状态，site + kind 决定物理拨动方向：
  工厂柜远方→就地：从右向左；工厂柜就地→远方：从左向右。
  实验室柜的印刷方向相反，因此同一物理方向对应相反的状态变化。
拨完识别 flip_to = 成功、flip_from = 失败重试。

取点（默认走 7005 语义点云算法）：冻结同帧 RGB-D → 建墙面坐标系 →
YOLO Mask 拟合面板矩形取中心（粉点）→ 0.2.0-s 模型偏移得到目的点 →
（可选）叠加墙面系人工微调 → 交 18001 确认成 p_root。7005 未配置时
退回旧方案：flip_from 框 + 固定相对像素偏移。
哪个 language 能执行由调度层按 site 决定。
只有压根没配 YOLO（--no-yolo 手动模式）才把视觉判断转 7002 人工确认台。

不给 console 时保持旧行为：人工顶不上的步骤抛 FlowError(NOT_IMPLEMENTED)。

腰部对齐目标从 config/waist_alignment.json 读取：
  3️⃣ 粗对齐：默认目标 -7°，验收 -8.5°~0°，预补偿抬手后的正向回转。
  6️⃣ 细保持：默认目标 -3°，验收 -8°~8°。判据取 3 帧中位数防单帧污染；
     超出范围才纠偏，服务端在抬手状态下限制单杆 ≤5°、累计 ≤30°并设安全闸。

错误码：占位。等正式定义后替换 ErrorCode 的取值即可，接口形状不变。
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .client import ReachClient
from .console_client import ConsoleAbort, ConsoleClient
from .flip_evidence import (
    PICK_HISTORY_DIR,
    save_flip_evidence,
    save_pick_flow_context,
)
from .yolo_client import YoloClient


class ErrorCode(IntEnum):
    """占位错误码——正式编码待定义，先保证每类失败有独立的码。"""

    OK = 0
    NOT_IMPLEMENTED = 1    # 步骤尚未实现
    PRECONDITION = 2       # 服务不可用 / 未接管 / DDS 断开等前置失败
    ALIGN_FAILED = 3       # 腰部调节超时或不收敛
    MEASURE_FAILED = 4     # 平面/距离测量失败
    YOLO_FAILED = 5        # YOLO 检测失败（场景/点位/复核通用，细分待定）
    IK_FAILED = 6          # IK 不可达或预演碰撞
    EXEC_FAILED = 7        # 真机执行失败
    VERIFY_FAILED = 8      # 拨动复核不通过且重试耗尽
    ABORTED = 9            # 人工急停或外部中断
    POSE_UNAVAILABLE = 10  # 距离不满足任何起手式的适用范围（如 <0.46m）


class FlowError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class FlowResult:
    ok: bool
    code: ErrorCode
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


FLIP_KIND_STATES: dict[str, tuple[str, str]] = {
    "close_to_remote": ("就地", "远方"),
    "remote_to_close": ("远方", "就地"),
}
SITE_RTL_KIND = {
    "lab": "close_to_remote",
    "factory": "remote_to_close",
}


def resolve_flip_intent(site: str, kind: str | None = None) -> dict[str, str]:
    """Resolve semantic states and the physical direction for one task."""
    clean_site = str(site or "").strip().lower()
    if clean_site not in SITE_RTL_KIND:
        raise ValueError(f"不支持的现场：{site!r}")
    clean_kind = str(kind or SITE_RTL_KIND[clean_site]).strip().lower()
    if clean_kind not in FLIP_KIND_STATES:
        raise ValueError(f"不支持的拨动任务：{kind!r}")
    flip_from, flip_to = FLIP_KIND_STATES[clean_kind]
    direction = "rtl" if clean_kind == SITE_RTL_KIND[clean_site] else "ltr"
    return {
        "site": clean_site,
        "kind": clean_kind,
        "flip_from": flip_from,
        "flip_to": flip_to,
        "direction": direction,
    }


def interpolate_offset_keyframes(
    keyframes: list[dict[str, Any]],
    distance_m: float,
) -> tuple[tuple[float, float, float], dict[str, Any]]:
    """Piecewise-linear XYZ interpolation with clamped outer boundaries."""
    if not keyframes:
        raise ValueError("距离偏移关键帧不能为空")
    frames = sorted(keyframes, key=lambda item: float(item["distance_m"]))

    def values(frame: dict[str, Any]) -> tuple[float, float, float]:
        raw = frame["offset_wall_m"]
        return tuple(float(raw[index]) for index in range(3))

    distance = float(distance_m)
    left = right = frames[0]
    ratio = 0.0
    if distance >= float(frames[-1]["distance_m"]):
        left = right = frames[-1]
    elif distance > float(frames[0]["distance_m"]):
        for index in range(1, len(frames)):
            candidate = frames[index]
            if distance <= float(candidate["distance_m"]):
                left = frames[index - 1]
                right = candidate
                left_d = float(left["distance_m"])
                right_d = float(right["distance_m"])
                ratio = (distance - left_d) / (right_d - left_d)
                break

    left_values = values(left)
    right_values = values(right)
    offset = tuple(
        left_values[index]
        + (right_values[index] - left_values[index]) * ratio
        for index in range(3)
    )
    return offset, {
        "mode": "keyframes",
        "distance_m": distance,
        "left_distance_m": float(left["distance_m"]),
        "right_distance_m": float(right["distance_m"]),
        "ratio": ratio,
        "offset_wall_m": list(offset),
    }


class SwitchFlow:
    """一键开始 → … → 拨动完成 的整条流程。

    未部署步骤的临时实现走 console（7002 确认台）；正式实现就绪后
    覆写对应方法（detect_scene / detect_points / verify_flip / …）即可。
    """

    def __init__(self,
                 client: ReachClient | None = None,
                 console: ConsoleClient | None = None,
                 yolo: YoloClient | None = None,
                 # 3️⃣ 粗对齐目标：抬手+起手式之后身体会自己往正方向转，所以
                 # 手放下时先"过打"这么多，让它自己漂进 6️⃣ 的保持带。
                 # 漂移量与起始角有关（从 -4.5 出发漂 +6.6 落 +2.1；从 -10 出发
                 # 漂 +3.9 落 -6.1），两点线性外推 → 想落 -3 该从 -8 出发。
                 # 又因为 6️⃣ 只能往 - 方向纠（见 adapters/reach.py 的单向闸），
                 # 落点宁可偏 + 一点（可纠）也别偏 -（只能干等），故取 -7：
                 # 预计落 -1.6，落带内且在可纠的那一侧
                 coarse_target_deg: float = -7.0,
                 coarse_tol_deg: float = 1.5,      # 兼容旧调用；未给 min/max 时生成对称带
                 coarse_accept_min_deg: float | None = None,
                 coarse_accept_max_deg: float | None = None,
                 coarse_command_tol_deg: float | None = None,
                 fine_target_deg: float = -3.0,    # 6️⃣ 保持目标
                 fine_tol_deg: float = 2.0,        # 兼容旧调用；未给 min/max 时生成对称带
                 fine_accept_min_deg: float | None = None,
                 fine_accept_max_deg: float | None = None,
                 fine_command_tol_deg: float | None = None,
                 align_mode: str = "hold",         # "hold"=新对中（打杆式）
                 dmin: float = 0.4, dmax: float = 1.0,
                 # ---- IK 拨动段参数（index.html 真机验证过的一组）----
                 approach_offset_m: float = 0.0,   # 接近偏移：0 = 指尖顶到表面
                 reach_duration_s: float = 6.0,    # 主段（到位）时长
                 sidestep_cm: float = 10.0,        # 到位后沿柜面左移（负=右移）
                 push_force_n: float = 15.0,       # 横移时的前馈推力
                 push_hold_s: float | None = None,  # 拨过后满推力保持秒数（None=执行端默认 1.5）
                 sidestep_down_deg: float | None = None,  # 横移向下倾角（None=类默认 15°）
                 pose_pattern: str | None = None,   # 起手式命名正则（能力注册表注入；None=按方向用内置正则）
                 lift_m: float = 0.02,             # 规划中段抬高 2cm（防刮底）
                 endpoint_speed_rad_s: float = 0.3,  # 插值回「终点」路点的关节限速
                 max_flip_rounds: int = 3,         # 拨动失败回到 5️⃣ 的最大轮数
                 # ---- 目标点上抬（抵消重力下垂，17001 可配，单位米）----
                 # 实测指尖落点比指令位低 20~31 mm（logs/reach 里
                 # tcp.planned_root vs actual_root），打不中多半是打低了。
                 lift_base_m: float = 0.01,        # 首轮上抬
                 lift_step_m: float = 0.01,        # 每重试一轮再多抬
                 lift_max_m: float = 0.03,         # 封顶：再高就不是下垂能解释的
                 align_timeout_s: float = 90.0,
                 exec_timeout_s: float = 120.0,
                 site: str = "lab",                # lab=实验室柜 / factory=工厂柜（印刷相反）
                 flip_kind: str | None = None,      # close_to_remote / remote_to_close
                 pointcloud: Any = None,           # 7005 点云找点客户端（None=退回旧框偏移法）
                 # 目的点人工微调（墙面系，米）：算法算出目的点后再叠加，
                 # 不动"粉点→目的点"的模型偏移。x=沿墙向右 y=法向入墙 z=沿墙向上
                 target_offset_wall_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 # 按所选起手式距离逐轴线性插值；有值时覆盖上面的静态偏移。
                 target_offset_keyframes: list[dict[str, Any]] | None = None,
                 target_offset_preset_name: str = "",
                 # 仅第1轮在上述基础偏移之上额外叠加；第2轮起自动归零。
                 first_round_offset_wall_m: tuple[float, float, float] = (0.0, 0.0, 0.0)):
        self.client = client or ReachClient()
        self.console = console
        self.yolo = yolo
        self.pointcloud = pointcloud
        self.target_offset_wall_m = tuple(
            float(v) for v in target_offset_wall_m
        )
        self.target_offset_keyframes = [
            {
                "distance_m": float(frame["distance_m"]),
                "offset_wall_m": tuple(
                    float(value) for value in frame["offset_wall_m"]
                ),
            }
            for frame in (target_offset_keyframes or [])
        ]
        self.target_offset_preset_name = str(target_offset_preset_name or "")
        self._target_offset_interpolation: dict[str, Any] | None = None
        self.first_round_offset_wall_m = tuple(
            float(v) for v in first_round_offset_wall_m
        )
        self.coarse_target_deg = coarse_target_deg
        self.coarse_accept_min_deg = (
            coarse_target_deg - coarse_tol_deg
            if coarse_accept_min_deg is None else coarse_accept_min_deg
        )
        self.coarse_accept_max_deg = (
            coarse_target_deg + coarse_tol_deg
            if coarse_accept_max_deg is None else coarse_accept_max_deg
        )
        self.coarse_command_tol_deg = (
            coarse_tol_deg / 2
            if coarse_command_tol_deg is None else coarse_command_tol_deg
        )
        self.fine_target_deg = fine_target_deg
        self.fine_accept_min_deg = (
            fine_target_deg - fine_tol_deg
            if fine_accept_min_deg is None else fine_accept_min_deg
        )
        self.fine_accept_max_deg = (
            fine_target_deg + fine_tol_deg
            if fine_accept_max_deg is None else fine_accept_max_deg
        )
        self.fine_command_tol_deg = (
            fine_tol_deg / 2
            if fine_command_tol_deg is None else fine_command_tol_deg
        )
        self.align_mode = align_mode
        self.dmin = dmin
        self.dmax = dmax
        self.approach_offset_m = approach_offset_m
        self.reach_duration_s = reach_duration_s
        self.sidestep_distance_cm = abs(float(sidestep_cm))
        self.push_force_n = push_force_n
        self.push_hold_s = None if push_hold_s is None else float(push_hold_s)
        self.sidestep_down_deg = (
            self.WALL_SIDESTEP_DOWN_DEG if sidestep_down_deg is None
            else float(sidestep_down_deg)
        )
        self.pose_pattern = (
            re.compile(pose_pattern) if pose_pattern else None
        )
        self.lift_m = lift_m
        self.endpoint_speed_rad_s = endpoint_speed_rad_s
        self.max_flip_rounds = max_flip_rounds
        self.lift_base_m = max(0.0, float(lift_base_m))
        self.lift_step_m = max(0.0, float(lift_step_m))
        self.lift_max_m = max(0.0, float(lift_max_m))
        self.align_timeout_s = align_timeout_s
        self.exec_timeout_s = exec_timeout_s
        intent = resolve_flip_intent(site, flip_kind)
        self.site = intent["site"]
        self.flip_kind = intent["kind"]
        self.flip_from = intent["flip_from"]
        self.flip_to = intent["flip_to"]
        self.flip_direction = intent["direction"]
        # 正=左移、负=右移；外部参数只决定距离绝对值，方向由任务语义唯一决定。
        self.sidestep_cm = (
            self.sidestep_distance_cm
            if self.flip_direction == "rtl"
            else -self.sidestep_distance_cm
        )
        self._current_pose: dict | None = None
        self._measured_distance_m: float | None = None
        self._armed_by_flow = False   # 手臂是流程接管的（而非用户本来就接管着）
        self._arm_moved = False       # 已下发过手臂动作 → 失败时要先受控回落
        # 拨动证据：横移前/复核时各存一帧头部相机图 + YOLO 判定，
        # 落到本轮选点记录目录（data/pick_history/<record>/flip_*）
        self._last_pick_record: str | None = None
        self._last_flip_round: int | None = None
        self.log_lines: deque[str] = deque(maxlen=300)   # 供调度服务透出进度
        # 强制停止开关：外部（/emergency/stop）置位后，流程在最近的检查点退出。
        # 置位时手臂多半已被强停端点直接释放了，所以退出路径不再做受控回落。
        self.abort = threading.Event()
        # 软复位与硬急停分开：流程停止后要等待调度层完成低刚度回位和释放，
        # 防止任务线程提前关闭 reach_server。
        self.reset_and_release = threading.Event()
        self.reset_complete = threading.Event()
        self.reset_result: dict[str, Any] | None = None
        # 手动模式步骤闸门：设置后每个主要步骤执行前先调用它（阻塞等操作员
        # 确认）。签名 gate(step_id, message, detail)；操作员选择中止时应抛
        # FlowError(ABORTED)。None = 全自动，不出提示。
        self.gate: Any = None
        # 分步耗时统计（"step"=步骤执行，"confirm"=手动模式的确认等待）
        self.step_times: list[dict[str, Any]] = []
        self._step_name: str | None = None
        self._step_started = 0.0

    def request_abort(self) -> None:
        self.abort.set()

    def request_reset_and_release(self) -> None:
        self.reset_and_release.set()

    def finish_reset_and_release(self, result: dict[str, Any]) -> None:
        self.reset_result = dict(result)
        self.reset_complete.set()

    def _check_abort(self) -> None:
        if self.abort.is_set():
            raise FlowError(ErrorCode.ABORTED, "收到强制停止")
        if self.reset_and_release.is_set():
            raise FlowError(ErrorCode.ABORTED, "收到机械臂复位并释放请求")

    def safe_reset_waypoints(self) -> list[str]:
        """当前姿态到释放点的安全路径；左-起手式先经过配套终点。"""
        route: list[str] = []
        pose = self._current_pose
        if self._is_left_start_pose(pose):
            endpoint = self._pose_endpoint_name(pose)
            if endpoint:
                route.append(endpoint)
        route.append(self.DESCEND_WAYPOINT)
        return route

    # ------------------------------------------------------ 步骤计时与手动闸门

    def _step_begin(self, name: str) -> None:
        """开始一个计时步骤；上一步（若还开着）自动结算。"""
        self._step_finish()
        self._step_name = name
        self._step_started = time.monotonic()

    def _step_finish(self) -> None:
        if self._step_name is None:
            return
        self.step_times.append({
            "step": self._step_name,
            "kind": "step",
            "duration_s": round(time.monotonic() - self._step_started, 1),
        })
        self._step_name = None

    def step_report(self) -> list[dict[str, Any]]:
        """已结算的分步耗时 + 正在进行的步骤，供调度服务状态接口透出。"""
        report = [dict(entry) for entry in self.step_times]
        if self._step_name is not None:
            report.append({
                "step": self._step_name,
                "kind": "step",
                "running": True,
                "duration_s": round(time.monotonic() - self._step_started, 1),
            })
        return report

    def _log_step_summary(self) -> None:
        entries = [e for e in self.step_times if e["kind"] == "step"]
        if not entries:
            return
        text = "；".join(f"{e['step']} {e['duration_s']}s" for e in entries)
        confirm_s = sum(
            e["duration_s"] for e in self.step_times if e["kind"] == "confirm"
        )
        if confirm_s:
            text += f"；确认等待合计 {round(confirm_s, 1)}s"
        self._log(f"步骤耗时：{text}")

    def _confirm(self, step_id: str, message: str,
                 detail: dict[str, Any] | None = None) -> None:
        """手动模式闸门：阻塞到操作员决定；等待时长单独计，不算进步骤耗时。"""
        # 全自动也必须经过这个中断检查，确保“复位并释放”后不会进入下一动作。
        self._check_abort()
        if self.gate is None:
            return
        self._step_finish()
        t0 = time.monotonic()
        self._log(f"⏸ 等待操作员确认：{message}")
        try:
            self.gate(step_id, message, dict(detail or {}))
        finally:
            self.step_times.append({
                "step": f"确认等待 · {message}",
                "kind": "confirm",
                "duration_s": round(time.monotonic() - t0, 1),
            })
        self._log("▶ 操作员已确认，继续执行")

    # ------------------------------------------------------------------ 主流程

    def run(self) -> FlowResult:
        """1️⃣ 一键开始。任何一步失败即返回，携带占位错误码。"""
        t0 = time.monotonic()
        try:
            self._check_abort()
            self._log("═══ 1️⃣ 一键开始 ═══")
            direction_text = "从右向左（左移）" if self.flip_direction == "rtl" \
                else "从左向右（右移）"
            # 措辞别带阶段卡的关键词（如"拨动成功"），看板靠日志兜底推进度
            self._log(
                f"现场={'工厂柜（印刷相反）' if self.site == 'factory' else '实验室柜'}："
                f"任务「{self.flip_from} → {self.flip_to}」，物理方向 "
                f"{direction_text} {self.sidestep_distance_cm:g}cm；"
                f"识别「{self.flip_from}」= 要拨、"
                f"「{self.flip_to}」= 目标状态"
            )
            self._confirm("preflight",
                          "即将检查前置条件（reach 服务 / 真机能力 / 确认台），"
                          "若手臂未接管会自动接管")
            self._step_begin("1️⃣ 前置检查与接管")
            self._preflight()

            self._log("═══ 2️⃣ 场景判断（是否需要拨动、往哪个方向）═══")
            self._confirm("scene", "即将进行场景判断（YOLO 纯视觉，不动机器人）")
            self._step_begin("2️⃣ 场景判断")
            scene = self.detect_scene()
            if not scene.get("need_flip", True):
                self._release_if_flow_armed()
                return self._done(t0, "无需拨动，流程结束", scene=scene)
            self._log(f"场景: {scene}")

            self._log(f"═══ 3️⃣ 腰部粗对齐：目标 "
                      f"{self.coarse_target_deg:+.1f}°，验收 "
                      f"{self.coarse_accept_min_deg:+.1f}°"
                      f"~{self.coarse_accept_max_deg:+.1f}°，"
                      f"已预补偿抬手后的回转 ═══")
            self._confirm(
                "coarse_align",
                f"即将进行腰部粗对齐（可能真机原地转身），目标 "
                f"{self.coarse_target_deg:+.1f}°、验收 "
                f"{self.coarse_accept_min_deg:+.1f}°"
                f"~{self.coarse_accept_max_deg:+.1f}°",
            )
            self._step_begin("3️⃣ 腰部粗对齐")
            self._coarse_align_with_retry()

            self._log("═══ 4️⃣ 测距离 ═══")
            self._confirm("measure", "即将测量距柜面距离（纯测量，不动机器人）")
            self._step_begin("4️⃣ 测距离")
            distance_m = self.measure_distance()
            self._measured_distance_m = distance_m
            self._log(f"距柜面 {distance_m:.3f} m")

            last_error: FlowError | None = None
            for round_no in range(1, self.max_flip_rounds + 1):
                self._check_abort()
                self._log(f"═══ 5️⃣ 第 {round_no}/{self.max_flip_rounds} 轮 ═══")
                try:
                    pose = self.choose_opening_pose(distance_m)
                    self._current_pose = pose   # 重试轮插值回它配套的「终点」路点
                    self._apply_offset_keyframes_for_pose(pose)
                    self._log(f"起手式: {pose}")
                    pose_detail = {
                        "起手式": pose["name"],
                        "距柜面": f"{distance_m:.3f} m",
                        "轮次": f"{round_no}/{self.max_flip_rounds}",
                    }
                    if round_no == 1:
                        # 新起手式按距离逐档录制，摆完即到位，不再分远/近补位
                        self._confirm(
                            "opening_pose",
                            f"即将回放起手式「{pose['name']}」"
                            f"（距柜面 {distance_m:.3f} m，真机手臂大幅动作）",
                            pose_detail,
                        )
                        self._step_begin(f"5️⃣ 起手式回放（第{round_no}轮）")
                        self.apply_opening_pose(pose)
                    else:
                        # 重试轮：上一轮结束时手臂已在「终点」高位附近，直接
                        # 插值回终点路点即可——回放整条起手式会让手下去再上来，
                        # 且起点漂移触发的重规划轨迹未经人工验证
                        self._log("重试轮：跳过起手式回放，插值回终点路点作为起手位")
                        self._confirm(
                            "goto_endpoint",
                            f"重试第{round_no}轮：即将插值回终点路点"
                            f"「{pose['name']}终点」作为起手位（真机手臂动作）",
                            pose_detail,
                        )
                        self._step_begin(f"5️⃣ 回终点路点（第{round_no}轮）")
                        self._goto_endpoint(f"重试第{round_no}轮")

                    self._log(f"═══ 6️⃣ 腰部细对齐并保持：目标 "
                              f"{self.fine_target_deg:+.1f}°，验收 "
                              f"{self.fine_accept_min_deg:+.1f}°"
                              f"~{self.fine_accept_max_deg:+.1f}° ═══")
                    self._confirm(
                        "fine_align",
                        f"即将进行腰部细对齐并保持（可能真机原地转身），目标 "
                        f"{self.fine_target_deg:+.1f}°、验收 "
                        f"{self.fine_accept_min_deg:+.1f}°"
                        f"~{self.fine_accept_max_deg:+.1f}°",
                    )
                    self._step_begin(f"6️⃣ 腰部细对齐（第{round_no}轮）")
                    self._fine_align_with_retry()

                    self._confirm("detect_points",
                                  "即将取点（视觉识别拨动目标，不动机器人）")
                    self._step_begin(f"7️⃣ 取点（第{round_no}轮）")
                    points = self._detect_points_held(round_no)
                    self._log(f"点位: {self._points_brief(points)}")

                    self._log("IK 执行拨动")
                    side_text = "左移" if self.sidestep_cm > 0 else "右移"
                    self._confirm(
                        "flip",
                        f"即将执行 IK 拨动：规划 → {self.reach_duration_s:g}s "
                        f"到位 → 沿柜面{side_text} "
                        f"{self.sidestep_distance_cm:g}cm + 前馈推力 "
                        f"{self.push_force_n:g}N（真机动作，规划就绪后直接执行）",
                        {"点位": self._points_brief(points),
                         "轮次": f"{round_no}/{self.max_flip_rounds}"},
                    )
                    self._step_begin(f"8️⃣ IK 拨动（第{round_no}轮）")
                    self.flip_switch(points, round_no)

                    self._log("复核拨动结果")
                    self._confirm("verify", "即将复核拨动结果（视觉判断，不动机器人）")
                    self._step_begin(f"9️⃣ 拨动复核（第{round_no}轮）")
                    if self.verify_flip():
                        self._log("拨动成功 ✔")
                        self._log("═══ 收尾：快速回落 ═══")
                        left_safe_route = self._is_left_start_pose(pose)
                        route_text = (
                            f"先回配套终点「{self._pose_endpoint_name(pose)}」，"
                            f"再回落到「{self.DESCEND_WAYPOINT}」"
                            if left_safe_route
                            else f"回落到「{self.DESCEND_WAYPOINT}」"
                        )
                        self._confirm(
                            "descend",
                            f"拨动成功：即将{route_text}，到位后释放手臂"
                            f"（真机动作）",
                        )
                        self._step_begin("🔟 收尾回落与释放")
                        self.descend_fast(pose)
                        return self._done(t0, "拨动成功", rounds=round_no,
                                          distance_m=distance_m)
                    self._log("拨动未成功，回到 5️⃣ 重试")
                    last_error = FlowError(ErrorCode.VERIFY_FAILED, "复核未通过")
                except FlowError as exc:
                    if exc.code in (ErrorCode.NOT_IMPLEMENTED,
                                    ErrorCode.POSE_UNAVAILABLE,
                                    ErrorCode.ALIGN_FAILED,
                                    ErrorCode.ABORTED):
                        # 未实现/距离不够/对不齐：重摆起手式也不会变，直接中止
                        raise
                    self._log(f"本轮失败（{exc.code.name}: {exc.message}），回到 5️⃣")
                    last_error = exc
            raise last_error or FlowError(ErrorCode.VERIFY_FAILED, "重试轮数耗尽")

        except ConsoleAbort:
            self._step_finish()
            self._log("✘ 操作员在确认台中止了流程")
            try:
                self.client.stop()
            except Exception:
                pass
            self._log_step_summary()
            return FlowResult(ok=False, code=ErrorCode.ABORTED,
                              message="确认台人工中止",
                              detail={"elapsed_s": round(time.monotonic() - t0, 1)})
        except FlowError as exc:
            self._step_finish()
            self._log(f"✘ 流程中止：[{exc.code.name}] {exc.message}")
            if self.abort.is_set():
                # 强制停止：手臂已由 /emergency/stop 急停并释放，这里绝不能
                # 再下发回落动作——那等于在"已经放手"之后又去动机器人
                self._log("强制停止：不做回落，手臂控制权已交还本体")
            elif self.reset_and_release.is_set():
                self._log("机械臂复位请求：流程已中断，等待低刚度回位并释放")
                if not self.reset_complete.wait(timeout=90.0):
                    self._log("⚠ 等待机械臂复位结果超时；保持当前接管状态")
                elif (self.reset_result or {}).get("ok"):
                    self._log("机械臂已回到起手点测试并释放")
                else:
                    self._log(
                        "⚠ 机械臂复位未完成："
                        f"{(self.reset_result or {}).get('error') or '未知错误'}"
                    )
            else:
                self._descend_on_failure()
            self._log_step_summary()
            return FlowResult(ok=False, code=exc.code, message=exc.message,
                              detail={"elapsed_s": round(time.monotonic() - t0, 1)})

    # ------------------------------------------------------------ 已就绪的步骤

    def _preflight(self) -> None:
        """服务、DDS、手臂接管状态检查；给了 console 时顺带确认它在线。"""
        try:
            st = self.client.status()
        except Exception as exc:
            raise FlowError(ErrorCode.PRECONDITION, f"reach_server 不可达: {exc}")
        self._log(f"服务状态: armed={st.get('armed')} "
                  f"arm_supported={st.get('arm_supported')}")
        if not st.get("arm_supported"):
            raise FlowError(ErrorCode.PRECONDITION, "无真机执行能力（--no-robot 模式？）")
        if not st.get("armed"):
            self._log("未接管手臂，自动接管…")
            res = self.client.arm()
            if not res.get("ok"):
                raise FlowError(ErrorCode.PRECONDITION,
                                f"接管失败: {res.get('error')}")
            self._armed_by_flow = True   # 提前退出时把手臂还回去
        if self.console is not None and not self.console.alive():
            raise FlowError(ErrorCode.PRECONDITION,
                            f"确认台不可达（{self.console.base}）——"
                            f"先启动 python -m api.console")
        if self.yolo is not None and not self.yolo.alive():
            # 不算硬失败：场景判断/复核会按次转确认台，人工顶上
            self._log(f"⚠ YOLO 服务不可达（{self.yolo.base}），"
                      f"场景判断和复核将转人工确认台")

    def _release_if_flow_armed(self) -> None:
        """无需拨动等"没动过手臂"的提前退出：流程自己接管的就还回去。

        用户进流程前就接管着的不动——别替人做主。
        """
        if not self._armed_by_flow:
            return
        self._log("手臂是本流程接管的，未执行任何动作，自动释放")
        res = self.client.disarm()
        if not res.get("ok"):
            self._log(f"释放失败（不影响流程结果）: {res.get('error')}")

    def measure_plane(self) -> dict:
        """平面指数（yaw_err_deg）等测量，失败抛 MEASURE_FAILED。"""
        fit = self.client.perpendicular(self.dmin, self.dmax)
        if not fit.get("ok"):
            raise FlowError(ErrorCode.MEASURE_FAILED,
                            f"平面拟合失败: {fit.get('error')}")
        return fit

    def measure_distance(self) -> float:
        return float(self.measure_plane()["distance_m"])

    @staticmethod
    def _in_band(yaw: float, minimum: float, maximum: float) -> bool:
        return minimum <= yaw <= maximum

    @staticmethod
    def _band_text(minimum: float, maximum: float) -> str:
        return f"{minimum:+.1f}°~{maximum:+.1f}°"

    def waist_align(self, target_deg: float, accept_min_deg: float,
                    accept_max_deg: float, cmd_tol_deg: float) -> None:
        """腰部调节：向 target_deg 闭环，最终按独立的验收范围判断。

        3️⃣ 抬手前和 6️⃣ 抬手后都用它。cmd_tol_deg 是服务端围绕目标角的
        停止阈值；accept_min/max 是流程验收范围，允许配置为非对称区间。
        """
        yaw = float(self.measure_plane()["yaw_err_deg"])
        band = self._band_text(accept_min_deg, accept_max_deg)
        if self._in_band(yaw, accept_min_deg, accept_max_deg):
            self._log(f"平面指数已在带内（yaw {yaw:+.2f}°，"
                      f"验收 {band}），跳过")
            return
        res = self.client.align_yaw_start(self.dmin, self.dmax,
                                          tol_deg=cmd_tol_deg,
                                          target_deg=target_deg,
                                          mode=self.align_mode)
        if not res.get("ok"):
            raise FlowError(ErrorCode.ALIGN_FAILED,
                            f"对中启动失败: {res.get('error')}")
        deadline = time.monotonic() + self.align_timeout_s
        last_msg: str | None = None
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if self.abort.is_set():
                self.client.align_yaw_stop()
                raise FlowError(ErrorCode.ABORTED, "收到强制停止（对中中）")
            fit = self.client.perpendicular(self.dmin, self.dmax)
            align = fit.get("align") or {}
            if not align.get("running"):
                err = (abs(float(fit["yaw_err_deg"]) - target_deg)
                       if fit.get("ok") else None)
                shown = "读不到" if err is None else f"{err:.2f}°"
                self._log(f"对中结束: {align.get('message')}（复测残差 {shown}）")
                yaw_final = (None if not fit.get("ok")
                             else float(fit["yaw_err_deg"]))
                if (yaw_final is not None
                        and self._in_band(yaw_final, accept_min_deg, accept_max_deg)):
                    return
                raise FlowError(ErrorCode.ALIGN_FAILED,
                                f"对中结束但 yaw "
                                f"{'读不到' if yaw_final is None else f'{yaw_final:+.2f}°'}"
                                f" 未进入验收范围 {band}")
            msg = align.get("message") or ""
            if msg != last_msg:          # 每秒轮询，同一杆别重复刷屏
                self._log(f"对中中… {msg}")
                last_msg = msg
        self.client.align_yaw_stop()
        raise FlowError(ErrorCode.ALIGN_FAILED, f"对中超时（>{self.align_timeout_s}s）")

    COARSE_ALIGN_ATTEMPTS = 3

    def _coarse_align_with_retry(self) -> None:
        """3️⃣ 粗对齐：收进配置的粗对齐验收范围，未达标原地重试。

        目标角已经把"抬手后身体自己回转 +6.6°"预补偿进去了（见构造函数注释），
        所以这一步结束时看着是"过打"的，抬手之后才会落到 -3° 附近。

        服务端围绕目标角使用更窄的 command_tolerance 停止；流程再用独立一帧
        按配置的非对称验收范围复测，避免把控制停止范围和业务通过范围绑死。
        """
        for i in range(1, self.COARSE_ALIGN_ATTEMPTS + 1):
            try:
                self.waist_align(
                    self.coarse_target_deg,
                    self.coarse_accept_min_deg,
                    self.coarse_accept_max_deg,
                    self.coarse_command_tol_deg,
                )
                return
            except FlowError as exc:
                if (exc.code != ErrorCode.ALIGN_FAILED
                        or i == self.COARSE_ALIGN_ATTEMPTS):
                    raise
                self._log(f"粗对齐未达标（{exc.message}），原地重试"
                          f"（第 {i}/{self.COARSE_ALIGN_ATTEMPTS - 1} 次）")

    FINE_MEASURE_FRAMES = 3    # 抬手后取多帧投票，单帧污染直接被中位数投掉
    FINE_MEASURE_GAP_S = 0.4

    def _fine_yaw(self, what: str) -> float:
        """抬手后的平面指数：取 3 帧中位数。

        抬手后躯干前倾、手臂进画面，单帧拟合有概率被地面/手臂污染（量出几十
        度的假值）。中位数直接把这种孤立帧投掉，避免拿假值去转身——2026-07-30
        17:19 就是拿单帧假值 +42° 连发了 6 杆 22° 的整体转身。
        """
        yaws = []
        for i in range(self.FINE_MEASURE_FRAMES):
            if i:
                time.sleep(self.FINE_MEASURE_GAP_S)
            yaws.append(float(self.measure_plane()["yaw_err_deg"]))
        yaw = sorted(yaws)[len(yaws) // 2]
        self._log(f"{what}：yaw {yaw:+.2f}°"
                  f"（{self.FINE_MEASURE_FRAMES} 帧 "
                  f"{'/'.join(f'{v:+.2f}' for v in yaws)}）")
        return yaw

    def _fine_align_with_retry(self, attempts: int = 3) -> None:
        """6️⃣ 抬手后细对齐：把平面指数纠回 fine 带，失败原地重试。

        手臂前伸会被整机配平带着把躯干转过去，实测漂移 +3.5~+9.9°（越往前伸
        越大，摆过「0.5以上」时最大），方向固定往正。先按配置的验收范围判断，
        超出时才向目标角纠偏。
        判据用 3 帧中位数（防单帧污染），纠偏由服务器闭环做，抬手状态下服务端
        只往 - 方向纠：yaw 低于目标时它只等不纠（那个方向和身体自己的 + 向
        回转同向，越纠越远，07-31 两次甩到 +30° 都是这么起头的）。所以真正靠
        3️⃣ 的过打量把落点摆在目标的 + 侧，这里只负责收掉多出来的那部分。
        另有单杆 ≤5°、累计 ≤30°，以及拟合点数、偏差上限（±24°）、运控无响应
        三道闸（见 adapters/reach.py）。
        """
        band = self._band_text(
            self.fine_accept_min_deg, self.fine_accept_max_deg
        )
        yaw = 0.0
        for i in range(1, attempts + 1):
            yaw = self._fine_yaw("6️⃣ 抬手后复查")
            if self._in_band(
                yaw, self.fine_accept_min_deg, self.fine_accept_max_deg
            ):
                self._log(f"在保持带 {band} 内")
                return
            self._log(f"抬手后漂出保持带 {band}，转身纠偏"
                      f"（第 {i}/{attempts} 次）")
            try:
                self.waist_align(
                    self.fine_target_deg,
                    self.fine_accept_min_deg,
                    self.fine_accept_max_deg,
                    self.fine_command_tol_deg,
                )
            except FlowError as exc:
                if exc.code != ErrorCode.ALIGN_FAILED or i == attempts:
                    raise
                self._log(f"纠偏未达标（{exc.message}），再试一次")
        raise FlowError(ErrorCode.ALIGN_FAILED,
                        f"抬手后 {attempts} 次纠偏仍在 {yaw:+.2f}°，"
                        f"未收进保持带 {band}，手臂将受控回落")

    def _detect_points_held(self, round_no: int = 1) -> list[dict]:
        """取点前后都守住保持带：取点期间漂出就重新纠偏，再重新取点。"""
        for attempt in (1, 2):
            points = self.detect_points(round_no)
            yaw = self._fine_yaw("取点后复查")
            if self._in_band(
                yaw, self.fine_accept_min_deg, self.fine_accept_max_deg
            ):
                return points
            self._log(f"取点期间漂出保持带（yaw {yaw:+.2f}°），"
                      f"重新纠偏后重新取点（第 {attempt} 次）")
            self._fine_align_with_retry(attempts=2)
        return self.detect_points(round_no)

    # 语义点云：沿柜面 ±X 并向 -Z 偏 15°；旧链路保留向下倾 2°。
    SIDESTEP_TILT_DEG = 2.0
    WALL_SIDESTEP_DOWN_DEG = 15.0
    SIDESTEP_PUSH_SPEED = 0.06   # 带推力时快拨（m/s）：借冲量越过定位卡点

    def flip_switch(self, points: list[dict], round_no: int = 1) -> None:
        """IK 执行拨动：

          取点（接近偏移 0）→ 左侧规划（中段抬高 2cm）→
          主段到位（6s）→ 按任务方向沿柜面横移 10cm + 前馈推力 15N

        拨完就地停住直接交给复核（拨动本身不要求到点精度，不再先插值回
        「终点」路点）：成功 → 收尾直接回「起手点测试」；失败 → 重试轮
        先插值回终点路点当起手位。规划就绪后直接真机执行，不经确认台。
        目标点按轮次上抬（抵消重力下垂）：首轮 lift_base_m，每重试一轮
        加 lift_step_m，封顶 lift_max_m——不再按距柜面远近区分，三个量
        都可在 17001 配置。
        """
        lift = min(self.lift_base_m + (round_no - 1) * self.lift_step_m,
                   self.lift_max_m)
        why = (f"首轮 {self.lift_base_m * 1000:g}"
               f" + 每轮 {self.lift_step_m * 1000:g}×{round_no - 1}"
               f"，封顶 {self.lift_max_m * 1000:g} mm")
        for i, pt in enumerate(points, 1):
            if pt.get("p_root"):
                # 点云算法路径：detect_points 已经过 18001 确认，直接用
                picked = pt
                tag = f"点位 {i}/{len(points)} (算法目的点)"
            else:
                u, v = int(pt["u"]), int(pt["v"])
                tag = f"点位 {i}/{len(points)} ({u},{v})"
                picked = self.client.pick(
                    u, v, approach_offset_m=self.approach_offset_m)
                if not picked.get("ok"):
                    raise FlowError(ErrorCode.IK_FAILED,
                                    f"{tag} 取点失败: {picked.get('error')}")

            target = [float(v) for v in picked["p_root"]]
            if lift > 0:
                target[2] += lift
                self._log(f"{tag} 目标点上抬 {lift * 1000:g} mm（{why}）"
                          f"（z {picked['p_root'][2]:.3f} → {target[2]:.3f} m）")
            self._save_pick_flow_context(picked, round_no, lift, target)

            joints = self.client.joints()
            if not joints.get("ok"):
                raise FlowError(ErrorCode.PRECONDITION,
                                f"读不到关节: {joints.get('error')}")
            # 不做碰撞检查：目标点本来就贴着柜面，指尖终点必然挨着"墙"，
            # 碰撞标注全是误报（调试页对主段也只提示不拦）
            plan = self.client.plan_axis_last(joints["named_joints"], target,
                                              lift_m=self.lift_m,
                                              check_collision=False)
            if not plan.get("ok"):
                raise FlowError(ErrorCode.IK_FAILED,
                                f"{tag} 规划失败: {plan.get('error')}")
            frames = plan["waypoints"]
            self._log(f"{tag} 预演就绪：{len(frames)} 路点，"
                      f"IK 误差 {plan.get('max_ik_error_mm')}mm")

            self._arm_moved = True
            self._check_abort()
            res = self.client.execute(
                waypoints=[f["named_joints"] for f in frames],
                duration=self.reach_duration_s, label="flow_reach")
            if not res.get("ok"):
                raise FlowError(ErrorCode.EXEC_FAILED,
                                f"{tag} 到位执行被拒: {res.get('error')}")
            self._wait_exec(f"{tag} 到位")

            # 横移（拨动本体）之前存一帧证据：此刻开关还是拨前状态
            self._last_pick_record = picked.get("record")
            self._last_flip_round = round_no
            self._flip_evidence_before()
            self._sidestep_flick(picked, tag)

    def _sidestep_direction(self, plane: dict) -> list[float]:
        """Return the root-frame left/right direction with the configured tilt."""
        left = plane.get("left_root")
        if not left:
            raise FlowError(ErrorCode.IK_FAILED,
                            "表面平面拟合失败，定不出横移方向")
        sg = 1.0 if self.sidestep_cm > 0 else -1.0
        right = plane.get("right_root")
        if right:
            wall_up = plane.get("wall_up_root")
            if not wall_up:
                raise FlowError(
                    ErrorCode.IK_FAILED,
                    "柜面坐标系缺少 Z 轴，无法计算向下偏移",
                )
            t = math.radians(self.sidestep_down_deg)
            c, s = math.cos(t), math.sin(t)
            # 柜面系 X 正=右、Z 正=上。正 sidestep 表示左：
            # 左右分量取 ∓X，两种方向都叠加 -Z 方向 15°。
            return [
                -right[i] * sg * c - wall_up[i] * s
                for i in range(3)
            ]
        t = math.radians(self.SIDESTEP_TILT_DEG)
        c, s = math.cos(t), math.sin(t)
        # 旧取点链路没有柜面 X 轴时才使用兼容算法。
        return [
            left[0] * sg * c,
            left[1] * sg * c,
            left[2] * sg * c - s,
        ]

    def _sidestep_flick(self, picked: dict, tag: str) -> None:
        """到位后的拨动本体：按任务方向就地规划横移并施加前馈推力。"""
        if abs(self.sidestep_cm) < 0.5:
            return
        direction = self._sidestep_direction(picked.get("plane") or {})
        dist = abs(self.sidestep_cm) / 100.0
        side_text = "左移" if self.sidestep_cm > 0 else "右移"

        joints = self.client.joints()
        if not joints.get("ok"):
            raise FlowError(ErrorCode.PRECONDITION,
                            f"读不到关节: {joints.get('error')}")
        seg = self.client.plan_cartesian(joints["named_joints"], direction,
                                         dist, step_m=0.01,
                                         check_collision=False)
        if not seg.get("ok"):
            raise FlowError(ErrorCode.IK_FAILED,
                            f"{tag} 横移规划失败: {seg.get('error')}")

        body: dict[str, Any] = {
            "waypoints": [f["named_joints"] for f in seg["waypoints"]],
            "label": f"flow_flick{self.sidestep_cm:+.0f}cm",
            # 带推力时快拨（0.06 m/s）；无推力保持慢滑（0.02 m/s）
            "duration": (max(1.0, dist / self.SIDESTEP_PUSH_SPEED)
                         if self.push_force_n > 0 else max(2.0, dist / 0.02)),
        }
        if self.push_force_n > 0:
            # 沿移动方向的前馈力：接触后位置环刚度不够，靠它出力拨动
            body["push"] = {"direction_root": direction,
                            "force_n": self.push_force_n}
            if self.push_hold_s is not None:
                body["push_hold_s"] = self.push_hold_s
        self._check_abort()
        res = self.client.execute(**body)
        if not res.get("ok"):
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"{tag} 横移执行被拒: {res.get('error')}")
        self._wait_exec(f"{tag} 拨动（{side_text}+推力）")

    def _pose_endpoint_name(self, pose: dict | None) -> str:
        """返回起手式配套终点名，兼容左-起手式的独立命名规则。"""
        selected = pose or {}
        explicit = str(selected.get("endpoint_name") or "").strip()
        if explicit:
            return explicit
        name = str(selected.get("name") or "").strip()
        if not name:
            return ""
        if self.LEFT_POSE_PATTERN.match(name):
            return re.sub(r"-起手式$", "-终点", name)
        return f"{name}终点"

    def _goto_endpoint(self, tag: str) -> None:
        """关节插值回起手式配套的「终点」路点。

        优先使用序列最后一个路点的名字，因此普通起手式和向右拨起手式都能
        正确配对：
        「0.49-起手式新」→「0.49-起手式新终点」；
        「0.49-左-起手式」→「0.49-左-终点」。
        """
        pose = self._current_pose or {}
        name = str(pose.get("name") or "").strip()
        if not name:
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"{tag} 没有当前起手式，配不出终点路点名")
        endpoint_name = self._pose_endpoint_name(pose)
        self._interp_to_waypoint(endpoint_name, tag)

    def _interp_to_waypoint(self, wp_name: str, tag: str,
                            only_if_beyond_rad: float = 0.0,
                            speed_rad_s: float | None = None) -> None:
        """关节空间插值到指定名字的已录路点。

        直接把 [当前姿态, 目标姿态] 交给 /execute 做关节插值，不走
        IK/规划——这些路点都是人工录制验证过的安全姿态。
        only_if_beyond_rad > 0 时，当前已在目标附近（最大关节差 ≤ 该值）
        就跳过不动。
        """
        wps = self.client.waypoints().get("waypoints") or []
        target = next((w for w in wps if str(w.get("name")) == wp_name), None)
        if target is None:
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"{tag} 找不到路点「{wp_name}」——请先在调试页录制它")

        joints = self.client.joints()
        if not joints.get("ok"):
            raise FlowError(ErrorCode.PRECONDITION,
                            f"读不到关节: {joints.get('error')}")
        cur = joints["named_joints"]
        end = target["named_joints"]
        travel = max(abs(float(end[k]) - float(cur.get(k, 0.0))) for k in end)
        if only_if_beyond_rad > 0 and travel <= only_if_beyond_rad:
            self._log(f"{tag} 已在「{wp_name}」附近"
                      f"（最大关节差 {travel:.2f} rad），跳过插值")
            return
        speed = speed_rad_s or self.endpoint_speed_rad_s
        duration = max(1.5, travel / max(speed, 0.05))
        self._arm_moved = True
        self._check_abort()
        res = self.client.execute(waypoints=[cur, end], duration=duration,
                                  max_speed_rad_s=speed,
                                  label=f"flow_goto_{wp_name}"[:32])
        if not res.get("ok"):
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"{tag} 回「{wp_name}」被拒: {res.get('error')}")
        self._wait_exec(f"{tag} 插值到「{wp_name}」")

    def _wait_exec(self, label: str) -> None:
        """轮询 exec_status 直到执行结束，按结束消息判断成败。"""
        deadline = time.monotonic() + self.exec_timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.5)
            self._check_abort()
            st = self.client.exec_status()
            if not st.get("running"):
                msg = str(st.get("message") or "")
                self._log(f"{label} 执行结束: {msg}")
                if any(bad in msg for bad in ("中止", "出错", "急停")):
                    raise FlowError(ErrorCode.EXEC_FAILED, f"{label}: {msg}")
                return
        self.client.stop()
        raise FlowError(ErrorCode.EXEC_FAILED,
                        f"{label} 超时（>{self.exec_timeout_s}s），已急停")

    # ------------------------------------------- 未部署步骤（确认台交互顶上）

    def _need_console(self, what: str) -> ConsoleClient:
        if self.console is None:
            raise FlowError(ErrorCode.NOT_IMPLEMENTED, f"{what}未实现（且未接确认台）")
        return self.console

    # 视觉判断的重试：反光、瞬时遮挡、抓到一帧花屏都会让单次推理没结论，
    # 多问几帧就好了。仍然没结论时按"自动化不许卡住"报错码退出，不转人工。
    YOLO_ATTEMPTS = 3
    YOLO_RETRY_WAIT_S = 0.6   # 两次之间等一下，等新的一帧

    def _yolo_scene(self, tag: str, include_image: bool = False) -> dict | None:
        """问 YOLO 服务当前是就地还是远方，最多问 YOLO_ATTEMPTS 次。

        返回 {"scene": "就地"|"远方", "conf": ...}；没配 YOLO、服务不可达
        或每次都没识别到 → 返回 None。include_image=True 时返回里带
        jpeg_b64（判定帧）和 boxes，供拨动证据存档。
        """
        if self.yolo is None:
            return None
        for i in range(1, self.YOLO_ATTEMPTS + 1):
            res = self.yolo.scene(include_image=include_image)
            self._last_yolo_result = res
            if res.get("ok") and res.get("scene") in ("就地", "远方"):
                self._log(f"{tag}：YOLO 识别为「{res['scene']}」"
                          f"（置信度 {res.get('conf')}，第 {i} 次尝试）")
                return {"scene": res["scene"], "conf": res.get("conf"),
                        "boxes": res.get("boxes"),
                        "jpeg_b64": res.get("jpeg_b64"),
                        "wrist_jpeg_b64": res.get("wrist_jpeg_b64"),
                        "wrist_error": res.get("wrist_error")}
            self._log(f"{tag}：第 {i}/{self.YOLO_ATTEMPTS} 次没结论"
                      f"（{res.get('error') or '画面里没识别到就地/远方'}）")
            if i < self.YOLO_ATTEMPTS:
                time.sleep(self.YOLO_RETRY_WAIT_S)
        return None

    def detect_scene(self) -> dict:
        """2️⃣ 是否需要拨动。按任务语义的起止状态判断：

        YOLO 识别 flip_from → 按 site + kind 对应的物理方向拨动；
        flip_to → 已在目标位，无需拨动结束。
        配了 YOLO 但多次都没结论 → 报 YOLO_FAILED 退出（不转人工，否则
        无人值守的自动化会卡在等人上）。只有压根没配 YOLO 时才走确认台。
        """
        got = self._yolo_scene("2️⃣ 场景判断")
        if got is not None:
            if got["scene"] == self.flip_to:
                self._log(f"「{got['scene']}」已是本柜目标状态")
                return {"need_flip": False, "source": "yolo",
                        "conf": got["conf"]}
            self._log(f"「{got['scene']}」是本柜拨前状态，需要拨动"
                      f"（{self.flip_from} → {self.flip_to}）")
            return {"need_flip": True, "direction": self.flip_direction,
                    "source": "yolo",
                    "conf": got["conf"]}
        if self.yolo is not None:
            raise FlowError(ErrorCode.YOLO_FAILED,
                            f"场景判断失败：YOLO 连续 {self.YOLO_ATTEMPTS} 次都没"
                            f"识别到「就地/远方」——检查画面是否被遮挡、反光，"
                            f"或机器人是否正对柜面")
        answer = self._need_console("YOLO 场景判断").choice(
            "2️⃣ 场景判断（YOLO 无结论，请看相机画面人工判断）\n"
            f"任务目标为「{self.flip_to}」，当前是什么状态？",
            [f"当前是「{self.flip_from}」：需要拨动",
             f"当前是「{self.flip_to}」：无需拨动"])
        if self.flip_to in answer:
            return {"need_flip": False, "source": "console"}
        return {"need_flip": True,
                "direction": self.flip_direction,
                "source": "console"}

    # 向左拨使用原「0.49-起手式新」；向右拨使用「0.49-左-起手式」。
    # 前缀数字是该档的录制距离。
    NEW_POSE_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)-起手式新\s*$")
    LEFT_POSE_PATTERN = re.compile(
        r"^\s*(\d+(?:\.\d+)?)-左-起手式\s*$"
    )
    # 选档基准 = 实测距离 - 0.03 m（手臂前伸量按比柜面近 3cm 的档位录制），
    # 再选数值最接近的已有档位；恰好位于两档中间时取较高档。
    POSE_MARGIN_M = 0.03

    def _apply_offset_keyframes_for_pose(self, pose: dict) -> None:
        """Resolve the task's base offset from the selected pose distance."""
        if not self.target_offset_keyframes:
            return
        distance = float(pose["min_distance_m"])
        offset, detail = interpolate_offset_keyframes(
            self.target_offset_keyframes,
            distance,
        )
        detail["preset_name"] = self.target_offset_preset_name
        changed = detail != self._target_offset_interpolation
        self.target_offset_wall_m = offset
        self._target_offset_interpolation = detail
        if changed:
            left = detail["left_distance_m"]
            right = detail["right_distance_m"]
            ratio = detail["ratio"]
            segment = (
                f"{left:.2f} m 边界值"
                if left == right
                else f"{left:.2f}→{right:.2f} m，比例 {ratio:.2f}"
            )
            self._log(
                f"距离偏移关键帧「{self.target_offset_preset_name}」："
                f"按起手式 {distance:.2f} m（{segment}）→ "
                f"右 {offset[0] * 1000:+.1f} / "
                f"上 {offset[2] * 1000:+.1f} / "
                f"入墙 {offset[1] * 1000:+.1f} mm"
            )

    def choose_opening_pose(self, distance_m: float) -> dict:
        """5️⃣ 按距离选起手式（已定规则，自动选，不问确认台）。

        向左拨只认「X.XX-起手式新」，向右拨只认「X.XX-左-起手式」。
        两组都按实测距离 - 0.03 m 四舍五入到最近已有档位；正好位于两档
        中间时取较高档。比该组最低档还近时
        → POSE_UNAVAILABLE。
        """
        seqs = (self.client.sequences().get("sequences") or [])
        rightward = self.flip_direction == "ltr"
        if self.pose_pattern is not None:
            # 能力注册表注入的正则优先（第 1 捕获组 = 档位距离 m）
            pattern = self.pose_pattern
            family = f"注册表正则 {pattern.pattern}"
        else:
            pattern = (self.LEFT_POSE_PATTERN if rightward
                       else self.NEW_POSE_PATTERN)
            family = "左-起手式" if rightward else "起手式新"
        poses: list[tuple[float, dict]] = []
        for s in seqs:
            m = pattern.match(str(s.get("name") or ""))
            if m:
                try:
                    poses.append((float(m.group(1)), s))
                except (TypeError, ValueError):
                    continue   # 注入正则的第 1 组不是数字 → 该序列不参与选档
        if not poses:
            example = "0.46-左-起手式" if rightward else "0.46-起手式新"
            raise FlowError(
                ErrorCode.POSE_UNAVAILABLE,
                f"没有任何「{family}」序列（如「{example}」）",
            )
        floor_thr = min(thr for thr, _ in poses)
        if distance_m < floor_thr:
            raise FlowError(
                ErrorCode.POSE_UNAVAILABLE,
                f"距柜面 {distance_m:.3f} m，小于最低起手式档位 {floor_thr} m"
                f"——距离太近，无可用起手式")
        target_thr = distance_m - self.POSE_MARGIN_M
        # 距离最近即四舍五入；第二关键字 -thr 让两档等距时选择较高档。
        # 最低档覆盖“实际距离已到最低档、但减去 3cm 后略低于最低档”的区间。
        if target_thr < floor_thr:
            candidates = [(thr, s) for thr, s in poses if thr == floor_thr]
        else:
            # 微小正偏置消除 0.455 这类二进制浮点表示误差，落实“五入”。
            rounding_target = target_thr + 1e-9
            nearest = min(
                poses,
                key=lambda item: (abs(item[0] - rounding_target), -item[0]),
            )[0]
            candidates = [(thr, s) for thr, s in poses if thr == nearest]
        thr, seq = candidates[0]
        endpoint_name = ""
        waypoints = seq.get("waypoints") or []
        if waypoints:
            endpoint_name = re.sub(
                r"_\d{8}_\d{6}\.json$",
                "",
                str(waypoints[-1]),
            )
        return {"name": seq["name"], "file": seq["file"],
                "manual": False, "min_distance_m": thr,
                "endpoint_name": endpoint_name}

    # 所有起手式序列都从这个已录路点起录。起点漂移 >0.5 rad 时服务端会
    # 重新规划（轨迹未经人工验证，还会覆盖文件里的录制），所以运行序列前
    # 先插值回录制起点，保证走"录播"路径。
    SEQ_START_WAYPOINT = "录制点位1"

    def apply_opening_pose(self, pose: dict) -> None:
        """把手臂摆到起手式：先插值回录制起点，再原样回放录制轨迹。"""
        if pose.get("manual"):
            self._need_console("起手式执行").confirm(
                "请手动把手臂摆到起手式，摆好后确认")
            return
        self._interp_to_waypoint(self.SEQ_START_WAYPOINT, "起手式起点",
                                 only_if_beyond_rad=0.4)
        self._arm_moved = True
        self._check_abort()
        res = self.client.run_sequence(pose["file"])
        if not res.get("ok"):
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"起手式序列启动失败: {res.get('error')}")
        if res.get("preview"):
            # preview 只剩一种可能：文件里还没有录制轨迹（全新序列的首次
            # 规划）。服务端已把它录进文件，再跑一次走录播执行。
            # （起点漂移如今是 409 报错，不会走到这里。）
            self._log(f"起手式「{pose['name']}」首次规划完成"
                      f"（{res.get('frames')} 帧，约 {res.get('duration_s')}s），"
                      f"继续执行")
            self._check_abort()
            res = self.client.run_sequence(pose["file"])
            if not res.get("ok") or res.get("preview"):
                raise FlowError(ErrorCode.EXEC_FAILED,
                                f"起手式回放失败: {res.get('error') or '仍在 preview'}")
        self._wait_exec(f"起手式「{pose['name']}」")

    # 取点 = 拨前状态框（flip_from）的固定相对偏移。40 个人工标注样本
    # （d 0.44~0.73m、yaw -16~+15°）实测 au/av 与距离和角度都无关
    # （残差 ±4px ≈ ±1.5mm）：手柄凸出带来的视差被框宽的透视缩放自动补偿了。
    # 样本来自实验室柜「就地」框；工厂柜用「远方」框套同一偏移——框的几何
    # 是同一个开关区域，首上工厂柜时建议手动模式核对一次取点落位。
    POINT_AU = 1.230   # u = x1 + au×框宽（>1 即框右缘外侧，手柄位置）
    POINT_AV = 0.543   # v = y1 + av×框高

    # 取点前只看机器人自身腰关节和IMU，不用柜面法向或距离参与稳定判定。
    WAIST_STABLE_WINDOW_S = 1.5
    WAIST_STABLE_SAMPLE_GAP_S = 0.25
    WAIST_STABLE_MAX_RANGE_DEG = 0.03
    IMU_STABLE_MAX_RANGE_DEG = 0.03
    WAIST_STABLE_TIMEOUT_S = 20.0

    def _wait_robot_stable(self) -> None:
        """腰关节和IMU同时稳定后才允许冻结 RGB-D。"""
        required = max(
            2,
            math.ceil(
                self.WAIST_STABLE_WINDOW_S / self.WAIST_STABLE_SAMPLE_GAP_S
            ) + 1,
        )
        waist_samples: deque[list[float]] = deque(maxlen=required)
        imu_samples: deque[list[float]] = deque(maxlen=required)
        waist_names: tuple[str, ...] = ()
        deadline = time.monotonic() + self.WAIST_STABLE_TIMEOUT_S
        last_waist_range_deg: float | None = None
        last_imu_range_deg: float | None = None
        self._log(
            f"等待机器人稳定：连续 {self.WAIST_STABLE_WINDOW_S:g}s "
            f"腰关节摆幅 ≤{self.WAIST_STABLE_MAX_RANGE_DEG:g}°，"
            f"IMU摆幅 ≤{self.IMU_STABLE_MAX_RANGE_DEG:g}°"
        )
        while True:
            state = self.client.torso()
            if not state.get("ok"):
                raise FlowError(
                    ErrorCode.PRECONDITION,
                    f"无法读取腰关节/IMU稳定状态: {state.get('error')}",
                )
            names = tuple(str(name) for name in state.get("waist_names") or [])
            positions = [
                float(value) for value in (state.get("waist_rad") or [])
            ]
            imu_rpy = [float(value) for value in (state.get("imu_rpy") or [])]
            if not names or len(names) != len(positions):
                raise FlowError(
                    ErrorCode.PRECONDITION,
                    "腰关节稳定状态不完整",
                )
            if len(imu_rpy) != 3:
                raise FlowError(
                    ErrorCode.PRECONDITION,
                    "IMU稳定状态不完整",
                )
            if not all(math.isfinite(value) for value in positions + imu_rpy):
                raise FlowError(
                    ErrorCode.PRECONDITION,
                    "腰关节/IMU稳定状态包含非法数值",
                )
            if names != waist_names:
                waist_names = names
                waist_samples.clear()
                imu_samples.clear()
            waist_samples.append(positions)
            imu_samples.append(imu_rpy)
            if len(waist_samples) == required:
                waist_ranges_deg = [
                    math.degrees(max(values) - min(values))
                    for values in zip(*waist_samples)
                ]
                # IMU角度可能跨过 ±π；相对窗口首帧展开后再计算摆幅。
                imu_ranges_deg = []
                for values in zip(*imu_samples):
                    reference = values[0]
                    unwrapped = [
                        reference + math.remainder(value - reference, 2 * math.pi)
                        for value in values
                    ]
                    imu_ranges_deg.append(
                        math.degrees(max(unwrapped) - min(unwrapped))
                    )
                last_waist_range_deg = max(waist_ranges_deg)
                last_imu_range_deg = max(imu_ranges_deg)
                if (
                    last_waist_range_deg <= self.WAIST_STABLE_MAX_RANGE_DEG
                    and last_imu_range_deg <= self.IMU_STABLE_MAX_RANGE_DEG
                ):
                    self._log(
                        f"机器人已稳定（{self.WAIST_STABLE_WINDOW_S:g}s："
                        f"腰关节最大摆幅 {last_waist_range_deg:.3f}°，"
                        f"IMU最大摆幅 {last_imu_range_deg:.3f}°），开始取点"
                    )
                    return
            if time.monotonic() >= deadline:
                detail = ""
                if (
                    last_waist_range_deg is not None
                    and last_imu_range_deg is not None
                ):
                    detail = (
                        f"，最近窗口腰关节/IMU最大摆幅 "
                        f"{last_waist_range_deg:.3f}°/"
                        f"{last_imu_range_deg:.3f}°"
                    )
                raise FlowError(
                    ErrorCode.ALIGN_FAILED,
                    f"等待机器人稳定超过 {self.WAIST_STABLE_TIMEOUT_S:g}s"
                    f"{detail}",
                )
            time.sleep(self.WAIST_STABLE_SAMPLE_GAP_S)

    def detect_points(self, round_no: int = 1) -> list[dict]:
        """7️⃣ 开关取点，三条路径按优先级：

        1. 7005 语义点云算法（默认）：冻结同帧 RGB-D → 墙面坐标系 →
           面板矩形中心（粉点）→ 0.2.0-s 模型偏移 → 目的点 → 叠加墙面系
           人工微调 → 18001 确认。返回含 p_root/plane 的完整目标，
           flip_switch 直接执行，不再走像素 /pick。
        2. 旧框偏移法（未配点云服务时）：YOLO 拨前状态框（flip_from）+
           固定相对像素偏移，返回像素点。
        3. 都没配 → 确认台人工点选。

        最多试 YOLO_ATTEMPTS 次；配了服务还是找不到 → 报 YOLO_FAILED
        退出（手臂由失败收尾受控回落），不转人工干等。
        """
        if self.pointcloud is not None:
            self._wait_robot_stable()
            return self._detect_points_pointcloud(round_no)
        if self.yolo is not None:
            self._wait_robot_stable()
            for i in range(1, self.YOLO_ATTEMPTS + 1):
                res = self.yolo.infer()
                boxes = ([b for b in res.get("boxes") or []
                          if b.get("name") == self.flip_from]
                         if res.get("ok") else [])
                if boxes:
                    b = max(boxes, key=lambda x: x["conf"])
                    x1, y1, x2, y2 = b["xyxy"]
                    u = int(round(x1 + self.POINT_AU * (x2 - x1)))
                    v = int(round(y1 + self.POINT_AV * (y2 - y1)))
                    self._log(f"YOLO 取点：「{self.flip_from}」框 conf {b['conf']}"
                              f"（共 {len(boxes)} 个，取最高，第 {i} 次尝试）"
                              f"→ ({u},{v})")
                    return [{"u": u, "v": v}]
                self._log(f"取点第 {i}/{self.YOLO_ATTEMPTS} 次没结果"
                          f"（{res.get('error') or f'画面里没有「{self.flip_from}」框'}）")
                if i < self.YOLO_ATTEMPTS:
                    time.sleep(self.YOLO_RETRY_WAIT_S)
            raise FlowError(ErrorCode.YOLO_FAILED,
                            f"取点失败：YOLO 连续 {self.YOLO_ATTEMPTS} 次都没找到"
                            f"「{self.flip_from}」框——检查开关是否在画面内、"
                            f"有无遮挡反光")
        pts = self._need_console("YOLO 点位识别").points(
            "请在相机画面上点击要拨动的开关点位（可多个），点完提交")
        if not pts:
            raise FlowError(ErrorCode.YOLO_FAILED, "没有点位")
        return pts

    @staticmethod
    def _points_brief(points: list[dict]) -> str:
        """点位的一行摘要（点云路径的完整结果太大，日志/确认台只放这个）。"""
        parts = []
        for pt in points:
            p_root = pt.get("p_root")
            if p_root and len(p_root) == 3:
                parts.append(f"p_root[{p_root[0]:+.3f}, {p_root[1]:+.3f}, "
                             f"{p_root[2]:+.3f}]m")
            else:
                parts.append(f"({pt.get('u')},{pt.get('v')})")
        return "；".join(parts)

    def _detect_points_pointcloud(self, round_no: int = 1) -> list[dict]:
        """7005 语义点云算法找点，与网页「算法找点1/3」同一条链路。"""
        last_err = ""
        for i in range(1, self.YOLO_ATTEMPTS + 1):
            picked, last_err = self._pointcloud_pick_once(i, round_no)
            if picked is not None:
                return [picked]
            self._log(f"点云找点第 {i}/{self.YOLO_ATTEMPTS} 次失败：{last_err}")
            if i < self.YOLO_ATTEMPTS:
                time.sleep(self.YOLO_RETRY_WAIT_S)
        raise FlowError(
            ErrorCode.YOLO_FAILED,
            f"取点失败：点云算法连续 {self.YOLO_ATTEMPTS} 次没算出目的点"
            f"（最后一次：{last_err}）——检查 7005 点云服务是否在跑、"
            f"画面有无遮挡反光")

    def _pointcloud_pick_once(
        self, attempt: int, round_no: int = 1
    ) -> tuple[dict | None, str]:
        """拍帧 → 算法找点 → 人工微调 → 18001 确认。返回 (picked, 错误)。"""
        cap = self.pointcloud.capture()
        if not cap.get("ok"):
            return None, f"拍帧失败: {cap.get('error')}"
        tgt = self.pointcloud.auto_target(cap["capture_id"])
        if not tgt.get("ok"):
            return None, f"算法找点失败: {tgt.get('error')}"
        name = str(tgt.get("matched_detection_name") or "")
        pc = tgt.get("panel_center_wall_m") or [0.0, 0.0, 0.0]
        tw = tgt.get("target_wall_m") or [0.0, 0.0, 0.0]
        self._log(f"算法找点（第 {attempt} 次）：识别「{name}」→ "
                  f"点{tgt.get('target_point_slot')}，粉点（面板中心）墙面系 "
                  f"[{pc[0]:+.3f}, {pc[1]:+.3f}, {pc[2]:+.3f}] m，"
                  f"模型偏移后目的点 [{tw[0]:+.3f}, {tw[1]:+.3f}, {tw[2]:+.3f}] m")
        if name != self.flip_from:
            try:
                archived = self.pointcloud.save_scene_mismatch(
                    cap["capture_id"],
                    {
                        "observed_scene": name or "未知",
                        "expected_scene": self.flip_from,
                        "site": self.site,
                        "flip_kind": self.flip_kind,
                        "direction": self.flip_direction,
                        "round": round_no,
                        "attempt": attempt,
                    },
                )
                if archived.get("ok"):
                    self._log(
                        f"类别不一致图像已保存为训练样本："
                        f"{archived.get('image')}"
                    )
                else:
                    self._log(
                        f"⚠ 类别不一致训练样本保存失败（不影响重试）："
                        f"{archived.get('error')}"
                    )
            except Exception as exc:
                self._log(
                    f"⚠ 类别不一致训练样本保存失败（不影响重试）：{exc}"
                )
            return (
                None,
                f"面板类别「{name or '未知'}」与任务拨前状态"
                f"「{self.flip_from}」不一致，拒绝按错误方向取点",
            )

        # 人工微调：墙面系 (x右, y入墙, z上) → 相机系向量，叠加在算好的
        # 目的点上；粉点→目的点的模型偏移保持原样。
        target_cam = [float(v) for v in tgt["target_camera_m"]]
        base_off = self.target_offset_wall_m
        first_off = (
            self.first_round_offset_wall_m
            if round_no == 1 else (0.0, 0.0, 0.0)
        )
        off = tuple(base_off[index] + first_off[index] for index in range(3))
        adj_cam = [0.0, 0.0, 0.0]
        if any(abs(v) > 1e-9 for v in off):
            axes = tgt.get("wall_axes_camera")
            if not axes or len(axes) != 3:
                return None, "算法结果缺墙面坐标轴，无法应用人工偏移"
            adj_cam = [sum(off[k] * float(axes[k][i]) for k in range(3))
                       for i in range(3)]
        self._log(
            f"基础偏置（墙面系）：右 {base_off[0] * 1000:+.1f} / "
            f"上 {base_off[2] * 1000:+.1f} / "
            f"入墙 {base_off[1] * 1000:+.1f} mm"
        )
        if round_no == 1:
            self._log(
                f"首轮额外偏置：右 {first_off[0] * 1000:+.1f} / "
                f"上 {first_off[2] * 1000:+.1f} / "
                f"入墙 {first_off[1] * 1000:+.1f} mm"
            )
        elif any(abs(v) > 1e-9 for v in self.first_round_offset_wall_m):
            self._log("首轮额外偏置本轮不应用")
        self._log(
            f"本轮合计偏置：右 {off[0] * 1000:+.1f} / "
            f"上 {off[2] * 1000:+.1f} / 入墙 {off[1] * 1000:+.1f} mm"
        )

        res = self.pointcloud.confirm(cap["capture_id"], {
            "p_camera": [target_cam[i] + adj_cam[i] for i in range(3)],
            "surface_reference_camera": target_cam,
            "adjustment_camera_m": adj_cam,
            # 墙面系原始微调量（mm），供 7005 选点记录按人看得懂的轴显示
            "adjustment_wall_mm": {"x": off[0] * 1000.0,
                                   "y": off[1] * 1000.0,
                                   "z": off[2] * 1000.0},
            "base_adjustment_wall_mm": {
                "x": base_off[0] * 1000.0,
                "y": base_off[1] * 1000.0,
                "z": base_off[2] * 1000.0,
            },
            "first_round_adjustment_wall_mm": {
                "x": first_off[0] * 1000.0,
                "y": first_off[1] * 1000.0,
                "z": first_off[2] * 1000.0,
            },
            "offset_interpolation": getattr(
                self, "_target_offset_interpolation", None
            ),
            "flow_round": round_no,
            "approach_offset_m": self.approach_offset_m,
            "selection_source": tgt.get("selection_source") or "flow-auto",
            "model_version": tgt.get("model_version"),
            "target_point_slot": tgt.get("target_point_slot"),
            "matched_detection_name": name or None,
        })
        if not res.get("ok"):
            return None, f"18001 确认目标失败: {res.get('error')}"
        p_root = res.get("p_root") or []
        if len(p_root) == 3:
            self._log(f"目的点已确认：p_root [{p_root[0]:+.3f}, "
                      f"{p_root[1]:+.3f}, {p_root[2]:+.3f}] m")
        if res.get("record"):
            self._log(f"选点记录已存档：{res['record']}"
                      f"（7005 页面 /picks 可回看截图与点云）")
        return res, ""

    # ---- 拨动证据：头部拍横移前/复核，右腕只拍横移前 ----
    # 存进本轮选点记录目录（data/pick_history/<record>/），与截图、点云
    # 同处一包，7005 /picks 和 web-picks 可回看。存档失败只记日志，
    # 绝不影响主流程。
    _PICK_HISTORY_DIR = PICK_HISTORY_DIR

    def _save_pick_flow_context(
        self,
        picked: dict,
        round_no: int,
        target_lift_m: float,
        effective_target_root_m: list[float],
    ) -> None:
        """Persist this round's distance, pose and every extra target lift."""
        record = picked.get("record")
        if not record:
            return
        pose = self._current_pose or {}
        opening_pose = {
            key: pose.get(key)
            for key in ("name", "file", "manual", "min_distance_m")
            if key in pose
        }
        base_offset = tuple(
            getattr(self, "target_offset_wall_m", (0.0, 0.0, 0.0))
        )
        configured_first_offset = tuple(
            getattr(self, "first_round_offset_wall_m", (0.0, 0.0, 0.0))
        )
        first_offset = (
            configured_first_offset
            if round_no == 1 else (0.0, 0.0, 0.0)
        )
        context = {
            "distance_m": self._measured_distance_m,
            "opening_pose": opening_pose,
            "round": round_no,
            "max_rounds": self.max_flip_rounds,
            "base_offset_wall_m": list(base_offset),
            "offset_interpolation": getattr(
                self, "_target_offset_interpolation", None
            ),
            "first_round_offset_wall_m": list(first_offset),
            "effective_offset_wall_m": [
                base_offset[index] + first_offset[index]
                for index in range(3)
            ],
            # 根坐标系目标上抬与墙面系取点偏置分开记录。
            "target_lift_m": target_lift_m,
            "lift_base_m": self.lift_base_m,
            "lift_step_m": self.lift_step_m,
            "lift_max_m": self.lift_max_m,
            "planner_mid_lift_m": self.lift_m,
            "approach_offset_m": self.approach_offset_m,
            "picked_target_root_m": [
                float(value) for value in (picked.get("p_root") or [])
            ],
            "effective_target_root_m": [
                float(value) for value in effective_target_root_m
            ],
        }
        try:
            path = save_pick_flow_context(
                record,
                context,
                history_dir=self._PICK_HISTORY_DIR,
            )
            self._log(f"本轮距离、起手式及附加偏移已存：{path}")
        except Exception as exc:
            self._log(f"⚠ 流程参数存档失败（不影响流程）: {exc}")

    def _flip_evidence_before(self) -> None:
        """横移前抓一帧：此刻开关应仍是拨前状态（手臂可能遮挡，识别可空）。"""
        if not self._last_pick_record:
            return
        if self.yolo is None:
            self._save_flip_evidence(
                "before",
                {"ok": False, "error": "未配置 YOLO 核验服务"},
            )
            return
        res = self.yolo.scene(include_image=True, include_wrist=True)
        if res.get("ok"):
            self._save_flip_evidence("before", res)
        else:
            self._save_flip_evidence("before", res)
            self._log(f"⚠ 拨动前证据抓帧失败（不影响流程）: {res.get('error')}")

    def _save_flip_evidence(self, stage: str, res: dict,
                            success: bool | None = None) -> None:
        record = self._last_pick_record
        if not record or not res:
            return
        try:
            saved = save_flip_evidence(
                record,
                stage,
                res,
                flip_from=self.flip_from,
                flip_to=self.flip_to,
                success=success,
                round_no=self._last_flip_round,
                history_dir=self._PICK_HISTORY_DIR,
            )
            if res.get("error"):
                self._log(
                    f"⚠ 拨动{'前' if stage == 'before' else '后'}核验失败已记录："
                    f"{record}/flip_result.json（{res['error']}）"
                )
                return
            suffix = ""
            if stage == "before":
                suffix = (" + flip_before_wrist.jpg"
                          if saved["wrist_saved"] else "（腕部图不可用）")
            self._log(f"拨动{'前' if stage == 'before' else '后'}证据已存："
                      f"{record}/flip_{stage}.jpg{suffix}")
        except Exception as exc:
            self._log(f"⚠ 拨动证据存档失败（不影响流程）: {exc}")

    VERIFY_SETTLE_S = 1.5   # 复核仍看到拨前状态时，等这么久再复看一眼

    def verify_flip(self) -> bool:
        """复核：拨完立即问 YOLO，flip_to = 成功，flip_from = 失败重试。

        看到拨前状态不立即判死：手可能正遮着开关、撤力回弹还没停，等一等
        再复看一眼，两次都是拨前状态才算失败（误判失败要白跑一整轮）。
        完全看不到开关（多次都没结论）→ 报 YOLO_FAILED 退出，别把"看不清"
        当成"没拨动"去重试；只有压根没配 YOLO 时才走确认台。
        判定用的那帧图和结论随手存进选点记录目录（flip_after.jpg）。
        """
        got = self._yolo_scene("复核", include_image=True)
        if got is not None:
            if got["scene"] == self.flip_to:
                self._save_flip_evidence("after", got, success=True)
                return True
            self._log(f"复核仍是「{got['scene']}」，等 {self.VERIFY_SETTLE_S}s "
                      f"排除手臂未停稳/遮挡后再看一眼")
            time.sleep(self.VERIFY_SETTLE_S)
            again = self._yolo_scene("复核（二次）", include_image=True)
            if again is not None:
                success = again["scene"] == self.flip_to
                self._save_flip_evidence("after", again, success=success)
                return success
            self._log(f"复核二次没结论，按第一次的「{got['scene']}」"
                      f"判为未拨动，走重试")
            self._save_flip_evidence("after", got, success=False)
            return False
        if self.yolo is not None:
            failed = dict(getattr(self, "_last_yolo_result", None) or {})
            failed["ok"] = False
            failed["error"] = (
                f"YOLO 连续 {self.YOLO_ATTEMPTS} 次都没识别到「就地/远方」"
            )
            self._save_flip_evidence("after", failed)
            raise FlowError(ErrorCode.YOLO_FAILED,
                            f"复核失败：YOLO 连续 {self.YOLO_ATTEMPTS} 次都没识别到"
                            f"「就地/远方」，无法判定拨动结果——开关是否被手臂"
                            f"遮住或已移出画面？")
        return self._need_console("拨动复核").yesno(
            "复核（YOLO 无结论）：开关拨动成功了吗？")

    DESCEND_WAYPOINT = "起手点测试"   # 复核成功后插值回落到这个已录路点
    DESCEND_SPEED_RAD_S = 0.6         # 收尾回落比常规插值快一倍

    def _is_left_start_pose(self, pose: dict | None = None) -> bool:
        """是否为需要经配套终点避开柜面的「X.XX-左-起手式」。"""
        selected = pose or self._current_pose or {}
        return bool(self.LEFT_POSE_PATTERN.match(
            str(selected.get("name") or "").strip()
        ))

    def _descend_to_safe_waypoint(
        self, pose: dict | None, tag: str
    ) -> None:
        """按起手式选择安全收尾路径；本方法不释放手臂。"""
        if self._is_left_start_pose(pose):
            selected = pose or self._current_pose or {}
            endpoint_name = self._pose_endpoint_name(selected)
            self._log(
                f"{tag}使用左-起手式安全路径：先回「{endpoint_name}」，"
                f"再到「{self.DESCEND_WAYPOINT}」"
            )
            self._interp_to_waypoint(endpoint_name, f"{tag}第一段")
        self._interp_to_waypoint(
            self.DESCEND_WAYPOINT,
            tag,
            speed_rad_s=self.DESCEND_SPEED_RAD_S,
        )

    def descend_fast(self, pose: dict | None) -> None:
        """安全收尾到「起手点测试」；左-起手式先回配套终点。"""
        self._descend_to_safe_waypoint(pose, "收尾")
        self._log("释放手臂")
        res = self.client.disarm()
        if not res.get("ok"):
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"释放手臂失败: {res.get('error')}")

    def _descend_on_failure(self) -> None:
        """失败收尾：按起手式安全回落，再按接管来源决定是否释放。

        不回落的话手臂会停在柜面前（拨动结束的姿态），随 reach_server 退出
        做 1s 权重渐出交还本体控制器——那一刻姿态不受我们控制，有下坠风险。
        全程只尽最大努力：回落或释放自身的失败只记日志，绝不抛出，否则
        真正的失败原因会被收尾异常盖掉。
        """
        if not self._arm_moved:
            self._release_if_flow_armed()
            return
        self._log("═══ 失败收尾：受控回落 ═══")
        try:
            self._descend_to_safe_waypoint(self._current_pose, "失败收尾")
        except Exception as exc:
            # 没收回来就别主动松手：保持刚性，交给人处置
            self._log(f"⚠ 回落失败，手臂停在半空且保持接管，请人工扶住后处置: {exc}")
            return
        if not self._armed_by_flow:
            self._log("手臂是进流程前就接管的，保持接管不释放")
            return
        try:
            res = self.client.disarm()
            self._log("释放手臂" if res.get("ok")
                      else f"⚠ 释放手臂失败: {res.get('error')}")
        except Exception as exc:
            self._log(f"⚠ 释放手臂异常: {exc}")

    # ---------------------------------------------------------------- 工具

    def _done(self, t0: float, message: str, **detail: Any) -> FlowResult:
        self._step_finish()
        self._log(f"✔ {message}")
        self._log_step_summary()
        return FlowResult(ok=True, code=ErrorCode.OK, message=message,
                          detail={"elapsed_s": round(time.monotonic() - t0, 1),
                                  **detail})

    def _log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] [flow] {msg}"
        print(line, flush=True)
        self.log_lines.append(line)
