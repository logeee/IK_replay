"""Time-parameterize a geometric joint path for fixed-rate execution.

The reach planner returns collision-checked joint waypoints without timestamps.
Legacy execution presents each sparse waypoint to the rate limiter and may reach
it early, then wait for the next waypoint.  This module follows the same joint
polyline with one global quintic progress law, sampled at the same 50 Hz rate as
the arm command loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np


QUINTIC_PEAK_VELOCITY = 1.875
DEFAULT_RATE_HZ = 50.0
DEFAULT_SPEED_UTILIZATION = 0.9


@dataclass(frozen=True)
class TimedTrajectory:
    frames: list[np.ndarray]
    duration_s: float
    diagnostics: dict[str, float | int]


def _quintic_progress(u: np.ndarray) -> np.ndarray:
    return 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5


def build_timed_trajectory(
    path: Sequence[np.ndarray],
    requested_duration_s: float,
    max_speed_rad_s: float,
    *,
    rate_hz: float = DEFAULT_RATE_HZ,
    speed_utilization: float = DEFAULT_SPEED_UTILIZATION,
) -> TimedTrajectory:
    """Return a 50 Hz trajectory that continuously traverses ``path``.

    Path distance uses the L-infinity joint norm, matching H2ArmController's
    synchronized vector limiter.  Linear interpolation along cumulative path
    distance follows the original joint-space segments (with at most one 50 Hz
    sample chord across a corner).  A single quintic progress law eases the
    whole motion at its two ends and does not stop at intermediate IK waypoints.
    """

    if len(path) < 2:
        raise ValueError("timed trajectory requires at least two path frames")
    points = [np.asarray(frame, dtype=float).reshape(-1) for frame in path]
    joint_count = points[0].size
    if joint_count == 0 or any(frame.size != joint_count for frame in points):
        raise ValueError("timed trajectory path has inconsistent joint counts")
    if not all(np.all(np.isfinite(frame)) for frame in points):
        raise ValueError("timed trajectory path contains NaN or infinity")

    duration_requested = float(requested_duration_s)
    speed_limit = float(max_speed_rad_s)
    sample_rate = float(rate_hz)
    utilization = float(speed_utilization)
    if not math.isfinite(duration_requested) or duration_requested <= 0.0:
        raise ValueError("requested duration must be positive and finite")
    if not math.isfinite(speed_limit) or speed_limit <= 0.0:
        raise ValueError("max joint speed must be positive and finite")
    if not math.isfinite(sample_rate) or sample_rate <= 0.0:
        raise ValueError("trajectory rate must be positive and finite")
    if not math.isfinite(utilization) or not 0.0 < utilization <= 1.0:
        raise ValueError("speed utilization must be in (0, 1]")

    # Consecutive duplicates have zero path length and make segment lookup
    # ambiguous.  Removing them does not alter the geometric path.
    deduped = [points[0]]
    for frame in points[1:]:
        if float(np.max(np.abs(frame - deduped[-1]))) > 1.0e-12:
            deduped.append(frame)

    if len(deduped) == 1:
        interval_count = max(1, int(math.ceil(duration_requested * sample_rate)))
        frames = [deduped[0].copy() for _ in range(interval_count + 1)]
        return TimedTrajectory(
            frames,
            duration_requested,
            {
                "source_frame_count": len(path),
                "source_unique_frame_count": 1,
                "timed_frame_count": len(frames),
                "rate_hz": sample_rate,
                "requested_duration_s": duration_requested,
                "actual_duration_s": duration_requested,
                "minimum_duration_s": 0.0,
                "path_length_linf_rad": 0.0,
                "max_frame_step_rad": 0.0,
                "max_sampled_speed_rad_s": 0.0,
            },
        )

    q = np.stack(deduped)
    segment_lengths = np.max(np.abs(np.diff(q, axis=0)), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(segment_lengths)))
    path_length = float(cumulative[-1])

    # The normalized quintic has peak derivative 1.875.  Keep ten percent
    # headroom so the downstream limiter follows the scheduled samples instead
    # of clipping them and destroying their timing.
    minimum_duration = (
        QUINTIC_PEAK_VELOCITY * path_length / (speed_limit * utilization)
    )
    duration = max(duration_requested, minimum_duration)
    interval_count = max(1, int(math.ceil(duration * sample_rate)))
    sample_dt = duration / interval_count
    u = np.linspace(0.0, 1.0, interval_count + 1)
    sample_distance = path_length * _quintic_progress(u)

    frames: list[np.ndarray] = []
    for distance in sample_distance:
        if distance >= path_length:
            frames.append(q[-1].copy())
            continue
        index = int(np.searchsorted(cumulative, distance, side="right") - 1)
        index = min(max(index, 0), len(segment_lengths) - 1)
        alpha = (distance - cumulative[index]) / segment_lengths[index]
        frames.append(q[index] + (q[index + 1] - q[index]) * float(alpha))
    frames[0] = q[0].copy()
    frames[-1] = q[-1].copy()

    max_step = max(
        float(np.max(np.abs(right - left)))
        for left, right in zip(frames, frames[1:])
    )
    diagnostics: dict[str, float | int] = {
        "source_frame_count": len(path),
        "source_unique_frame_count": len(deduped),
        "timed_frame_count": len(frames),
        "rate_hz": sample_rate,
        "requested_duration_s": duration_requested,
        "actual_duration_s": duration,
        "minimum_duration_s": minimum_duration,
        "path_length_linf_rad": path_length,
        "max_frame_step_rad": max_step,
        "max_sampled_speed_rad_s": max_step / sample_dt,
    }
    return TimedTrajectory(frames, duration, diagnostics)
