"""Fault-state policy for an ArmMotion runtime.

This module owns no robot transport. It decides whether trajectory time must
freeze, whether automatic recovery is permitted, and when release is legal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import threading
import time
from typing import Callable


class SupervisorState(str, Enum):
    RUNNING = "RUNNING"
    RECOVERING_HOLD = "RECOVERING_HOLD"
    AUTO_RESUME = "AUTO_RESUME"
    PAUSED_MANUAL = "PAUSED_MANUAL"
    PREPARING_HANDOFF = "PREPARING_HANDOFF"
    HANDOFF_READY = "HANDOFF_READY"
    GRADUAL_RELEASE = "GRADUAL_RELEASE"
    CONTROL_AUTHORITY_LOST = "CONTROL_AUTHORITY_LOST"
    STOPPED = "STOPPED"


class FaultCode(str, Enum):
    STATE_AGE_TRANSIENT = "STATE_AGE_TRANSIENT"
    IK_QP_TRANSIENT = "IK_QP_TRANSIENT"
    VELOCITY_WARNING = "VELOCITY_WARNING"
    TRACKING_REFERENCE_TRANSIENT = "TRACKING_REFERENCE_TRANSIENT"
    PLANNER_SOLVER_TRANSIENT = "PLANNER_SOLVER_TRANSIENT"
    JOINT_LIMIT_RISK = "JOINT_LIMIT_RISK"
    COLLISION_RISK = "COLLISION_RISK"
    INVALID_STATE_OR_TARGET = "INVALID_STATE_OR_TARGET"
    MODE_FAILURE = "MODE_FAILURE"
    COMMAND_STREAM_LOSS = "COMMAND_STREAM_LOSS"
    OWNERSHIP_FAILURE = "OWNERSHIP_FAILURE"
    NATIVE_PROTECTION = "NATIVE_PROTECTION"
    ESTOP = "ESTOP"
    RUNTIME_EXCEPTION = "RUNTIME_EXCEPTION"


AUTO_RECOVERABLE_FAULTS = frozenset(
    {
        FaultCode.STATE_AGE_TRANSIENT,
        FaultCode.IK_QP_TRANSIENT,
        FaultCode.VELOCITY_WARNING,
        FaultCode.TRACKING_REFERENCE_TRANSIENT,
        FaultCode.PLANNER_SOLVER_TRANSIENT,
    }
)


class SupervisorCommandRejected(RuntimeError):
    """A control-plane command is invalid for the current supervisor state."""


@dataclass(frozen=True)
class FaultRecord:
    code: FaultCode
    message: str
    first_seen_monotonic_s: float
    last_seen_monotonic_s: float
    occurrence_count: int
    auto_recoverable: bool
    can_still_command_reliably: bool


@dataclass
class RuntimeTelemetry:
    lifecycle_state: str = "STARTING"
    ownership_state: str = "NOT_ACQUIRED"
    state_age_ms: float | None = None
    holder_rate_hz: float | None = None
    max_dq_rad_s: float | None = None
    tcp_position_error_m: float | None = None
    tcp_orientation_error_rad: float | None = None


class PausableTrajectoryClock:
    """Monotonic elapsed time that excludes supervisor hold intervals."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started_s = float(clock())
        self._paused_started_s: float | None = None
        self._paused_total_s = 0.0

    def pause(self) -> None:
        if self._paused_started_s is None:
            self._paused_started_s = float(self._clock())

    def resume(self) -> None:
        if self._paused_started_s is not None:
            self._paused_total_s += float(self._clock()) - self._paused_started_s
            self._paused_started_s = None

    def elapsed_s(self) -> float:
        now = (
            self._paused_started_s
            if self._paused_started_s is not None
            else float(self._clock())
        )
        return max(0.0, now - self._started_s - self._paused_total_s)


