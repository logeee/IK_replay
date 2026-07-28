"""拨动开关全流程编排。

已部署的步骤（测平面/测距、腰部对齐、pick→规划→执行）直接走 reach_server；
起手式按距离自动选（序列名带门槛，如「0.46起手式」需 ≥0.46m，太近报
POSE_UNAVAILABLE）；收尾 = 插值到「起手点测试」路点后释放手臂。
尚未部署的步骤（YOLO 场景判断/点位识别/复核）走 7002 人工确认台交互顶上：
流程把问题推到网页，操作员回答后继续。以后某步的自动化就绪，把对应方法里
"问人"换成"问模型"即可，编排本身不用动。

不给 console 时保持旧行为：未实现步骤抛 FlowError(NOT_IMPLEMENTED)。

腰部对齐目标（按 2026-07-28 流程定义）：
  3️⃣ 粗对齐：平面指数（yaw）收进 -3 ~ -6°（target -4.5° ± 1.5°）
  6️⃣ 细保持：收进 -3° ± 2°，取点前复查，漂出带则重新对齐再取点

错误码：占位。等正式定义后替换 ErrorCode 的取值即可，接口形状不变。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .client import ReachClient
from .console_client import ConsoleAbort, ConsoleClient


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


class SwitchFlow:
    """一键开始 → … → 拨动完成 的整条流程。

    未部署步骤的临时实现走 console（7002 确认台）；正式实现就绪后
    覆写对应方法（detect_scene / detect_points / verify_flip / …）即可。
    """

    def __init__(self,
                 client: ReachClient | None = None,
                 console: ConsoleClient | None = None,
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
            self.waist_align(self.coarse_target_deg, self.coarse_tol_deg)

            self._log("═══ 4️⃣ 测距离 ═══")
            distance_m = self.measure_distance()
            self._log(f"距柜面 {distance_m:.3f} m")

            last_error: FlowError | None = None
            for round_no in range(1, self.max_flip_rounds + 1):
                self._log(f"═══ 5️⃣ 第 {round_no}/{self.max_flip_rounds} 轮 ═══")
                try:
                    pose = self.choose_opening_pose(distance_m)
                    self._current_pose = pose   # 拨完插值回它配套的「终点」路点
                    self._log(f"起手式: {pose}")
                    self.apply_opening_pose(pose)

                    self._log(f"═══ 6️⃣ 腰部细对齐并保持："
                              f"{self.fine_target_deg:+.1f}°±{self.fine_tol_deg}° ═══")
                    self.waist_align(self.fine_target_deg, self.fine_tol_deg)

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
                                    ErrorCode.POSE_UNAVAILABLE):
                        raise    # 未实现/距离不够：重试也不会变，直接中止
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

    def waist_align(self, target_deg: float, tol_deg: float) -> None:
        """腰部调节：把平面指数收进 target_deg ± tol_deg。"""
        yaw = float(self.measure_plane()["yaw_err_deg"])
        if abs(yaw - target_deg) <= tol_deg:
            self._log(f"平面指数已在带内（yaw {yaw:+.2f}°，"
                      f"目标 {target_deg:+.1f}°±{tol_deg}°），跳过")
            return
        res = self.client.align_yaw_start(self.dmin, self.dmax,
                                          tol_deg=tol_deg,
                                          target_deg=target_deg,
                                          mode=self.align_mode)
        if not res.get("ok"):
            raise FlowError(ErrorCode.ALIGN_FAILED,
                            f"对中启动失败: {res.get('error')}")
        deadline = time.monotonic() + self.align_timeout_s
        while time.monotonic() < deadline:
            time.sleep(1.0)
            fit = self.client.perpendicular(self.dmin, self.dmax)
            align = fit.get("align") or {}
            if not align.get("running"):
                err = (abs(float(fit["yaw_err_deg"]) - target_deg)
                       if fit.get("ok") else None)
                self._log(f"对中结束: {align.get('message')}（带内残差 {err}°）")
                if err is not None and err <= tol_deg:
                    return
                raise FlowError(ErrorCode.ALIGN_FAILED,
                                f"对中结束但残差 {err}° 未达 ±{tol_deg}°")
            self._log(f"对中中… {align.get('message') or ''}")
        self.client.align_yaw_stop()
        raise FlowError(ErrorCode.ALIGN_FAILED, f"对中超时（>{self.align_timeout_s}s）")

    def _detect_points_held(self) -> list[dict]:
        """取点前复查保持带：取点期间腰若漂出 -3°±2° 就重新对齐再取。"""
        for attempt in (1, 2):
            points = self.detect_points()
            yaw = float(self.measure_plane()["yaw_err_deg"])
            if abs(yaw - self.fine_target_deg) <= self.fine_tol_deg:
                return points
            self._log(f"取点期间漂出保持带（yaw {yaw:+.2f}°），重新对齐后重新取点"
                      f"（第 {attempt} 次）")
            self.waist_align(self.fine_target_deg, self.fine_tol_deg)
        return self.detect_points()

    # 横移方向 = 拟合平面的"左"再向下倾 2°（同 main.js SIDESTEP_TILT_DEG）
    SIDESTEP_TILT_DEG = 2.0
    SIDESTEP_PUSH_SPEED = 0.06   # 带推力时快拨（m/s）：借冲量越过定位卡点

    def flip_switch(self, points: list[dict]) -> None:
        """IK 执行拨动，完整复刻 index.html 真机验证过的链路和参数：

          取点（接近偏移 0）→ 左侧规划（中段抬高 2cm）→ 确认 →
          主段到位（6s）→ 沿柜面左移 6cm + 前馈推力 25N（快拨 1s）→
          关节插值回起手式配套的「终点」路点（如 0.46终点），之后交给复核
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

            if self.console is not None:
                self.console.confirm(
                    f"{tag} 预演就绪（{len(frames)} 路点，IK 误差 "
                    f"{plan.get('max_ik_error_mm')}mm）。\n"
                    f"确认后真机执行：到位 {self.reach_duration_s:.0f}s → "
                    f"左移 {self.sidestep_cm:.0f}cm + 推力 "
                    f"{self.push_force_n:.0f}N → 插值回终点路点")
            res = self.client.execute(
                waypoints=[f["named_joints"] for f in frames],
                duration=self.reach_duration_s, label="flow_reach")
            if not res.get("ok"):
                raise FlowError(ErrorCode.EXEC_FAILED,
                                f"{tag} 到位执行被拒: {res.get('error')}")
            self._wait_exec(f"{tag} 到位")

            self._sidestep_flick(picked, tag)
            self._goto_endpoint(tag)

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

    def _interp_to_waypoint(self, wp_name: str, tag: str) -> None:
        """关节空间插值到指定名字的已录路点。

        直接把 [当前姿态, 目标姿态] 交给 /execute 做关节插值，不走
        IK/规划——这些路点都是人工录制验证过的安全姿态。
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
        duration = max(2.0, travel / max(self.endpoint_speed_rad_s, 0.05))
        res = self.client.execute(waypoints=[cur, end], duration=duration,
                                  max_speed_rad_s=self.endpoint_speed_rad_s,
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

    def detect_scene(self) -> dict:
        """2️⃣ 是否需要拨动、往哪个方向拨。TODO：接 YOLO 后替换问人。

        目前只有「从右向左」的拨法真机验证过（左移+推力）；
        「从左向右」未支持，流程直接退出。
        """
        answer = self._need_console("YOLO 场景判断").choice(
            "2️⃣ 场景判断（YOLO 未部署，请看相机画面人工判断）\n"
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
        「0.46起手式」→ 距柜面 ≥ 0.46 m 才能用。多个够格时选门槛最大
        （最贴近当前距离）的那个；一个都不够格 → POSE_UNAVAILABLE。
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

    def apply_opening_pose(self, pose: dict) -> None:
        """把手臂摆到起手式：序列走 /sequences/run（首次规划→确认→回放），
        手动摆位则等确认台放行。"""
        if pose.get("manual"):
            self._need_console("起手式执行").confirm(
                "请手动把手臂摆到起手式，摆好后确认")
            return
        # 等价于调试页对该序列点「确定」：已有录制立即回放；起点漂移触发
        # 重规划时（服务端只回 preview 不执行），自动再跑一次直接执行
        res = self.client.run_sequence(pose["file"])
        if not res.get("ok"):
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"起手式序列启动失败: {res.get('error')}")
        if res.get("preview"):
            self._log(f"起手式「{pose['name']}」起点变了已重新规划"
                      f"（{res.get('frames')} 帧，约 {res.get('duration_s')}s），继续执行")
            res = self.client.run_sequence(pose["file"])
            if not res.get("ok") or res.get("preview"):
                raise FlowError(ErrorCode.EXEC_FAILED,
                                f"起手式回放失败: {res.get('error') or '仍在 preview'}")
        self._wait_exec(f"起手式「{pose['name']}」")

    def detect_points(self) -> list[dict]:
        """开关点位（像素坐标）。TODO：接 YOLO 后替换问人。"""
        pts = self._need_console("YOLO 点位识别").points(
            "请在相机画面上点击要拨动的开关点位（可多个），点完提交")
        if not pts:
            raise FlowError(ErrorCode.YOLO_FAILED, "没有点位")
        return pts

    def verify_flip(self) -> bool:
        """复核是否拨动成功。TODO：接 YOLO 后替换问人。"""
        return self._need_console("拨动复核").yesno(
            "复核（YOLO 未部署）：开关拨动成功了吗？")

    DESCEND_WAYPOINT = "起手点测试"   # 复核成功后插值回落到这个已录路点

    def descend_fast(self, pose: dict | None) -> None:
        """收尾：关节插值到「起手点测试」路点，然后释放手臂。"""
        self._interp_to_waypoint(self.DESCEND_WAYPOINT, "收尾")
        self._log("释放手臂")
        res = self.client.disarm()
        if not res.get("ok"):
            raise FlowError(ErrorCode.EXEC_FAILED,
                            f"释放手臂失败: {res.get('error')}")

    # ---------------------------------------------------------------- 工具

    def _done(self, t0: float, message: str, **detail: Any) -> FlowResult:
        self._log(f"✔ {message}")
        return FlowResult(ok=True, code=ErrorCode.OK, message=message,
                          detail={"elapsed_s": round(time.monotonic() - t0, 1),
                                  **detail})

    @staticmethod
    def _log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] [flow] {msg}", flush=True)
