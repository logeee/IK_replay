"""pink 运动后端：世界系 PINK 闭环跟踪接入 18001 执行层。

由 18000 ``active.motion_backend == "pink"`` 启用（默认 ``legacy``，走原有
``execution._exec_loop``，本模块不会被 import）。

与 legacy 的关系
----------------
* 取点 / IK / 规划 / 碰撞 / 路点文件 **完全复用**，``/api/reach/execute`` 收到的
  关节路点列表也是同一份；
* 差别只在"怎么把路点送到 ``H2ArmController``"：legacy 按节拍逐个
  ``set_target(q_i)``；pink 把路点提升为**世界系** TCP 参考轨迹，50 Hz 用 PINK
  微分 IK 解出补偿了躯干实时位姿的 ``q_target`` 再 ``set_target``；
* 执行器仍是 ``H2ArmController``（限速、重力前馈、限位、急停都不变）。

世界系来源：``control.world_frame``（IMU + 支撑腿运动学）。**必须先锚定**
（``POST /api/reach/pink/anchor``，机器人双脚站定时），取点时记录
``world_T_root``，规划出的路点用它提升到世界系；执行中躯干再怎么动，目标在
世界系不变。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from fastapi.responses import JSONResponse

from .state import _read_torso, router, state

MOTION_BACKENDS = ("legacy", "pink")
CONTROL_DT = 0.02
HOLD_S = 1.0                     # 到位后世界系保持时长（同时测终态误差）
RECOVERY_TIMEOUT_S = 10.0        # 自动恢复窗口，超时进 PAUSED_MANUAL 等人工 RESUME
PROJECT_ROOT = Path(__file__).resolve().parents[2]

PINK_CONFIG_BY_ARM = {
    "right": PROJECT_ROOT / "config/robots/h2_pink_right.yaml",
    "left": PROJECT_ROOT / "config/robots/h2_pink_left.yaml",
}
FLOATING_BASE_CONFIG = PROJECT_ROOT / "config/robots/h2_floating_base.yaml"
H2_ARM_MOTOR_INDICES = {
    "right": (22, 23, 24, 25, 26, 27, 28),
    "left": (15, 16, 17, 18, 19, 20, 21),
}


def normalize_motion_backend(value: Any) -> str:
    text = str(value or "legacy").strip().lower()
    if text not in MOTION_BACKENDS:
        raise ValueError(f"motion_backend 必须是 {MOTION_BACKENDS} 之一，收到 {value!r}")
    return text


class PinkRuntime:
    """进程级 pink 运行时：配置、世界系估计器、按 TCP 缓存的 PINK 控制器、当前会话。"""

    def __init__(self, *, arm_side: str, sampler, wrist_link: str,
                 pink_config_path: Path | None = None,
                 floating_base_config_path: Path | None = None) -> None:
        from control.approach_tracker import ApproachTrackerConfig
        from control.world_frame import WorldFrameEstimator

        if arm_side not in PINK_CONFIG_BY_ARM:
            raise ValueError(f"arm_side 必须是 right/left，收到 {arm_side!r}")
        self.arm_side = arm_side
        self.wrist_link = str(wrist_link)
        self.arm_motor_indices = H2_ARM_MOTOR_INDICES[arm_side]
        self.pink_config_path = Path(pink_config_path or PINK_CONFIG_BY_ARM[arm_side])
        self.config: dict[str, Any] = yaml.safe_load(self.pink_config_path.read_text(encoding="utf-8"))
        model_cfg = self.config["model"]
        if str(model_cfg["wrist_frame"]) != self.wrist_link:
            raise ValueError(
                f"pink 配置的 wrist_frame={model_cfg['wrist_frame']} 与手眼标定 wrist_link={self.wrist_link} 不一致")
        urdf = Path(str(model_cfg["urdf_path"]))
        self.urdf_path = urdf if urdf.is_absolute() else PROJECT_ROOT / urdf
        self.tracker_config = ApproachTrackerConfig.from_mapping(self.config.get("tracker") or {})
        execution_cfg = self.config.get("execution") or {}
        self.feedback = str(execution_cfg.get("feedback", "internal")).lower()
        if self.feedback not in ("internal", "command", "measured"):
            raise ValueError("execution.feedback 必须是 internal / command / measured")
        self.executor_lag_fault_rad = float(execution_cfg.get("executor_lag_fault_rad", 0.25))
        self.hold_s = float(execution_cfg.get("hold_s", HOLD_S))
        self.world_frame = WorldFrameEstimator.from_yaml(
            floating_base_config_path or FLOATING_BASE_CONFIG, project_root=PROJECT_ROOT)
        self.sampler = sampler
        self._controllers: dict[tuple, Any] = {}
        self._lock = threading.RLock()
        self.session = None                      # 当前执行的 TrackingSession
        self.last_summary: dict[str, Any] | None = None
        self.pick_world_T_root: np.ndarray | None = None
        self.pick_world_frame_anchor: int | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ 控制器
    def controller_for_tool(self, p_tool) :
        """按当前 TCP 偏移（腕系点，来自手眼标定/18003 选择）取 PINK 控制器。"""
        from control.pink_arm_controller import PinkArmController
        from control.tool_config import ToolConfig

        if p_tool is None:
            raise RuntimeError("p_tool(TCP) 未就绪，pink 后端需要腕系 TCP 偏移")
        p = tuple(round(float(v), 6) for v in np.asarray(p_tool, dtype=float).reshape(3))
        with self._lock:
            controller = self._controllers.get(p)
            if controller is None:
                wrist_T_tcp = np.eye(4)
                wrist_T_tcp[:3, 3] = p
                tool = ToolConfig(f"{self.arm_side}_tcp", self.wrist_link, wrist_T_tcp)
                controller = PinkArmController(self.urdf_path, self.config, tool)
                self._controllers[p] = controller
            return controller

    # ------------------------------------------------------------------ 世界系
    def anchor(self) -> dict[str, Any]:
        sample = self.sampler.sample()
        fb = self.world_frame.anchor(sample)
        # 旧的取点世界系已失效（锚点变了）
        self.pick_world_T_root = None
        self.pick_world_frame_anchor = None
        return fb.to_dict()

    def update_world(self):
        sample = self.sampler.sample()
        return sample, self.world_frame.update(sample)

    def capture_pick_frame(self) -> np.ndarray | None:
        """取点时刻调用：记录 world_T_root，之后的规划结果都相对它提升到世界系。"""
        if not self.world_frame.anchored:
            self.pick_world_T_root = None
            self.pick_world_frame_anchor = None
            return None
        try:
            _, fb = self.update_world()
        except Exception as exc:
            self.last_error = f"capture_pick_frame: {exc}"
            return None
        self.pick_world_T_root = fb.world_T_root.copy()
        self.pick_world_frame_anchor = self.world_frame.anchor_count
        return self.pick_world_T_root

    def status(self) -> dict[str, Any]:
        session = self.session
        return {
            "backend": "pink",
            "arm_side": self.arm_side,
            "config": str(self.pink_config_path.relative_to(PROJECT_ROOT)),
            "feedback": self.feedback,
            "pink_qdot_ceiling_rad_s": float(self.config["pink"]["max_joint_velocity_rad_s"]),
            "world_frame": self.world_frame.snapshot(),
            "lowstate_age_ms": self.sampler.age_ms(),
            "pick_world_frame": None if self.pick_world_T_root is None else {
                "anchor_count": self.pick_world_frame_anchor,
                "world_T_root": self.pick_world_T_root.tolist(),
            },
            "session": None if session is None else {
                "elapsed_s": session.elapsed_s(),
                "finished": session.finished,
                "plan": session.plan.to_dict(),
                "supervisor": session.supervisor.snapshot(),
            },
            "last_summary": self.last_summary,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------- 构建


def build_runtime(*, arm_side: str, wrist_link: str, network_interface: str | None,
                  mock: bool) -> PinkRuntime:
    from .lowstate import H2LowStateSampler, MockLowStateSampler

    if mock:
        sampler = MockLowStateSampler(arm_motor_indices=H2_ARM_MOTOR_INDICES[arm_side])
    else:
        sampler = H2LowStateSampler(network_interface=network_interface)
    return PinkRuntime(arm_side=arm_side, sampler=sampler, wrist_link=wrist_link)


# ---------------------------------------------------------------------- 执行


def exec_loop_pink(q_list: list[np.ndarray], duration: float,
                   push_tau: np.ndarray | None = None, speed: float = 0.2,
                   push_hold_s: float = 1.5,
                   label: str = "reach", command_start_q: np.ndarray | None = None,
                   command_handoff: dict | None = None,
                   execution_context: dict | None = None,
                   flip_evidence: dict | None = None,
                   stiffness_scale: float = 1.0) -> None:
    """与 ``execution._exec_loop`` 同签名、同阶段语义（traj→converge→settle / push）。"""
    from . import execution as legacy
    from control.fault_supervisor import FaultSupervisor, SupervisorState
    from control.reference_builder import build_reference_plan
    from control.tracking_session import TrackingSession
    from control.world_frame import WorldFrameNotAnchored

    ctl = state.controller
    rt: PinkRuntime = state.pink_runtime
    trace, trace_stop = legacy._start_torso_trace(ctl)
    pink_log: dict[str, Any] = {"backend": "pink", "steps": []}
    log = dict(duration=duration, speed=speed, pushing=push_tau is not None,
               push_tau=push_tau, push_hold_s=push_hold_s,
               trace=trace, command_handoff=command_handoff,
               execution_context=execution_context,
               stiffness_scale=stiffness_scale, extra=pink_log)
    completed = False
    stiffness_snapshot = None
    session = None
    try:
        if command_start_q is None:
            raise RuntimeError("缺少上一帧已发送关节命令，拒绝启动轨迹")
        if rt is None:
            raise RuntimeError("pink 运行时未初始化")
        stiffness_snapshot = legacy._apply_stiffness_scale(ctl, stiffness_scale)
        state.last_settle_trim = None
        control_q_list = legacy._build_control_waypoints(q_list, command_start_q)

        # ---- 世界系：必须已锚定；规划参考用取点时刻的 world_T_root ----
        try:
            _, fb = rt.update_world()
        except WorldFrameNotAnchored:
            raise RuntimeError("世界系未锚定：请在机器人双脚站定时点「锚定世界系」，再取点、规划、执行")
        if rt.pick_world_T_root is not None and rt.pick_world_frame_anchor == rt.world_frame.anchor_count:
            world_T_root_ref = rt.pick_world_T_root
            ref_source = "pick"
        else:
            world_T_root_ref = fb.world_T_root
            ref_source = "execution_start"
        pink_log["world_T_root_ref_source"] = ref_source
        pink_log["world_frame_at_start"] = fb.to_dict()

        controller = rt.controller_for_tool(state.p_tool)
        fk = lambda q: controller.root_T_tcp_actual(q).homogeneous  # noqa: E731

        state.exec_phase = "traj"
        ctl.enable_jog()
        if hasattr(ctl, "set_max_speed"):
            ctl.set_max_speed(max(0.4, speed) if push_tau is not None else speed)
        exec_speed = float(ctl.max_speed)
        plan = build_reference_plan(control_q_list, duration, fk=fk,
                                    world_T_root_ref=world_T_root_ref, max_qdot_rad_s=exec_speed)
        if plan.time_scale > 1.0 + 1e-9:
            state.exec_message = f"时长过短，按限速拉长到 {plan.duration_s:.1f}s"
        pink_log["plan"] = plan.to_dict()

        supervisor = FaultSupervisor(f"reach-{state.session_id}", rt.arm_side,
                                     recovery_timeout_s=RECOVERY_TIMEOUT_S)
        session = TrackingSession(controller, rt.tracker_config, plan, supervisor=supervisor,
                                  executor_max_qdot_rad_s=exec_speed, hold_s=rt.hold_s,
                                  executor_lag_fault_rad=rt.executor_lag_fault_rad)
        rt.session = session

        def executor_q(status: dict) -> np.ndarray:
            """执行器实际所在：指令角（默认）或实测角。"""
            if rt.feedback == "measured" and status.get("measured_rad"):
                return np.asarray(status["measured_rad"], dtype=float)
            return np.asarray(status["cmd_rad"], dtype=float)

        def feedback_q(status: dict) -> np.ndarray:
            """喂给 PINK 的状态：internal=PINK 自己上一拍的 q_target（见 TrackingSession.step）。"""
            if rt.feedback == "internal" and session.last_q_target is not None:
                return session.last_q_target
            return executor_q(status)

        status = ctl.status()
        # 起点用上一帧已发送的指令（与 legacy 相同的连续性语义）
        session.start(np.asarray(command_start_q, dtype=float), None, fb.world_T_root)

        n_ramp = max(1, int(round(0.3 * plan.duration_s / CONTROL_DT)))
        tick = 0
        next_t = time.monotonic()
        last_phase = None
        while True:
            if state.exec_cancel.is_set():
                ctl.disable_jog()
                state.exec_message = "已中止（保持当前位置）"
                pink_log["summary"] = session.summary.to_dict()
                legacy._log_exec(label, "cancelled", q_list[-1], **log)
                return
            sample, fb = rt.update_world()
            status = ctl.status()
            warnings = ()
            if fb.quality != "DOUBLE_SUPPORT_GOOD":
                warnings = (f"floating_base:{fb.quality}",)
            step = session.step(feedback_q(status), None, fb.world_T_root,
                                state_age_ms=rt.sampler.age_ms(), warnings=warnings,
                                q_executor=executor_q(status))
            ctl.set_target(step.q_target)
            if push_tau is not None:
                ctl.set_tau_ff(push_tau * min(1.0, (tick + 1) / n_ramp))
            tick += 1
            state.exec_progress = float(min(1.0, step.t_s / max(plan.duration_s, 1e-6)))
            if step.phase != last_phase:
                last_phase = step.phase
                state.exec_message = {
                    "TRACK": "执行中（pink 世界系跟踪）",
                    "HOLD": "到位，世界系保持中",
                    "RECOVERING": f"故障保持中，自动恢复: {supervisor.snapshot().get('fault_state')}",
                    "PAUSED": "已暂停（保持最后目标，等待 RESUME）",
                    "AUTHORITY_LOST": "失去控制权：手臂不再由本进程驱动",
                    "DONE": "到位",
                }.get(step.phase, step.phase)
            if tick % 5 == 0 or step.phase not in ("TRACK", "HOLD"):
                rec = step.to_dict()
                rec["fb_quality"] = fb.quality
                pink_log["steps"].append(rec)
                if len(pink_log["steps"]) > 400:
                    pink_log["steps"] = pink_log["steps"][::2]
            if step.finished:
                break
            if supervisor.state is SupervisorState.CONTROL_AUTHORITY_LOST:
                raise RuntimeError("pink: control authority lost")
            next_t += CONTROL_DT
            sleep = next_t - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_t = time.monotonic()

        pink_log["summary"] = session.summary.to_dict()
        rt.last_summary = pink_log["summary"]
        q_final = session.last_q_target if session.last_q_target is not None else q_list[-1]

        # ---- 以下与 legacy 相同：等限速滑动到位、推力保持/撤力、落点测量 ----
        state.exec_message = "收敛中"
        state.exec_phase = "converge"
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not state.exec_cancel.is_set():
            st = ctl.status()
            gap = float(np.max(np.abs(np.asarray(st["desired_rad"]) - np.asarray(st["cmd_rad"]))))
            if gap < 1e-3:
                break
            time.sleep(0.1)

        if push_tau is not None:
            if not state.exec_cancel.is_set() and push_hold_s > 0:
                state.exec_message = "持续出力中"
                state.exec_phase = "push_hold"
                deadline = time.monotonic() + push_hold_s
                while time.monotonic() < deadline and not state.exec_cancel.is_set():
                    time.sleep(0.05)
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
                st = ctl.status()
                if float(np.max(np.abs(np.asarray(st["desired_rad"]) - np.asarray(st["cmd_rad"])))) < 1e-3:
                    break
                time.sleep(0.05)
            ctl.disable_jog()
            state.exec_progress = 1.0
            cancelled = state.exec_cancel.is_set()
            state.exec_message = ("已中止（保持当前位置）" if cancelled
                                  else f"完成（推力段结束，已撤力保持{legacy._finish_torso_diag()}）")
            legacy._log_exec(label, "cancelled" if cancelled else "done", q_final, **log)
            completed = not cancelled
            return

        sag = None
        trim_info = None
        if not state.exec_cancel.is_set():
            state.exec_phase = "settle"
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not state.exec_cancel.is_set():
                st = ctl.status()
                if float(np.max(np.abs(np.asarray(st["desired_rad"]) - np.asarray(st["cmd_rad"])))) < 1e-3:
                    break
                time.sleep(0.08)
            if not state.exec_cancel.is_set():
                time.sleep(0.3)
                st = ctl.status()
                measured = np.asarray(st["measured_rad"] or ctl.read_measured().tolist())
                sag = float(np.max(np.abs(q_final - measured)))
                trim_info = legacy._run_settle_trim(ctl, q_final)
                state.last_settle_trim = trim_info

        ctl.disable_jog()
        state.exec_progress = 1.0
        summary = session.summary
        err_note = ""
        if summary.final_executor_error_m is not None:
            err_note = f"，世界系终态误差 {summary.final_executor_error_m*1e3:.1f} mm"
        elif summary.final_position_error_m is not None:
            err_note = f"，世界系终态误差 {summary.final_position_error_m*1e3:.1f} mm"
        sag_note = f"，落点残差 {sag:.3f} rad" if sag is not None else ""
        if trim_info is not None:
            sag_note += f"，{trim_info['mode']} 修正后 {trim_info['final_residual_max_rad']:.3f} rad"
        cancelled = state.exec_cancel.is_set()
        state.exec_message = ("已中止（保持当前位置）" if cancelled
                              else f"完成（pink 刚性保持{err_note}{sag_note}{legacy._finish_torso_diag()}）")
        legacy._log_exec(label, "cancelled" if cancelled else "done", q_final,
                         sag=sag, settle_trim=trim_info, **log)
        completed = not cancelled
    except Exception as exc:
        try:
            ctl.stop()
        except Exception:
            pass
        rt_err = f"{type(exc).__name__}: {exc}"
        if rt is not None:
            rt.last_error = rt_err
        if session is not None:
            pink_log["summary"] = session.summary.to_dict()
        state.exec_message = f"执行出错已停止: {exc}"
        legacy._log_exec(label, f"error: {exc}", q_list[-1], **log)
    finally:
        legacy._restore_stiffness(ctl, stiffness_snapshot)
        trace_stop.set()
        if rt is not None:
            rt.session = None
        if completed and flip_evidence is not None:
            from .flip_verification import verify_manual_after
            try:
                verification = verify_manual_after(flip_evidence)
            except Exception as exc:
                verification = {"ok": False, "error": str(exc)}
            state.last_flip_verification = {"stage": "after", **verification}
            if verification.get("ok"):
                state.exec_message += "；YOLO复核" + ("成功" if verification.get("success") else "未成功")
            else:
                state.exec_message += f"；YOLO复核失败: {verification.get('error') or '未知错误'}"
        state.exec_phase = "idle"
        state.exec_running = False


# ---------------------------------------------------------------------- 接口


def _require_pink():
    if state.motion_backend != "pink" or state.pink_runtime is None:
        return JSONResponse({"ok": False, "error": "当前运动后端不是 pink（18000 active.motion_backend）"},
                            status_code=409)
    return None


@router.get("/pink/status")
def pink_status():
    if state.pink_runtime is None:
        return {"ok": True, "backend": state.motion_backend, "available": False}
    try:
        return {"ok": True, "available": True, "motion_backend": state.motion_backend,
                **state.pink_runtime.status()}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/pink/anchor")
def pink_anchor():
    """（重新）锚定世界系。机器人必须双脚站定；走动后必须重新锚定。"""
    rejected = _require_pink()
    if rejected is not None:
        return rejected
    if state.exec_running:
        return JSONResponse({"ok": False, "error": "轨迹执行中不能重新锚定"}, status_code=409)
    try:
        fb = state.pink_runtime.anchor()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"锚定失败: {exc}"}, status_code=500)
    return {"ok": True, "floating_base": fb, "torso": _read_torso(),
            "note": "取点世界系已清空：请重新取点再规划执行"}


@router.post("/pink/hold")
def pink_hold():
    rejected = _require_pink()
    if rejected is not None:
        return rejected
    session = state.pink_runtime.session
    if session is None:
        return JSONResponse({"ok": False, "error": "没有正在跟踪的会话"}, status_code=409)
    try:
        session.manual_hold()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return {"ok": True, "supervisor": session.supervisor.snapshot()}


@router.post("/pink/resume")
def pink_resume():
    rejected = _require_pink()
    if rejected is not None:
        return rejected
    session = state.pink_runtime.session
    if session is None:
        return JSONResponse({"ok": False, "error": "没有正在跟踪的会话"}, status_code=409)
    try:
        session.manual_resume()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return {"ok": True, "supervisor": session.supervisor.snapshot()}
