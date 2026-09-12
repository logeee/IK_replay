from __future__ import annotations

import unittest

import numpy as np

from control.timed_trajectory import build_timed_trajectory


class TimedTrajectoryTest(unittest.TestCase):
    def test_resamples_exact_polyline_at_fifty_hz_without_intermediate_stop(self):
        path = [
            np.array([0.0, 0.0]),
            np.array([0.1, 0.0]),
            np.array([0.1, 0.2]),
        ]
        result = build_timed_trajectory(path, 2.0, 0.4)

        self.assertEqual(len(result.frames), 101)
        np.testing.assert_allclose(result.frames[0], path[0])
        np.testing.assert_allclose(result.frames[-1], path[-1])
        self.assertEqual(result.diagnostics["rate_hz"], 50.0)
        # Resampling stays on one of the two original joint-space segments.
        for frame in result.frames:
            self.assertTrue(
                (abs(frame[1]) < 1e-12 and 0.0 <= frame[0] <= 0.1)
                or (abs(frame[0] - 0.1) < 1e-12 and 0.0 <= frame[1] <= 0.2)
            )
        # The via point is traversed; it is not duplicated as a scheduled hold.
        moving_steps = [
            np.max(np.abs(right - left))
            for left, right in zip(result.frames, result.frames[1:])
        ]
        self.assertEqual(sum(step == 0.0 for step in moving_steps), 0)

    def test_short_request_is_stretched_below_executor_speed(self):
        path = [np.zeros(2), np.array([1.0, 0.25])]
        result = build_timed_trajectory(path, 0.5, 0.4)

        self.assertGreater(result.duration_s, 0.5)
        self.assertLessEqual(result.diagnostics["max_sampled_speed_rad_s"], 0.4 * 0.9 + 1e-9)

    def test_duplicate_source_frames_are_removed(self):
        q0 = np.zeros(3)
        q1 = np.ones(3) * 0.1
        result = build_timed_trajectory([q0, q0.copy(), q1], 1.0, 0.4)

        self.assertEqual(result.diagnostics["source_frame_count"], 3)
        self.assertEqual(result.diagnostics["source_unique_frame_count"], 2)
        np.testing.assert_allclose(result.frames[-1], q1)


if __name__ == "__main__":
    unittest.main()
