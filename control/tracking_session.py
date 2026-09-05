"""50 Hz 世界系 PINK 闭环跟踪会话 + Fault Supervisor 接入。

每个控制周期由硬件侧调用 :meth:`TrackingSession.step`，传入实测 ``q``/``dq``、
当前 ``world_T_root`` 与状态新鲜度，返回要下发给执行器的 ``q_target``。

故障策略（与同事中间件一致）：
* QP 连续失败 / 状态过期 / 跟踪误差过大 → ``RECOVERING_HOLD``：冻结轨迹时钟，
  **保持最后一个有效 q_target**，绝不自动松手；
* 恢复严格按 fresh state → controller reset → 重规划（关节空间五次多项式直连
  到原目标）→ resume；恢复超时 → ``PAUSED_MANUAL`` 等人工 RESUME；
* 人工 HOLD 优先级最高；RUNNING 中不允许 release（由调用方决定何时结束）。

本模块不接触硬件：不 import DDS，不调用 ``set_target``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pinocchio as pin

from control.approach_tracker import ApproachTracker, ApproachTrackerConfig
from control.fault_supervisor import (
    FaultCode,
    FaultSupervisor,
    PausableTrajectoryClock,
    RecoveryCoordinator,
    SupervisorState,
)
from control.interfaces import ArmState
from control.pink_arm_controller import PinkArmController
from control.reference_builder import ReferencePlan, build_recovery_plan


@dataclass(frozen=True)
class TrackingStep:
    t_s: float
    phase: str                      # TRACK | HOLD | RECOVERING | PAUSED | DONE | AUTHORITY_LOST
    supervisor_state: str
    q_target: np.ndarray
    qdot_cmd: np.ndarray
    qp_success: bool
    position_error_m: float         # 世界系：PINK 反馈状态的 TCP 相对 true reference
    orientation_error_rad: float
    solve_time_s: float
    plan_kind: str
    finished: bool
    warnings: tuple[str, ...] = ()
    executor_position_error_m: float | None = None   # 世界系：执行器实际关节角的 TCP 误差
    executor_lag_rad: float | None = None            # max|q_target - q_executor|

    def to_dict(self) -> dict[str, object]:
        return {
            "t_s": self.t_s,
            "phase": self.phase,
            "supervisor_state": self.supervisor_state,
            "qp_success": self.qp_success,
            "position_error_m": self.position_error_m,
            "orientation_error_rad": self.orientation_error_rad,
            "solve_time_s": self.solve_time_s,
            "plan_kind": self.plan_kind,
            "finished": self.finished,
            "warnings": list(self.warnings),
            "qdot_max_abs_rad_s": float(np.max(np.abs(self.qdot_cmd))) if self.qdot_cmd.size else 0.0,
            "executor_position_error_m": self.executor_position_error_m,
            "executor_lag_rad": self.executor_lag_rad,
        }


@dataclass
class TrackingSummary:
    steps: int = 0
    qp_failures: int = 0
    faults: list[dict[str, object]] = field(default_factory=list)
    recoveries: int = 0
    max_position_error_m: float = 0.0
    max_orientation_error_rad: float = 0.0
    final_position_error_m: float | None = None
    final_orientation_error_rad: float | None = None
    max_qdot_rad_s: float = 0.0
    solve_times_s: list[float] = field(default_factory=list)
    hold_ticks: int = 0
    max_executor_error_m: float | None = None
    final_executor_error_m: float | None = None
    max_executor_lag_rad: float | None = None

    def to_dict(self) -> dict[str, object]:
        solve = np.asarray(self.solve_times_s, dtype=np.float64)
        return {
            "steps": self.steps,
            "qp_failures": self.qp_failures,
            "faults": list(self.faults),
            "recoveries": self.recoveries,
            "hold_ticks": self.hold_ticks,
            "max_position_error_m": self.max_position_error_m,
            "max_orientation_error_rad": self.max_orientation_error_rad,
            "final_position_error_m": self.final_position_error_m,
            "final_orientation_error_rad": self.final_orientation_error_rad,
            "max_executor_error_m": self.max_executor_error_m,
            "final_executor_error_m": self.final_executor_error_m,
            "max_executor_lag_rad": self.max_executor_lag_rad,
            "max_qdot_rad_s": self.max_qdot_rad_s,
            "solve_time_ms": {
                "p50": float(np.percentile(solve, 50) * 1e3) if solve.size else None,
                "p95": float(np.percentile(solve, 95) * 1e3) if solve.size else None,
                "max": float(solve.max() * 1e3) if solve.size else None,
            },
        }


def _pose_error(actual_world_T_tcp: np.ndarray, reference_world_T_tcp: np.ndarray) -> tuple[float, float]:
    actual = pin.SE3(actual_world_T_tcp[:3, :3], actual_world_T_tcp[:3, 3])
    reference = pin.SE3(reference_world_T_tcp[:3, :3], reference_world_T_tcp[:3, 3])
    twist = pin.log6(actual.inverse() * reference).vector
    return float(np.linalg.norm(twist[:3])), float(np.linalg.norm(twist[3:]))


class TrackingSession:
    """一次"跟踪参考轨迹 → 到位保持"的完整会话。"""

    def __init__(
        self,
        controller: PinkArmController,
        tracker_config: ApproachTrackerConfig,
        plan: ReferencePlan,
        *,
        supervisor: FaultSupervisor,
        executor_max_qdot_rad_s: float,
        hold_s: float = 1.0,
        max_consecutive_qp_failures: int = 10,
        tracking_abort_error_m: float = 0.08,
        executor_lag_fault_rad: float = 0.25,
        state_age_fault_ms: float = 150.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if hold_s < 0.0 or not np.isfinite(hold_s):
            raise ValueError("hold_s must be finite and non-negative")
        self.executor_lag_fault_rad = float(executor_lag_fault_rad)
        self.controller = controller
        self.tracker_config = tracker_config
        self.plan = plan
        self.original_plan = plan
        self.supervisor = supervisor
        self.hold_s = float(hold_s)
        self.max_consecutive_qp_failures = int(max_consecutive_qp_failures)
        self.tracking_abort_error_m = float(tracking_abort_error_m)
        self.state_age_fault_ms = float(state_age_fault_ms)
        self._clock = clock
        if not np.isfinite(executor_max_qdot_rad_s) or executor_max_qdot_rad_s <= 0.0:
            raise ValueError("executor_max_qdot_rad_s must be positive and finite")
        # 分工：PINK 的 qdot 上限（配置 1.5 rad/s）保证 QP 不饱和、方向正确；
        # 真正限速由执行器 H2ArmController 的"矢量同步限速"完成（保方向）。
        # 若把 PINK 上限压到执行器速度，frame_gain·e/dt 会持续饱和，盒约束
        # 裁剪不保方向 → 目标在终点附近极限环振荡（离线复现过 15 mm 残差）。
        self.executor_max_qdot_rad_s = float(executor_max_qdot_rad_s)
        if controller.max_joint_velocity_rad_s < 2.0 * self.executor_max_qdot_rad_s:
            raise ValueError(
                "PINK max_joint_velocity_rad_s must be at least 2x the executor speed "
                f"({controller.max_joint_velocity_rad_s} vs {self.executor_max_qdot_rad_s})"
            )
        self._recovery = RecoveryCoordinator(supervisor)
        self._tracker: ApproachTracker | None = None
        self._trajectory_clock: PausableTrajectoryClock | None = None
        self._last_step_wall_s: float | None = None
        self._last_q_target: np.ndarray | None = None
        self._consecutive_qp_failures = 0
        self._finished = False
        self.summary = TrackingSummary()

    # ------------------------------------------------------------------ 帮助
    def fk_root_T_tcp(self, q: np.ndarray) -> np.ndarray:
        return np.asarray(self.controller.root_T_tcp_actual(q).homogeneous, dtype=np.float64)

    def arm_state(self, q: np.ndarray, dq: np.ndarray | None, world_T_root: np.ndarray) -> ArmState:
        q = np.asarray(q, dtype=np.float64).reshape(-1)
        dq_arr = np.zeros_like(q) if dq is None else np.asarray(dq, dtype=np.float64).reshape(-1)
        root = np.asarray(world_T_root, dtype=np.float64)
        return ArmState(q, dq_arr, root, root @ self.fk_root_T_tcp(q))

    @property
    def started(self) -> bool:
        return self._tracker is not None

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def last_q_target(self) -> np.ndarray | None:
        return None if self._last_q_target is None else self._last_q_target.copy()

    def elapsed_s(self) -> float:
        return 0.0 if self._trajectory_clock is None else self._trajectory_clock.elapsed_s()

    # ------------------------------------------------------------------ 生命周期
    def start(self, q: np.ndarray, dq: np.ndarray | None, world_T_root: np.ndarray) -> None:
        self._install_plan(self.plan, q, world_T_root)
        self._last_q_target = np.asarray(q, dtype=np.float64).copy()
        self._finished = False

    def _install_plan(self, plan: ReferencePlan, q: np.ndarray, world_T_root: np.ndarray) -> None:
        self.plan = plan
        self._tracker = ApproachTracker(self.controller, plan.reference, self.tracker_config)
        self._tracker.reset(np.asarray(q, dtype=np.float64), 0.0, np.asarray(world_T_root, dtype=np.float64))
        self.controller.set_posture_reference(plan.sample_posture(0.0))
        self._trajectory_clock = PausableTrajectoryClock(self._clock)
        self._last_step_wall_s = None
        self._consecutive_qp_failures = 0

    def manual_hold(self) -> None:
        self.supervisor.manual_hold()
        if self._trajectory_clock is not None:
            self._trajectory_clock.pause()

    def manual_resume(self) -> None:
        self.supervisor.manual_resume()

    # ------------------------------------------------------------------ 主循环
    def step(
        self,
        q: np.ndarray,
        dq: np.ndarray | None,
        world_T_root: np.ndarray,
        *,
        state_age_ms: float | None = None,
        warnings: tuple[str, ...] = (),
        q_executor: np.ndarray | None = None,
    ) -> TrackingStep:
        """推进一拍。

        ``q``：喂给 PINK 的反馈状态。推荐传 :attr:`last_q_target`（PINK 积分自己的
        内部状态，与同事真机验证的语义一致——执行器在 PINK 模型里"立刻到位"）；
        本项目的 ``H2ArmController`` 限速（0.4 rad/s）远低于 PINK 上限，若把滞后
        的指令角反馈回去，QP 每拍饱和、盒约束裁剪方向失真，会在终点附近极限环
        振荡（离线复现 9~15 mm 残差）。
        ``q_executor``：执行器实际（指令或实测）关节角，只用于诊断世界系真实误差
        与滞后；滞后超过 ``executor_lag_fault_rad`` 触发保持并从执行器状态重规划。
        """
        if self._tracker is None or self._trajectory_clock is None or self._last_q_target is None:
            raise RuntimeError("call start() before step()")
        now = float(self._clock())
        dt = 0.02 if self._last_step_wall_s is None else float(np.clip(now - self._last_step_wall_s, 0.005, 0.1))
        self._last_step_wall_s = now
        state = self.arm_state(q, dq, world_T_root)
        exec_state = None if q_executor is None else self.arm_state(q_executor, None, world_T_root)
        warnings = tuple(warnings)

        # 1. 状态新鲜度 / 执行器滞后
        if state_age_ms is not None and state_age_ms > self.state_age_fault_ms:
            self._fault(FaultCode.STATE_AGE_TRANSIENT, f"state age {state_age_ms:.0f} ms")
        lag = None
        if exec_state is not None:
            lag = float(np.max(np.abs(state.q_actual - exec_state.q_actual)))
            if lag > self.executor_lag_fault_rad:
                self._fault(FaultCode.TRACKING_REFERENCE_TRANSIENT,
                            f"executor lags PINK target by {lag:.3f} rad")

        # 2. 监管器状态 → 是否允许推进轨迹
        sup = self.supervisor.tick()
        if sup is not SupervisorState.RUNNING:
            self._trajectory_clock.pause()
            if sup in (SupervisorState.RECOVERING_HOLD, SupervisorState.AUTO_RESUME):
                recovered = self._try_recover(exec_state if exec_state is not None else state)
                if recovered:
                    sup = self.supervisor.tick()
            if sup is not SupervisorState.RUNNING:
                return self._hold_step(state, sup, warnings, exec_state=exec_state, lag=lag)
        self._trajectory_clock.resume()

        # 3. 正常推进
        t = self._trajectory_clock.elapsed_s()
        self.controller.set_posture_reference(self.plan.sample_posture(t))
        try:
            output = self._tracker.compute(t, state, dt)
        except Exception as exc:  # QP/求解器异常一律按 QP 失败处理，保持上一目标
            self._consecutive_qp_failures += 1
            self.summary.qp_failures += 1
            message = f"{type(exc).__name__}: {exc}"
            if self._consecutive_qp_failures >= self.max_consecutive_qp_failures:
                self._fault(FaultCode.IK_QP_TRANSIENT, f"{self._consecutive_qp_failures} consecutive QP failures: {message}")
            return self._hold_step(state, self.supervisor.tick(), warnings + (f"qp_exception:{message}",),
                                   exec_state=exec_state, lag=lag)
        diag = output.controller_diagnostics
        if diag.qp_success:
            self._consecutive_qp_failures = 0
            q_target = np.asarray(output.q_target, dtype=np.float64).copy()
            self._last_q_target = q_target
            qdot = np.asarray(output.qdot_cmd, dtype=np.float64).copy()
        else:
            self._consecutive_qp_failures += 1
            self.summary.qp_failures += 1
            q_target = self._last_q_target.copy()
            qdot = np.zeros_like(q_target)
            warnings = warnings + (f"qp_fail:{diag.message}",)
            if self._consecutive_qp_failures >= self.max_consecutive_qp_failures:
                self._fault(FaultCode.IK_QP_TRANSIENT, f"{self._consecutive_qp_failures} consecutive QP failures: {diag.message}")
        if getattr(diag, "joint_limit_warning", False):
            warnings = warnings + ("joint_limit:" + ",".join(diag.joint_limit_warning_joints),)

        pos_err, ori_err = _pose_error(state.world_T_tcp, output.true_reference_world_T_tcp)
        if pos_err > self.tracking_abort_error_m:
            self._fault(FaultCode.TRACKING_REFERENCE_TRANSIENT, f"world TCP tracking error {pos_err*1e3:.0f} mm")
        exec_err = None
        if exec_state is not None:
            exec_err, _ = _pose_error(exec_state.world_T_tcp, output.true_reference_world_T_tcp)

        in_hold = t >= self.plan.duration_s
        phase = "HOLD" if in_hold else "TRACK"
        finished = in_hold and (t - self.plan.duration_s) >= self.hold_s
        if finished:
            self._finished = True
            phase = "DONE"
            self.summary.final_position_error_m = pos_err
            self.summary.final_orientation_error_rad = ori_err
            self.summary.final_executor_error_m = exec_err
        self._record(diag.solve_time_s, pos_err, ori_err, qdot, in_hold, exec_err, lag)
        return TrackingStep(
            t_s=t,
            phase=phase,
            supervisor_state=self.supervisor.state.value,
            q_target=q_target,
            qdot_cmd=qdot,
            qp_success=bool(diag.qp_success),
            position_error_m=pos_err,
            orientation_error_rad=ori_err,
            solve_time_s=float(diag.solve_time_s),
            plan_kind=self.plan.kind,
            finished=finished,
            warnings=warnings,
            executor_position_error_m=exec_err,
            executor_lag_rad=lag,
        )

    # ------------------------------------------------------------------ 内部
    def _fault(self, code: FaultCode, message: str) -> None:
        state = self.supervisor.report_fault(code, message, can_still_command_reliably=True)
        self.summary.faults.append({"code": code.value, "message": message, "t_s": self.elapsed_s(), "state": state.value})

    def _hold_step(self, state: ArmState, sup: SupervisorState, warnings: tuple[str, ...], *,
                   exec_state: ArmState | None = None, lag: float | None = None) -> TrackingStep:
        assert self._last_q_target is not None and self._tracker is not None
        t = self.elapsed_s()
        ref = self._tracker.reference.sample(min(t, self.plan.duration_s))
        pos_err, ori_err = _pose_error(state.world_T_tcp, ref)
        exec_err = None if exec_state is None else _pose_error(exec_state.world_T_tcp, ref)[0]
        phase = {
            SupervisorState.RUNNING: "TRACK",
            SupervisorState.RECOVERING_HOLD: "RECOVERING",
            SupervisorState.AUTO_RESUME: "RECOVERING",
            SupervisorState.PAUSED_MANUAL: "PAUSED",
            SupervisorState.CONTROL_AUTHORITY_LOST: "AUTHORITY_LOST",
        }.get(sup, sup.value)
        self.summary.hold_ticks += 1
        self.summary.steps += 1
        return TrackingStep(
            t_s=t,
            phase=phase,
            supervisor_state=sup.value,
            q_target=self._last_q_target.copy(),
            qdot_cmd=np.zeros_like(self._last_q_target),
            qp_success=True,
            position_error_m=pos_err,
            orientation_error_rad=ori_err,
            solve_time_s=0.0,
            plan_kind=self.plan.kind,
            finished=False,
            warnings=warnings + ("holding_last_valid_target",),
            executor_position_error_m=exec_err,
            executor_lag_rad=lag,
        )

    def _try_recover(self, state: ArmState) -> bool:
        goal_q = self.original_plan.goal_q
        world_T_root_ref = self.original_plan.world_T_root_ref

        def acquire_fresh():
            return state

        def reset_controller(fresh: ArmState) -> None:
            self.controller.reset(fresh.q_actual)

        def replan(fresh: ArmState) -> ReferencePlan:
            return build_recovery_plan(
                fresh.q_actual,
                goal_q,
                fk=self.fk_root_T_tcp,
                world_T_root_ref=world_T_root_ref,
                max_qdot_rad_s=self.executor_max_qdot_rad_s,
            )

        plan = self._recovery.attempt(acquire_fresh, reset_controller, replan)
        if plan is None:
            return False
        # 从执行器真实所在处重新出发：内部状态也回到 fresh q
        self._install_plan(plan, state.q_actual, state.world_T_root)
        self._last_q_target = np.asarray(state.q_actual, dtype=np.float64).copy()
        self.summary.recoveries += 1
        return True

    def _record(self, solve_time_s: float, pos_err: float, ori_err: float, qdot: np.ndarray, in_hold: bool,
                exec_err: float | None = None, lag: float | None = None) -> None:
        s = self.summary
        s.steps += 1
        s.solve_times_s.append(float(solve_time_s))
        s.max_position_error_m = max(s.max_position_error_m, pos_err)
        s.max_orientation_error_rad = max(s.max_orientation_error_rad, ori_err)
        if qdot.size:
            s.max_qdot_rad_s = max(s.max_qdot_rad_s, float(np.max(np.abs(qdot))))
        if in_hold:
            s.hold_ticks += 1
        if exec_err is not None:
            s.max_executor_error_m = max(s.max_executor_error_m or 0.0, exec_err)
        if lag is not None:
            s.max_executor_lag_rad = max(s.max_executor_lag_rad or 0.0, lag)