class FaultSupervisor:
    """Thread-safe state machine shared by lifecycle and control plane."""

    def __init__(
        self,
        runtime_id: str,
        session_id: str,
        *,
        recovery_timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not runtime_id or not session_id:
            raise ValueError("runtime_id and session_id must not be empty")
        if recovery_timeout_s <= 0.0:
            raise ValueError("recovery_timeout_s must be positive")
        self.runtime_id = str(runtime_id)
        self.session_id = str(session_id)
        self.recovery_timeout_s = float(recovery_timeout_s)
        self._clock = clock
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._state = SupervisorState.RUNNING
        self._fault: FaultRecord | None = None
        self._recovery_started_s: float | None = None
        self._manual_hold_latched = False
        self._can_command_reliably = True
        self._telemetry = RuntimeTelemetry()

    @property
    def state(self) -> SupervisorState:
        with self._lock:
            self._apply_timeout_locked()
            return self._state

    @property
    def trajectory_frozen(self) -> bool:
        return self.state is not SupervisorState.RUNNING

    @property
    def can_still_command_reliably(self) -> bool:
        with self._lock:
            return self._can_command_reliably

    def update_telemetry(self, **values: object) -> None:
        with self._condition:
            for key, value in values.items():
                if not hasattr(self._telemetry, key):
                    raise ValueError(f"unknown runtime telemetry field {key!r}")
                setattr(self._telemetry, key, value)
            self._condition.notify_all()

    def report_fault(
        self,
        code: FaultCode | str,
        message: str,
        *,
        can_still_command_reliably: bool,
        auto_recoverable: bool | None = None,
    ) -> SupervisorState:
        parsed = FaultCode(code)
        now = float(self._clock())
        recoverable = (
            parsed in AUTO_RECOVERABLE_FAULTS
            if auto_recoverable is None
            else bool(auto_recoverable)
        )
        with self._condition:
            occurrence_count = (
                self._fault.occurrence_count + 1
                if self._fault is not None and self._fault.code is parsed
                else 1
            )
            first_seen = (
                self._fault.first_seen_monotonic_s
                if self._fault is not None and self._fault.code is parsed
                else now
            )
            self._can_command_reliably = bool(can_still_command_reliably)
            self._fault = FaultRecord(
                parsed,
                str(message),
                first_seen,
                now,
                occurrence_count,
                recoverable,
                self._can_command_reliably,
            )
            if not self._can_command_reliably:
                self._state = SupervisorState.CONTROL_AUTHORITY_LOST
                self._recovery_started_s = None
            elif self._state is SupervisorState.GRADUAL_RELEASE:
                # A gradual handoff cannot safely raise ownership weight again.
                # Keep the release state and let its loop freeze/retry at the
                # current weight while command authority remains available.
                self._recovery_started_s = None
            elif self._manual_hold_latched or not recoverable:
                self._state = SupervisorState.PAUSED_MANUAL
                self._recovery_started_s = None
            else:
                if self._state is not SupervisorState.RECOVERING_HOLD:
                    self._recovery_started_s = now
                self._state = SupervisorState.RECOVERING_HOLD
            self._condition.notify_all()
            return self._state

    def observe_recovered(self) -> bool:
        """Return True when lifecycle should resync and replan the active goal."""
        with self._condition:
            self._apply_timeout_locked()
            if self._state is not SupervisorState.RECOVERING_HOLD:
                return False
            if self._manual_hold_latched:
                self._state = SupervisorState.PAUSED_MANUAL
                return False
            self._state = SupervisorState.AUTO_RESUME
            self._condition.notify_all()
            return True

    def complete_auto_resume(self, success: bool, message: str = "") -> bool:
        """Finish a recovery transaction without overriding a manual command.

        Recovery planning can block.  A concurrent HOLD or PREPARE RELEASE
        received while it runs has priority and therefore makes this method a
        no-op.  The return value tells the lifecycle whether the new plan may
        actually be installed.
        """
        with self._condition:
            if self._state is not SupervisorState.AUTO_RESUME:
                if self._manual_hold_latched or self._state in {
                    SupervisorState.PAUSED_MANUAL,
                    SupervisorState.PREPARING_HANDOFF,
                    SupervisorState.HANDOFF_READY,
                    SupervisorState.GRADUAL_RELEASE,
                    SupervisorState.CONTROL_AUTHORITY_LOST,
                    SupervisorState.STOPPED,
                }:
                    return False
                raise SupervisorCommandRejected("auto-resume is not pending")
            if success:
                self._state = SupervisorState.RUNNING
                self._fault = None
                self._recovery_started_s = None
            else:
                self._state = SupervisorState.RECOVERING_HOLD
                if self._recovery_started_s is None:
                    self._recovery_started_s = float(self._clock())
                if self._fault is not None and message:
                    self._fault = FaultRecord(
                        self._fault.code,
                        str(message),
                        self._fault.first_seen_monotonic_s,
                        float(self._clock()),
                        self._fault.occurrence_count + 1,
                        self._fault.auto_recoverable,
                        True,
                    )
                self._apply_timeout_locked()
            self._condition.notify_all()
            return success and self._state is SupervisorState.RUNNING

    def manual_hold(self) -> None:
        with self._condition:
            if self._state in {
                SupervisorState.CONTROL_AUTHORITY_LOST,
                SupervisorState.GRADUAL_RELEASE,
                SupervisorState.STOPPED,
            }:
                raise SupervisorCommandRejected(
                    f"HOLD is unavailable in {self._state.value}"
                )
            self._manual_hold_latched = True
            self._state = SupervisorState.PAUSED_MANUAL
            self._recovery_started_s = None
            self._condition.notify_all()

    def manual_resume(self) -> None:
        with self._condition:
            if self._state is not SupervisorState.PAUSED_MANUAL:
                raise SupervisorCommandRejected("RESUME requires PAUSED_MANUAL")
            if not self._can_command_reliably:
                raise SupervisorCommandRejected("control authority is unavailable")
            self._manual_hold_latched = False
            self._state = SupervisorState.AUTO_RESUME
            self._condition.notify_all()

    def prepare_release(self) -> None:
        with self._condition:
            if not self._can_command_reliably or self._state in {
                SupervisorState.CONTROL_AUTHORITY_LOST,
                SupervisorState.GRADUAL_RELEASE,
                SupervisorState.STOPPED,
            }:
                raise SupervisorCommandRejected(
                    f"PREPARE RELEASE is unavailable in {self._state.value}"
                )
            # PREPARE RELEASE is an explicit motion request toward the handoff
            # pose, so it supersedes an earlier HOLD latch. A subsequent HOLD
            # command can still interrupt it immediately.
            self._manual_hold_latched = False
            self._state = SupervisorState.PREPARING_HANDOFF
            self._condition.notify_all()

    def mark_handoff_ready(self, success: bool, message: str = "") -> None:
        with self._condition:
            if self._state is not SupervisorState.PREPARING_HANDOFF:
                raise SupervisorCommandRejected("handoff preparation is not active")
            if success:
                self._state = SupervisorState.HANDOFF_READY
                self._fault = None
            else:
                self._state = SupervisorState.PAUSED_MANUAL
                self._fault = FaultRecord(
                    FaultCode.PLANNER_SOLVER_TRANSIENT,
                    str(message or "handoff planning failed"),
                    float(self._clock()),
                    float(self._clock()),
                    1,
                    True,
                    True,
                )
            self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            # Release is a manual stop-and-handoff command.  It is legal only
            # after intentional motion is frozen; AUTO_RESUME is included so
            # a manual release can cancel an in-flight recovery transaction.
            releaseable = {
                SupervisorState.PAUSED_MANUAL,
                SupervisorState.RECOVERING_HOLD,
                SupervisorState.AUTO_RESUME,
                # Kept for old in-process callers; Control Plane no longer
                # exposes prepare_release/HANDOFF_READY.
                SupervisorState.HANDOFF_READY,
            }
            if self._state not in releaseable:
                raise SupervisorCommandRejected(
                    "RELEASE requires intentional motion to be stopped "
                    "(PAUSED_MANUAL, RECOVERING_HOLD, or AUTO_RESUME)"
                )
            self._manual_hold_latched = True
            self._recovery_started_s = None
            self._state = SupervisorState.GRADUAL_RELEASE
            self._condition.notify_all()

    def mark_released(self) -> None:
        with self._condition:
            if self._state is not SupervisorState.GRADUAL_RELEASE:
                raise SupervisorCommandRejected("gradual release is not active")
            self._state = SupervisorState.STOPPED
            self._can_command_reliably = False
            self._condition.notify_all()

    def mark_authority_lost(self, message: str) -> None:
        self.report_fault(
            FaultCode.COMMAND_STREAM_LOSS,
            message,
            can_still_command_reliably=False,
            auto_recoverable=False,
        )

    def tick(self) -> SupervisorState:
        with self._condition:
            self._apply_timeout_locked()
            return self._state

    def wait_for_change(self, state: SupervisorState, timeout_s: float) -> SupervisorState:
        with self._condition:
            self._condition.wait_for(
                lambda: self._state is not state,
                timeout=max(0.0, float(timeout_s)),
            )
            self._apply_timeout_locked()
            return self._state

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self._apply_timeout_locked()
            now = float(self._clock())
            recovery_elapsed = (
                None
                if self._recovery_started_s is None
                else max(0.0, now - self._recovery_started_s)
            )
            return {
                "runtime_id": self.runtime_id,
                "session_id": self.session_id,
                "supervisor_state": self._state.value,
                "fault_state": None if self._fault is None else self._fault.code.value,
                "fault": (
                    None
                    if self._fault is None
                    else {
                        **asdict(self._fault),
                        "code": self._fault.code.value,
                    }
                ),
                "manual_hold_latched": self._manual_hold_latched,
                "can_still_command_reliably": self._can_command_reliably,
                "trajectory_frozen": self._state is not SupervisorState.RUNNING,
                "recovery_elapsed_s": recovery_elapsed,
                "recovery_timeout_s": self.recovery_timeout_s,
                **asdict(self._telemetry),
            }

    def _apply_timeout_locked(self) -> None:
        if (
            self._state is SupervisorState.RECOVERING_HOLD
            and self._recovery_started_s is not None
            and float(self._clock()) - self._recovery_started_s
            >= self.recovery_timeout_s
        ):
            self._state = SupervisorState.PAUSED_MANUAL
            self._manual_hold_latched = True
            self._recovery_started_s = None
            self._condition.notify_all()


class RecoveryCoordinator:
    """Run the mandatory fresh-state resync/replan transaction."""

    def __init__(self, supervisor: FaultSupervisor) -> None:
        self.supervisor = supervisor

    def attempt(
        self,
        acquire_fresh_state: Callable[[], object],
        reset_controller: Callable[[object], None],
        replan_original_goal: Callable[[object], object],
    ) -> object | None:
        state = self.supervisor.state
        if state is SupervisorState.RECOVERING_HOLD:
            if not self.supervisor.observe_recovered():
                return None
        elif state is not SupervisorState.AUTO_RESUME:
            return None
        try:
            fresh = acquire_fresh_state()
            reset_controller(fresh)
            plan = replan_original_goal(fresh)
        except Exception as error:
            self.supervisor.complete_auto_resume(False, str(error))
            return None
        if not self.supervisor.complete_auto_resume(True):
            return None
        return plan


def classify_runtime_exception(error: BaseException) -> FaultCode:
    """Conservative text adapter for legacy exceptions at the lifecycle edge."""
    text = f"{type(error).__name__}: {error}".lower()
    # cuRobo reports a frame-boundary collision as a planner/scene failure;
    # it is recoverable after rebuilding the scene from a fresh root snapshot.
    if any(
        marker in text
        for marker in (
            "start or end state in collision",
            "start state in collision",
            "end state in collision",
            "scene/frame mismatch",
            "planning frame revision mismatch",
        )
    ):
        return FaultCode.PLANNER_SOLVER_TRANSIENT
    if (
        "stale" in text
        or "state age" in text
        or "state_age" in text
        or "hardwarestateunavailable" in text
        or "lowstate" in text
        or "state unavailable" in text
    ):
        return FaultCode.STATE_AGE_TRANSIENT
    if "qp" in text or "inverse kinematic" in text or " ik " in f" {text} ":
        return FaultCode.IK_QP_TRANSIENT
    if "velocity" in text or "dq" in text:
        return FaultCode.VELOCITY_WARNING
    if "tracking" in text or "reference" in text:
        return FaultCode.TRACKING_REFERENCE_TRANSIENT
    if "planner" in text or "planning" in text or "solver" in text:
        return FaultCode.PLANNER_SOLVER_TRANSIENT
    if "collision" in text:
        return FaultCode.COLLISION_RISK
    if (
        "joint limit" in text
        or "joint-limit" in text
        or "soft limit" in text
        or "soft-limit" in text
    ):
        return FaultCode.JOINT_LIMIT_RISK
    if "nan" in text or "inf" in text or "non-finite" in text:
        return FaultCode.INVALID_STATE_OR_TARGET
    if "mode_machine" in text or "mode failure" in text:
        return FaultCode.MODE_FAILURE
    if "ownership" in text:
        return FaultCode.OWNERSHIP_FAILURE
    if "command stream" in text or "publisher" in text or "broken pipe" in text:
        return FaultCode.COMMAND_STREAM_LOSS
    if "e-stop" in text or "estop" in text:
        return FaultCode.ESTOP
    if "native protection" in text:
        return FaultCode.NATIVE_PROTECTION
    return FaultCode.RUNTIME_EXCEPTION


def classify_collision_failure(error: BaseException) -> str | None:
    """Classify collision reports into stable audit categories.

    The returned value is telemetry only.  Recovery decisions use the typed
    ``FaultCode`` and command-authority result, never this diagnostic string.
    Ambiguous cuRobo text intentionally returns ``None`` instead of inventing
    a self/environment classification.
    """
    text = f"{type(error).__name__}: {error}".lower()
    if "stale" in text or "state age" in text:
        return "STATE_STALE"
    if "scene/frame mismatch" in text or "planning frame revision" in text:
        return "PLANNING_FRAME_MISMATCH"
    is_self = "self collision" in text or "self-collision" in text
    is_environment = any(
        marker in text
        for marker in ("environment collision", "obstacle", "panel", "environment")
    )
    is_start = any(
        marker in text
        for marker in ("start state", "current state", "actual current")
    )
    is_end = any(marker in text for marker in ("end state", "endpoint"))
    if is_start and is_self:
        return "START_SELF_COLLISION"
    if is_start and is_environment:
        return "START_ENV_COLLISION"
    if is_end and is_self:
        return "END_SELF_COLLISION"
    if is_end and is_environment:
        return "END_ENV_COLLISION"
    return None
