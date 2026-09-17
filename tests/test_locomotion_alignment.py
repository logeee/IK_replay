from __future__ import annotations

import threading
import unittest
from unittest import mock

from adapters.reach import locomotion


class _LocoClient:
    def __init__(self) -> None:
        self.velocity_calls: list[tuple[float, float, float, float]] = []
        self.stop_calls = 0

    def SetVelocity(self, vx: float, vy: float, omega: float, duration: float) -> int:
        self.velocity_calls.append((vx, vy, omega, duration))
        return 0

    def StopMove(self) -> None:
        self.stop_calls += 1


class RaisedArmAlignmentTests(unittest.TestCase):
    def test_negative_yaw_is_corrected_immediately_instead_of_waiting(self) -> None:
        loco = _LocoClient()
        measurements = iter(
            [
                {"ok": True, "yaw_err_deg": -11.92, "points_used": 1000},
                {"ok": True, "yaw_err_deg": 0.0, "points_used": 1000},
            ]
        )
        events: list[dict] = []

        with (
            mock.patch.object(locomotion, "_get_loco_client", return_value=loco),
            mock.patch.object(locomotion, "_arm_raised", return_value=True),
            mock.patch.object(
                locomotion, "_fit_view_plane", side_effect=lambda *_: next(measurements)
            ),
            mock.patch.object(locomotion, "_align_log", side_effect=events.append),
            mock.patch.object(locomotion.state, "align_cancel", threading.Event()),
        ):
            locomotion._align_loop_hold(2.8, 0.3, 1.0, 0.0)

        self.assertEqual(len(loco.velocity_calls), 1)
        self.assertLess(loco.velocity_calls[0][2], 0.0)
        self.assertFalse(any(event.get("event") == "one_way_wait" for event in events))
        self.assertIn("对中完成", locomotion.state.align_message)


if __name__ == "__main__":
    unittest.main()
