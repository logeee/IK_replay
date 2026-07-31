"""拨动开关全流程编排。

已部署的步骤（测平面/测距、腰部对齐、pick→规划→执行）直接走 reach_server；
起手式按距离自动选（序列名带门槛，如「0.44避障起手式」需 ≥0.44m，太近报
POSE_UNAVAILABLE）；取点前的补位分三档：≥0.5m 起手式后加摆「0.5以上」、
0.46~0.5m 摆完直接取点、0.44~0.46m 摆完补位到配套「终点」路点；收尾 =
插值到「起手点测试」路点后释放手臂——成功和失败（含重试耗尽）都走这个
回落，避免手臂停在柜面前被权重渐出交还本体。
场景判断和拨后复核走 7004 YOLO 服务（python -m api.yolo_server）：
每处视觉判断连问 3 帧再下结论；配了 YOLO 却仍没结论时报 YOLO_FAILED
退出（手臂受控回落），不转人工——无人值守的自动化不能卡在等人上。
本 API 专做「就地 → 远方」——开始前识别「就地」= 要拨、「远方」= 无需拨；
拨完识别「远方」= 成功、「就地」= 失败重试。
点位识别 = 「就地」框 + 固定相对偏移（标注数据实测与距离/角度无关）。
只有压根没配 YOLO（--no-yolo 手动模式）才把视觉判断转 7002 人工确认台。

不给 console 时保持旧行为：人工顶不上的步骤抛 FlowError(NOT_IMPLEMENTED)。

腰部对齐目标（按 2026-07-28 流程定义）：
  3️⃣ 粗对齐：平面指数（yaw）收进 -3 ~ -6°（target -4.5° ± 1.5°）
  6️⃣ 细保持：抬手后收进 -3° ± 2°。手臂前伸会把躯干配平带偏 +4.5~+8.2°（实测），
     所以这一步必须转身纠偏；判据取 3 帧中位数防单帧污染，服务端在抬手状态下
     限死单杆 ≤5°、累计 ≤15°，并有三道安全闸（见 adapters/reach.py）

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
    POSE_UNAVAILABLE = 10  # 距离不满足任何起手式的适用范围（如 <0.44m）


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


class SwitchFlow:
    """一键开始 → … → 拨动完成 的整条流程。

    未部署步骤的临时实现走 console（7002 确认台）；正式实现就绪后
    覆写对应方法（detect_scene / detect_points / verify_flip / …）即可。
    """

    def __init__(self,
                 client: ReachClient | None = None,
                 console: ConsoleClient | None = None,
                 yolo: YoloClient | None = None,
                 coarse_target_deg: float = -4.5,  # 3️⃣ 粗对齐目标（-3~-6 带中心）
                 coarse_tol_deg: float = 1.5,      # 3️⃣ 带半宽 → [-6, -3]
                 fine_target_deg: float = -3.0,    # 6️⃣ 保持目标
                 fine_tol_deg: float = 2.0,        # 6️⃣ 带半宽 → [-5, -1]
                 align_mode: str = "hold",         # "hold"=新对中（打杆式）
                 dmin: float = 0.4, dmax: float = 1.0,
                 # ---- IK 拨动段参数（index.html 真机验证过的一组）----
                 approach_offset_m: float = 0.0,   # 接近偏移：0 = 指尖顶到表面
                 reach_duration_s: float = 6.0,    # 主段（到位）时长
                 sidestep_cm: float = 6.0,         # 到位后沿柜面左移（负=右移）
                 push_force_n: float = 25.0,       # 横移时的前馈推力
                 lift_m: float = 0.02,             # 规划中段抬高 2cm（防刮底）
                 endpoint_speed_rad_s: float = 0.3,  # 插值回「终点」路点的关节限速
                 max_flip_rounds: int = 3,         # 拨动失败回到 5️⃣ 的最大轮数
                 align_timeout_s: float = 90.0,
                 exec_timeout_s: float = 120.0):
        self.client = client or ReachClient()
        self.console = console
        self.yolo = yolo
        self.coarse_target_deg = coarse_target_deg
        self.coarse_tol_deg = coarse_tol_deg
        self.fine_target_deg = fine_target_deg
        self.fine_tol_deg = fine_tol_deg
        self.align_mode = align_mode
        self.dmin = dmin
        self.dmax = dmax
        self.approach_offset_m = approach_offset_m
        self.reach_duration_s = reach_duration_s
        self.sidestep_cm = sidestep_cm
        self.push_force_n = push_force_n
        self.lift_m = lift_m
        self.endpoint_speed_rad_s = endpoint_speed_rad_s
        self.max_flip_rounds = max_flip_rounds
        self.align_timeout_s = align_timeout_s
        self.exec_timeout_s = exec_timeout_s
        self._current_pose: dict | None = None
        self._armed_by_flow = False   # 手臂是流程接管的（而非用户本来就接管着）
        self._arm_moved = False       # 已下发过手臂动作 → 失败时要先受控回落
        self.log_lines: deque[str] = deque(maxlen=300)   # 供调度服务透出进度
        # 强制停止开关：外部（/emergency/stop）置位后，流程在最近的检查点退出。
        # 置位时手臂多半已被强停端点直接释放了，所以退出路径不再做受控回落。
        self.abort = threading.Event()

    def request_abort(self) -> None:
        self.abort.set()

    def _check_abort(self) -> None:
        if self.abort.is_set():
            raise FlowError(ErrorCode.ABORTED, "收到强制停止")

    # ------------------------------------------------------------------ 主流程

    def run(self) -> FlowResult:
        """1️⃣ 一键开始。任何一步失败即返回，携带占位错误码。"""
        t0 = time.monotonic()
        try:
            self._log("═══ 1️⃣ 一键开始 ═══")
            self._preflight()

            self._log("═══ 2️⃣ 场景判断（是否需要拨动、往哪个方向）═══")
            scene = self.detect_scene()
            if not scene.get("need_flip", True):
                self._release_if_flow_armed()
                return self._done(t0, "无需拨动，流程结束", scene=scene)
            if scene.get("direction") == "ltr":
                self._release_if_flow_armed()
                raise FlowError(ErrorCode.NOT_IMPLEMENTED,
                                "「从左向右」拨动暂未支持（只验证过从右向左），流程退出")
            self._log(f"场景: {scene}")

            self._log(f"═══ 3️⃣ 腰部粗对齐：平面指数收进 "
                      f"{self.coarse_target_deg:+.1f}°±{self.coarse_tol_deg}° "
                      f"（即 -6°~-3°）═══")
            self._coarse_align_with_retry()

            self._log("═══ 4️⃣ 测距离 ═══")
            distance_m = self.measure_distance()
            self._log(f"距柜面 {distance_m:.3f} m")

            last_error: FlowError | None = None
            for round_no in range(1, self.max_flip_rounds + 1):
                self._check_abort()
                self._log(f"═══ 5️⃣ 第 {round_no}/{self.max_flip_rounds} 轮 ═══")
                try:
                    pose = self.choose_opening_pose(distance_m)
                    self._current_pose = pose   # 拨完插值回它配套的「终点」路点
                    self._log(f"起手式: {pose}")
                    far = distance_m >= self.FAR_DISTANCE_M
                    near = distance_m < self.NEAR_DISTANCE_M
                    if round_no == 1:
                        self.apply_opening_pose(pose)
                        if far:
                            self._log(f"距柜面 {distance_m:.3f} m ≥ "
                                      f"{self.FAR_DISTANCE_M} m，起手式后加摆"
                                      f"「{self.FAR_EXTRA_WAYPOINT}」")
                            self._interp_to_waypoint(self.FAR_EXTRA_WAYPOINT,
                                                     f"远距补位第{round_no}轮")
                        elif near:
                            self._log(f"距柜面 {distance_m:.3f} m < "
                                      f"{self.NEAR_DISTANCE_M} m，起手式后补位到"
                                      f"配套「终点」路点再取点")
                            self._goto_endpoint(f"近距补位第{round_no}轮")
                    elif far:
                        # 远距离重试：起手位就是「0.5以上」，不必先绕回「终点」
                        self._log(f"重试轮：直接插值回「{self.FAR_EXTRA_WAYPOINT}」"
                                  f"作为起手位")
                        self._interp_to_waypoint(self.FAR_EXTRA_WAYPOINT,
                                                 f"重试第{round_no}轮")
                    else:
                        # 重试轮：上一轮结束时手臂已在「终点」高位附近，直接
                        # 插值回终点路点即可——回放整条起手式会让手下去再上来，
                        # 且起点漂移触发的重规划轨迹未经人工验证
                        self._log("重试轮：跳过起手式回放，插值回终点路点作为起手位")
                        self._goto_endpoint(f"重试第{round_no}轮")

                    self._log(f"═══ 6️⃣ 腰部细对齐并保持："
                              f"{self.fine_target_deg:+.1f}°±{self.fine_tol_deg}° ═══")
                    self._fine_align_with_retry()

                    points = self._detect_points_held()
                    self._log(f"点位: {points}")

                    self._log("IK 执行拨动")
                    self.flip_switch(points)

                    self._log("复核拨动结果")
                    if self.verify_flip():
                        self._log("拨动成功 ✔")
                        self._log("═══ 收尾：快速回落 ═══")
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
            self._log("✘ 操作员在确认台中止了流程")
            try:
                self.client.stop()
            except Exception:
                pass
            return FlowResult(ok=False, code=ErrorCode.ABORTED,
                              message="确认台人工中止",
                              detail={"elapsed_s": round(time.monotonic() - t0, 1)})
        except FlowError as exc:
            self._log(f"✘ 流程中止：[{exc.code.name}] {exc.message}")
            if self.abort.is_set():
                # 强制停止：手臂已由 /emergency/stop 急停并释放，这里绝不能
                # 再下发回落动作——那等于在"已经放手"之后又去动机器人
                self._log("强制停止：不做回落，手臂控制权已交还本体")
            else:
                self._descend_on_failure()
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

    def waist_align(self, target_deg: float, tol_deg: float,
                    cmd_tol_deg: float | None = None) -> None:
        """腰部调节：把平面指数收进 target_deg ± tol_deg（真机转身）。

        3️⃣ 抬手前和 6️⃣ 抬手后都用它；抬手后服务端会自动限幅（单杆 ≤5°、
        累计 ≤15°）。cmd_tol_deg：发给服务器的收敛阈值（默认同 tol_deg），
        必须比验收带更严，否则服务器停在带边缘、流程独立复测的噪声就会判失败。
        """
        yaw = float(self.measure_plane()["yaw_err_deg"])
        if abs(yaw - target_deg) <= tol_deg:
            self._log(f"平面指数已在带内（yaw {yaw:+.2f}°，"
                      f"目标 {target_deg:+.1f}°±{tol_deg}°），跳过")
            return
        res = self.client.align_yaw_start(self.dmin, self.dmax,
                                          tol_deg=cmd_tol_deg or tol_deg,
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
                if err is not None and err <= tol_deg:
                    return
                raise FlowError(ErrorCode.ALIGN_FAILED,
                                f"对中结束但复测残差 {shown} 未达 ±{tol_deg}°")
            msg = align.get("message") or ""
            if msg != last_msg:          # 每秒轮询，同一杆别重复刷屏
                self._log(f"对中中… {msg}")
                last_msg = msg
        self.client.align_yaw_stop()
        raise FlowError(ErrorCode.ALIGN_FAILED, f"对中超时（>{self.align_timeout_s}s）")

    COARSE_ALIGN_ATTEMPTS = 3

    def _coarse_align_with_retry(self) -> None:
        """3️⃣ 粗对齐：收进 -4.5°±1.5°（即 -6~-3°），未达标原地重试。

        发给服务器的收敛阈值取验收半宽的一半：服务器要是停在验收带边缘，流程
        用另一帧独立复测（噪声 ±0.2°）就可能量到带外——2026-07-30 18:08 的任务
        就是这么挂的（服务器报残差 1.46° 完成，流程复测 1.57° 判 ALIGN_FAILED）。
        """
        for i in range(1, self.COARSE_ALIGN_ATTEMPTS + 1):
            try:
                self.waist_align(self.coarse_target_deg, self.coarse_tol_deg,
                                 cmd_tol_deg=self.coarse_tol_deg / 2)
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

        手臂前伸会被整机配平带着把躯干转过去，实测漂移 +4.5~+8.2°（越往前伸
        越大，摆过「0.5以上」时最大），方向固定往正，所以这一步必须纠——不纠
        任务永远过不去。历史上一杆约 3° 就够。
        判据用 3 帧中位数（防单帧污染），纠偏由服务器闭环做：抬手状态下服务端
        限死单杆 ≤5°、累计 ≤15°，另有拟合点数、偏差上限、运控无响应三道闸
        （见 adapters/reach.py），不会再出现对着柜面空转的情况。
        """
        band = f"{self.fine_target_deg:+.1f}°±{self.fine_tol_deg}°"
        yaw = 0.0
        for i in range(1, attempts + 1):
            yaw = self._fine_yaw("6️⃣ 抬手后复查")
            if abs(yaw - self.fine_target_deg) <= self.fine_tol_deg:
                self._log(f"在保持带 {band} 内")
                return
            self._log(f"抬手后漂出保持带 {band}，转身纠偏"
                      f"（第 {i}/{attempts} 次）")
            try:
                self.waist_align(self.fine_target_deg, self.fine_tol_deg,
                                 cmd_tol_deg=self.fine_tol_deg / 2)
            except FlowError as exc:
                if exc.code != ErrorCode.ALIGN_FAILED or i == attempts:
                    raise
                self._log(f"纠偏未达标（{exc.message}），再试一次")
        raise FlowError(ErrorCode.ALIGN_FAILED,
                        f"抬手后 {attempts} 次纠偏仍在 {yaw:+.2f}°，"
                        f"未收进保持带 {band}，手臂将受控回落")

    def _detect_points_held(self) -> list[dict]:
        """取点前后都守住保持带：取点期间漂出就重新纠偏，再重新取点。"""
        for attempt in (1, 2):
            points = self.detect_points()
            yaw = self._fine_yaw("取点后复查")
            if abs(yaw - self.fine_target_deg) <= self.fine_tol_deg:
                return points
            self._log(f"取点期间漂出保持带（yaw {yaw:+.2f}°），"
                      f"重新纠偏后重新取点（第 {attempt} 次）")
            self._fine_align_with_retry(attempts=2)
        return self.detect_points()

    # 横移方向 = 拟合平面的"左"再向下倾 2°（同 main.js SIDESTEP_TILT_DEG）
    SIDESTEP_TILT_DEG = 2.0
    SIDESTEP_PUSH_SPEED = 0.06   # 带推力时快拨（m/s）：借冲量越过定位卡点

    def flip_switch(self, points: list[dict]) -> None:
        """IK 执行拨动：

          取点（接近偏移 0）→ 左侧规划（中段抬高 2cm）→
          主段到位（6s）→ 沿柜面左移 6cm + 前馈推力 25N（快拨 1s）

        拨完就地停住直接交给复核（拨动本身不要求到点精度，不再先插值回
        「终点」路点）：成功 → 收尾直接回「起手点测试」；失败 → 重试轮
        先插值回终点路点当起手位。规划就绪后直接真机执行，不经确认台。
        """
        for i, pt in enumerate(points, 1):
            u, v = int(pt["u"]), int(pt["v"])
            tag = f"点位 {i}/{len(points)} ({u},{v})"

            picked = self.client.pick(u, v,
                                      approach_offset_m=self.approach_offset_m)
            if not picked.get("ok"):
                raise FlowError(ErrorCode.IK_FAILED,
                                f"{tag} 取点失败: {picked.get('error')}")

            joints = self.client.joints()
            if not joints.get("ok"):
                raise FlowError(ErrorCode.PRECONDITION,
                                f"读不到关节: {joints.get('error')}")
            # 不做碰撞检查：目标点本来就贴着柜面，指尖终点必然挨着"墙"，
            # 碰撞标注全是误报（调试页对主段也只提示不拦）
            plan = self.client.plan_axis_last(joints["named_joints"],
                                              picked["p_root"],
                                              lift_m=self.lift_m,
                                              check_collision=False)
            if not plan.get("ok"):
                raise FlowError(ErrorCode.IK_FAILED,
                                f"{tag} 规划失败: {plan.get('error')}")
            frames = plan["waypoints"]
            self._log(f"{tag} 预演就绪：{len(frames)} 路点，"
                      f"IK 误差 {plan.get('max_ik_error_mm')}mm")

            self._arm_moved = True
            res = self.client.execute(
                waypoints=[f["named_joints"] for f in frames],
                duration=self.reach_duration_s, label="flow_reach")
            if not res.get("ok"):
                raise FlowError(ErrorCode.EXEC_FAILED,
                                f"{tag} 到位执行被拒: {res.get('error')}")
            self._wait_exec(f"{tag} 到位")

            self._sidestep_flick(picked, tag)

    def _sidestep_flick(self, picked: dict, tag: str) -> None:
        """到位后的拨动本体：按真机实际姿态就地规划横移，带前馈推力执行。"""
        if abs(self.sidestep_cm) < 0.5:
            return
        left = (picked.get("plane") or {}).get("left_root")
        if not left:
            raise FlowError(ErrorCode.IK_FAILED,
                            f"{tag} 表面平面拟合失败，定不出左移方向")
        sg = 1.0 if self.sidestep_cm > 0 else -1.0
        t = math.radians(self.SIDESTEP_TILT_DEG)
        c, s = math.cos(t), math.sin(t)
        # 先取实际移动方向（±左），再往下倾：右移时同样是"偏下"
        direction = [left[0] * sg * c, left[1] * sg * c, left[2] * sg * c - s]
        dist = abs(self.sidestep_cm) / 100.0

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
        res = self.client.execute(**body)
        if not res.get("ok"):
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"{tag} 横移执行被拒: {res.get('error')}")
        self._wait_exec(f"{tag} 拨动（左移+推力）")

    def _goto_endpoint(self, tag: str) -> None:
        """拨完后关节插值回起手式配套的「终点」路点（如 0.46起手式 → 0.46终点）。"""
        pose = self._current_pose or {}
        m = re.match(r"\s*(\d+(?:\.\d+)?)", str(pose.get("name") or ""))
        if not m:
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"{tag} 起手式「{pose.get('name')}」没有距离前缀，"
                            f"配不出终点路点名")
        self._interp_to_waypoint(f"{m.group(1)}终点", tag)

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

    def _yolo_scene(self, tag: str) -> dict | None:
        """问 YOLO 服务当前是就地还是远方，最多问 YOLO_ATTEMPTS 次。

        返回 {"scene": "就地"|"远方", "conf": ...}；没配 YOLO、服务不可达
        或每次都没识别到 → 返回 None。
        """
        if self.yolo is None:
            return None
        for i in range(1, self.YOLO_ATTEMPTS + 1):
            res = self.yolo.scene()
            if res.get("ok") and res.get("scene") in ("就地", "远方"):
                self._log(f"{tag}：YOLO 识别为「{res['scene']}」"
                          f"（置信度 {res.get('conf')}，第 {i} 次尝试）")
                return {"scene": res["scene"], "conf": res.get("conf")}
            self._log(f"{tag}：第 {i}/{self.YOLO_ATTEMPTS} 次没结论"
                      f"（{res.get('error') or '画面里没识别到就地/远方'}）")
            if i < self.YOLO_ATTEMPTS:
                time.sleep(self.YOLO_RETRY_WAIT_S)
        return None

    def detect_scene(self) -> dict:
        """2️⃣ 是否需要拨动。本 API 专做「就地 → 远方」：

        YOLO 识别「就地」→ 需要拨（方向即验证过的从右向左）；
        「远方」→ 已在目标位，无需拨动直接结束。
        配了 YOLO 但多次都没结论 → 报 YOLO_FAILED 退出（不转人工，否则
        无人值守的自动化会卡在等人上）。只有压根没配 YOLO 时才走确认台。
        """
        got = self._yolo_scene("2️⃣ 场景判断")
        if got is not None:
            if got["scene"] == "远方":
                return {"need_flip": False, "source": "yolo",
                        "conf": got["conf"]}
            return {"need_flip": True, "direction": "rtl", "source": "yolo",
                    "conf": got["conf"]}
        if self.yolo is not None:
            raise FlowError(ErrorCode.YOLO_FAILED,
                            f"场景判断失败：YOLO 连续 {self.YOLO_ATTEMPTS} 次都没"
                            f"识别到「就地/远方」——检查画面是否被遮挡、反光，"
                            f"或机器人是否正对柜面")
        answer = self._need_console("YOLO 场景判断").choice(
            "2️⃣ 场景判断（YOLO 无结论，请看相机画面人工判断）\n"
            "开关需要拨动吗？往哪个方向拨？",
            ["需要：从右向左", "需要：从左向右", "无需拨动"])
        if answer == "无需拨动":
            return {"need_flip": False, "source": "console"}
        return {"need_flip": True,
                "direction": "rtl" if "从右向左" in answer else "ltr",
                "source": "console"}

    def choose_opening_pose(self, distance_m: float) -> dict:
        """5️⃣ 按距离选起手式（已定规则，自动选，不问确认台）。

        起手式 = 已存的动作序列，名字开头的数字是它的最小适用距离：
        「0.44避障起手式」→ 距柜面 ≥ 0.44 m 才能用。多个够格时选门槛最大
        （最贴近当前距离）的那个；一个都不够格 → POSE_UNAVAILABLE。
        现有两档：0.44（0.44~0.46 m）和 0.46（≥0.46 m）。
        """
        seqs = (self.client.sequences().get("sequences") or [])
        poses: list[tuple[float, dict]] = []
        for s in seqs:
            m = re.match(r"\s*(\d+(?:\.\d+)?)", str(s.get("name") or ""))
            if m:
                poses.append((float(m.group(1)), s))
        if not poses:
            raise FlowError(ErrorCode.POSE_UNAVAILABLE,
                            "没有任何名字带距离门槛的起手式序列（如「0.46起手式」）")
        usable = [(thr, s) for thr, s in poses if distance_m >= thr]
        if not usable:
            nearest = min(thr for thr, _ in poses)
            raise FlowError(
                ErrorCode.POSE_UNAVAILABLE,
                f"距柜面 {distance_m:.3f} m，小于最近的起手式门槛 {nearest} m"
                f"——距离太近，无可用起手式")
        thr, seq = max(usable, key=lambda p: p[0])
        return {"name": seq["name"], "file": seq["file"],
                "manual": False, "min_distance_m": thr}

    # 所有起手式序列都从这个已录路点起录。起点漂移 >0.5 rad 时服务端会
    # 重新规划（轨迹未经人工验证，还会覆盖文件里的录制），所以运行序列前
    # 先插值回录制起点，保证走"录播"路径。
    SEQ_START_WAYPOINT = "录制点位1"

    # 起手式之后、取点之前的补位，按距柜面距离分三档：
    #   ≥0.50 m：起手式摆完再插值到「0.5以上」（手臂前伸更多）；重试轮直接
    #            回这个路点当起手位，不绕经「终点」
    #   0.46~0.50 m：起手式摆完直接取点
    #   0.44~0.46 m：起手式（「0.44避障起手式」）摆完再补位到配套的「0.44终点」
    FAR_DISTANCE_M = 0.5
    FAR_EXTRA_WAYPOINT = "0.5以上"
    NEAR_DISTANCE_M = 0.46      # 小于此为近距档，起手式后补位到「终点」再取点

    def apply_opening_pose(self, pose: dict) -> None:
        """把手臂摆到起手式：先插值回录制起点，再原样回放录制轨迹。"""
        if pose.get("manual"):
            self._need_console("起手式执行").confirm(
                "请手动把手臂摆到起手式，摆好后确认")
            return
        self._interp_to_waypoint(self.SEQ_START_WAYPOINT, "起手式起点",
                                 only_if_beyond_rad=0.4)
        self._arm_moved = True
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
            res = self.client.run_sequence(pose["file"])
            if not res.get("ok") or res.get("preview"):
                raise FlowError(ErrorCode.EXEC_FAILED,
                                f"起手式回放失败: {res.get('error') or '仍在 preview'}")
        self._wait_exec(f"起手式「{pose['name']}」")

    # 取点 = 「就地」框的固定相对偏移。40 个人工标注样本（d 0.44~0.73m、
    # yaw -16~+15°）实测 au/av 与距离和角度都无关（残差 ±4px ≈ ±1.5mm）：
    # 手柄凸出带来的视差被框宽的透视缩放自动补偿了。
    POINT_AU = 1.230   # u = x1 + au×框宽（>1 即框右缘外侧，手柄位置）
    POINT_AV = 0.543   # v = y1 + av×框高

    DETECT_SETTLE_S = 2.0   # 到位/对齐后腰部还在自平衡，等它稳住再取点

    def detect_points(self) -> list[dict]:
        """开关点位（像素坐标）：YOLO「就地」框 + 固定相对偏移。

        最多试 YOLO_ATTEMPTS 次；配了 YOLO 还是找不到「就地」框 → 报
        YOLO_FAILED 退出（手臂由失败收尾受控回落），不转人工干等。
        """
        if self.yolo is not None:
            self._log(f"等 {self.DETECT_SETTLE_S:.0f}s 让躯干自平衡稳定后取点")
            time.sleep(self.DETECT_SETTLE_S)
            for i in range(1, self.YOLO_ATTEMPTS + 1):
                res = self.yolo.infer()
                boxes = ([b for b in res.get("boxes") or []
                          if b.get("name") == "就地"] if res.get("ok") else [])
                if boxes:
                    b = max(boxes, key=lambda x: x["conf"])
                    x1, y1, x2, y2 = b["xyxy"]
                    u = int(round(x1 + self.POINT_AU * (x2 - x1)))
                    v = int(round(y1 + self.POINT_AV * (y2 - y1)))
                    self._log(f"YOLO 取点：「就地」框 conf {b['conf']}"
                              f"（共 {len(boxes)} 个，取最高，第 {i} 次尝试）"
                              f"→ ({u},{v})")
                    return [{"u": u, "v": v}]
                self._log(f"取点第 {i}/{self.YOLO_ATTEMPTS} 次没结果"
                          f"（{res.get('error') or '画面里没有「就地」框'}）")
                if i < self.YOLO_ATTEMPTS:
                    time.sleep(self.YOLO_RETRY_WAIT_S)
            raise FlowError(ErrorCode.YOLO_FAILED,
                            f"取点失败：YOLO 连续 {self.YOLO_ATTEMPTS} 次都没找到"
                            f"「就地」框——检查开关是否在画面内、有无遮挡反光")
        pts = self._need_console("YOLO 点位识别").points(
            "请在相机画面上点击要拨动的开关点位（可多个），点完提交")
        if not pts:
            raise FlowError(ErrorCode.YOLO_FAILED, "没有点位")
        return pts

    VERIFY_SETTLE_S = 1.5   # 复核看到「就地」时，等这么久再复看一眼

    def verify_flip(self) -> bool:
        """复核：拨完立即问 YOLO，「远方」= 成功，「就地」= 失败重试。

        看到「就地」不立即判死：手可能正遮着开关、撤力回弹还没停，等一等
        再复看一眼，两次都是「就地」才算失败（误判失败要白跑一整轮）。
        完全看不到开关（多次都没结论）→ 报 YOLO_FAILED 退出，别把"看不清"
        当成"没拨动"去重试；只有压根没配 YOLO 时才走确认台。
        """
        got = self._yolo_scene("复核")
        if got is not None:
            if got["scene"] == "远方":
                return True
            self._log(f"复核看到「就地」，等 {self.VERIFY_SETTLE_S}s "
                      f"排除手臂未停稳/遮挡后再看一眼")
            time.sleep(self.VERIFY_SETTLE_S)
            again = self._yolo_scene("复核（二次）")
            if again is not None:
                return again["scene"] == "远方"
            self._log("复核二次没结论，按第一次的「就地」判为未拨动，走重试")
            return False
        if self.yolo is not None:
            raise FlowError(ErrorCode.YOLO_FAILED,
                            f"复核失败：YOLO 连续 {self.YOLO_ATTEMPTS} 次都没识别到"
                            f"「就地/远方」，无法判定拨动结果——开关是否被手臂"
                            f"遮住或已移出画面？")
        return self._need_console("拨动复核").yesno(
            "复核（YOLO 无结论）：开关拨动成功了吗？")

    DESCEND_WAYPOINT = "起手点测试"   # 复核成功后插值回落到这个已录路点
    DESCEND_SPEED_RAD_S = 0.6         # 收尾回落比常规插值快一倍

    def descend_fast(self, pose: dict | None) -> None:
        """收尾：快速关节插值到「起手点测试」路点，到位立即释放手臂。"""
        self._interp_to_waypoint(self.DESCEND_WAYPOINT, "收尾",
                                 speed_rad_s=self.DESCEND_SPEED_RAD_S)
        self._log("释放手臂")
        res = self.client.disarm()
        if not res.get("ok"):
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"释放手臂失败: {res.get('error')}")

    def _descend_on_failure(self) -> None:
        """失败收尾：手臂动过就先回落到「起手点测试」，再按接管来源决定释放。

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
            self._interp_to_waypoint(self.DESCEND_WAYPOINT, "失败收尾",
                                     speed_rad_s=self.DESCEND_SPEED_RAD_S)
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
        self._log(f"✔ {message}")
        return FlowResult(ok=True, code=ErrorCode.OK, message=message,
                          detail={"elapsed_s": round(time.monotonic() - t0, 1),
                                  **detail})

    def _log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] [flow] {msg}"
        print(line, flush=True)
        self.log_lines.append(line)
