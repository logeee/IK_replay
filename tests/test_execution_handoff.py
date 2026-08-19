from __future__ import annotations

import unittest

import numpy as np

from adapters.reach.execution import (
    COMMAND_SNAPSHOT_MAX_AGE_S,
    _build_control_waypoints,
    _validated_command_snapshot,
)


class ExecutionHandoffTests(unittest.TestCase):
    def test_plan_keeps_measured_start_but_control_uses_last_sent(self):
        measured = np.array([0.10, -0.20, 0.30])
        last_sent = np.array([0.15, -0.18, 0.30])
        planned = [measured.copy(), np.array([0.20, -0.10, 0.35])]

        control = _build_control_waypoints(planned, last_sent)

        np.testing.assert_allclose(planned[0], measured)
        np.testing.assert_allclose(control[0], last_sent)
        np.testing.assert_allclose(control[1], planned[1])

    def test_valid_snapshot_preserves_published_command_and_tau(self):
        snapshot = {
            "q_rad": [0.15, -0.18, 0.30],
            "tau_ff_nm": [3.5, 0.2, -1.0],
            "sent_at_monotonic": 10.0,
            "sequence": 42,
        }

        q, meta = _validated_command_snapshot(snapshot, 3, now=10.1)

        np.testing.assert_allclose(q, snapshot["q_rad"])
        self.assertEqual(meta["sequence"], 42)
        self.assertEqual(meta["tau_ff_nm"], snapshot["tau_ff_nm"])

    def test_stale_snapshot_is_rejected(self):
        snapshot = {
            "q_rad": [0.1, 0.2],
            "tau_ff_nm": [0.0, 0.0],
            "sent_at_monotonic": 10.0,
            "sequence": 1,
        }

        with self.assertRaisesRegex(RuntimeError, "过期"):
            _validated_command_snapshot(
                snapshot, 2, now=10.0 + COMMAND_SNAPSHOT_MAX_AGE_S + 0.01)


if __name__ == "__main__":
    unittest.main()
