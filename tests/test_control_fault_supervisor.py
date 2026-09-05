"""control/fault_supervisor.py：故障状态机语义（纯逻辑，无硬件）。

移植自 arm-motion-middleware v1.0.3 tests/unit/test_arm_motion_fault_supervisor.py 中
与 FaultSupervisor 相关的用例（control-plane 部分未移植）。核心不变量：
* 可恢复故障 -> RECOVERING_HOLD 冻结轨迹时钟、保持最后有效目标，绝不自动 release；
* 恢复严格按 fresh state -> reset -> replan -> resume；
* 人工 HOLD 优先级高于自动恢复，必须显式 RESUME；
* RUNNING 状态下拒绝 release；真正失去控制权时不再声称能 hold/release。
"""

from __future__ import annotations

import unittest

from control.fault_supervisor import (
    FaultCode,
    FaultSupervisor,
    PausableTrajectoryClock,
    RecoveryCoordinator,
    SupervisorCommandRejected,
    SupervisorState,
    classify_collision_failure,
    classify_runtime_exception,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_supervisor(clock: FakeClock, timeout: float = 2.0) -> FaultSupervisor:
    return FaultSupervisor("runtime-test", "right", recovery_timeout_s=timeout, clock=clock)


class FaultSupervisorTest(unittest.TestCase):
    def test_state_age_fault_freezes_clock_then_resyncs_replans_and_resumes(self) -> None:
        clock = FakeClock()
        task_clock = PausableTrajectoryClock(clock)
        state = make_supervisor(clock)
        clock.advance(0.5)
        self.assertAlmostEqual(task_clock.elapsed_s(), 0.5)
        state.report_fault(
            FaultCode.STATE_AGE_TRANSIENT, "state age 120 ms", can_still_command_reliably=True
        )
        task_clock.pause()
        clock.advance(0.4)
        self.assertAlmostEqual(task_clock.elapsed_s(), 0.5)
        calls: list = []
        plan = RecoveryCoordinator(state).attempt(
            lambda: calls.append("fresh") or {"root": "fresh"},
            lambda fresh: calls.append(("reset", fresh["root"])),
            lambda fresh: calls.append(("replan", fresh["root"])) or "new-plan",
        )
        task_clock.resume()
        self.assertEqual(plan, "new-plan")
        self.assertEqual(calls, ["fresh", ("reset", "fresh"), ("replan", "fresh")])
        self.assertIs(state.state, SupervisorState.RUNNING)
        clock.advance(0.3)
        self.assertAlmostEqual(task_clock.elapsed_s(), 0.8)

    def test_temporary_qp_failure_auto_recovers(self) -> None:
        clock = FakeClock()
        state = make_supervisor(clock)
        self.assertIs(
            state.report_fault(
                FaultCode.IK_QP_TRANSIENT, "temporary QP failure", can_still_command_reliably=True
            ),
            SupervisorState.RECOVERING_HOLD,
        )
        self.assertEqual(
            RecoveryCoordinator(state).attempt(lambda: object(), lambda _f: None, lambda _f: "plan"),
            "plan",
        )
        self.assertIs(state.state, SupervisorState.RUNNING)

    def test_recovery_timeout_enters_manual_pause_without_release(self) -> None:
        clock = FakeClock()
        state = make_supervisor(clock, timeout=1.0)
        state.report_fault(
            FaultCode.TRACKING_REFERENCE_TRANSIENT,
            "reference temporarily invalid",
            can_still_command_reliably=True,
        )
        clock.advance(1.01)
        self.assertIs(state.tick(), SupervisorState.PAUSED_MANUAL)
        snapshot = state.snapshot()
        self.assertIs(snapshot["can_still_command_reliably"], True)
        self.assertNotEqual(snapshot["supervisor_state"], SupervisorState.GRADUAL_RELEASE.value)

    def test_manual_hold_has_priority_and_blocks_automatic_resume(self) -> None:
        clock = FakeClock()
        state = make_supervisor(clock)
        state.report_fault(
            FaultCode.STATE_AGE_TRANSIENT, "brief stale state", can_still_command_reliably=True
        )
        state.manual_hold()
        self.assertFalse(state.observe_recovered())
        self.assertIs(state.state, SupervisorState.PAUSED_MANUAL)
        state.manual_resume()
        self.assertIs(state.state, SupervisorState.AUTO_RESUME)
        RecoveryCoordinator(state).attempt(lambda: object(), lambda _: None, lambda _: "plan")
        self.assertIs(state.state, SupervisorState.RUNNING)

    def test_manual_hold_during_replan_cancels_auto_resume_commit(self) -> None:
        clock = FakeClock()
        state = make_supervisor(clock)
        state.report_fault(
            FaultCode.IK_QP_TRANSIENT, "temporary QP failure", can_still_command_reliably=True
        )

        def replan(_fresh):
            state.manual_hold()
            return "plan-that-must-not-be-installed"

        self.assertIsNone(
            RecoveryCoordinator(state).attempt(lambda: object(), lambda _: None, replan)
        )
        self.assertIs(state.state, SupervisorState.PAUSED_MANUAL)
        self.assertIs(state.snapshot()["manual_hold_latched"], True)

    def test_command_authority_loss_never_claims_hold_or_release(self) -> None:
        clock = FakeClock()
        state = make_supervisor(clock)
        state.report_fault(FaultCode.MODE_FAILURE, "mode changed", can_still_command_reliably=False)
        self.assertIs(state.state, SupervisorState.CONTROL_AUTHORITY_LOST)
        with self.assertRaises(SupervisorCommandRejected):
            state.release()

    def test_release_requires_stopped_motion_then_is_gradual(self) -> None:
        clock = FakeClock()
        state = make_supervisor(clock)
        with self.assertRaisesRegex(SupervisorCommandRejected, "stopped"):
            state.release()
        state.manual_hold()
        state.release()
        self.assertIs(state.state, SupervisorState.GRADUAL_RELEASE)
        state.mark_released()
        self.assertIs(state.state, SupervisorState.STOPPED)

    def test_collision_diagnostics_use_structured_categories(self) -> None:
        cases = {
            "start state in self collision": "START_SELF_COLLISION",
            "start state environment collision": "START_ENV_COLLISION",
            "end state in self-collision": "END_SELF_COLLISION",
            "endpoint obstacle collision": "END_ENV_COLLISION",
            "planning frame revision mismatch": "PLANNING_FRAME_MISMATCH",
            "state age 400 ms": "STATE_STALE",
        }
        for message, expected in cases.items():
            self.assertEqual(classify_collision_failure(RuntimeError(message)), expected, message)
        self.assertIsNone(classify_collision_failure(RuntimeError("start or end state in collision")))

    def test_lowstate_unavailable_is_classified_as_recoverable_state_age(self) -> None:
        self.assertIs(
            classify_runtime_exception(
                RuntimeError("HardwareStateUnavailable: no H2 lowstate sample")
            ),
            FaultCode.STATE_AGE_TRANSIENT,
        )

    def test_soft_joint_limit_is_classified_as_joint_limit_risk(self) -> None:
        self.assertIs(
            classify_runtime_exception(RuntimeError("joint soft-limit margin 0.049 rad < 0.050 rad")),
            FaultCode.JOINT_LIMIT_RISK,
        )


if __name__ == "__main__":
    unittest.main()
