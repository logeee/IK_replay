from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from adapters.reach import execution, recordings
from adapters.reach.execution import (
    COMMAND_SNAPSHOT_MAX_AGE_S,
    _build_control_waypoints,
    _json_safe_value,
    _validated_command_snapshot,
)


class _SequenceController:
    def read_measured(self):
        return [0.0, 0.0]

    def status(self):
        return {"float": False}

    def command_snapshot(self):
        return {
            "q_rad": [0.02, -0.01],
            "tau_ff_nm": [0.5, -0.2],
            "sent_at_monotonic": time.monotonic(),
            "sequence": 7,
        }


class ExecutionHandoffTests(unittest.TestCase):
    def test_numpy_command_snapshot_is_json_serializable(self):
        snapshot = {
            "q_rad": np.array([0.15, -0.18]),
            "tau_ff_nm": np.array([3.5, 0.2]),
            "sent_at_monotonic": np.float64(10.0),
            "sequence": np.int64(42),
        }

        encoded = _json_safe_value(snapshot)

        self.assertEqual(encoded["q_rad"], [0.15, -0.18])
        self.assertEqual(encoded["tau_ff_nm"], [3.5, 0.2])
        self.assertEqual(encoded["sequence"], 42)
        json.dumps(encoded)

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

    def test_execution_log_is_structured_and_exposed_without_log_directory(self):
        class Controller:
            def status(self):
                return {
                    "cmd_rad": [0.19, -0.11],
                    "measured_rad": [0.18, -0.12],
                    "grav_alpha": 1.1,
                    "payload_kg": 0.5,
                    "kp": 40.0,
                    "kd": 1.0,
                }

        state = execution.state
        attributes = [
            "controller",
            "joint_names",
            "log_dir",
            "pick_target_root",
            "pick_target_torso",
            "pick_pixel",
            "pick_context",
            "pick_torso",
            "torso_diag",
            "gravity_profile",
            "robot_id",
            "chain_id",
        ]
        saved = {name: getattr(state, name) for name in attributes}
        saved_history = list(state.execution_history)
        try:
            state.controller = Controller()
            state.joint_names = ["j1", "j2"]
            state.log_dir = None
            state.pick_target_root = [0.4, 0.0, 0.8]
            state.pick_target_torso = [0.4, 0.0, 0.8]
            state.pick_pixel = [300, 200]
            state.pick_context = {
                "selection_mode": "frozen_rgbd_pointcloud",
                "source_frame_id": "frame-8",
                "capture_id": "capture-8",
                "p_root": [0.4, 0.0, 0.8],
            }
            state.pick_torso = None
            state.torso_diag = None
            state.gravity_profile = {"version": "0.1.0"}
            state.robot_id = "h2"
            state.chain_id = "right_arm"
            state.execution_history.clear()
            with (
                mock.patch.object(
                    execution,
                    "_tcp_position",
                    side_effect=[[0.401, 0.0, 0.8], [0.399, 0.0, 0.797]],
                ),
                mock.patch.object(execution, "_read_torso", return_value=None),
            ):
                execution._log_exec(
                    "主轨迹",
                    "done",
                    [0.2, -0.1],
                    duration=4.0,
                    speed=0.2,
                )

            response = execution.reach_executions(
                limit=5, pointcloud_only=True
            )
            self.assertTrue(response["ok"])
            self.assertEqual(len(response["executions"]), 1)
            record = response["executions"][0]
            self.assertEqual(record["segment"], "主轨迹")
            self.assertEqual(record["gravity_profile"]["version"], "0.1.0")
            self.assertEqual(
                record["pick_context"]["source_frame_id"], "frame-8"
            )
            self.assertAlmostEqual(record["tcp"]["ik_mm"], 1.0)
            detail = execution.reach_execution(record["id"])
            self.assertEqual(detail["execution"]["id"], record["id"])
            json.dumps(record)
        finally:
            for name, value in saved.items():
                setattr(state, name, value)
            state.execution_history.clear()
            state.execution_history.extend(saved_history)

    def test_sequence_replay_passes_last_sent_command_to_exec_loop(self):
        state = recordings.state
        attributes = [
            "controller", "sequences_dir", "waypoints_dir", "joint_names",
            "exec_running", "exec_progress", "exec_message", "exec_thread",
        ]
        saved = {name: getattr(state, name) for name in attributes}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sequences = root / "sequences"
            waypoints = root / "waypoints"
            sequences.mkdir()
            waypoints.mkdir()
            (waypoints / "target.json").write_text(
                json.dumps({
                    "name": "target",
                    "named_joints": {"j1": 0.1, "j2": 0.1},
                })
            )
            (sequences / "sequence.json").write_text(
                json.dumps({
                    "name": "sequence",
                    "waypoints": ["target.json"],
                    "trajectory": {
                        "frames": [[0.0, 0.0], [0.1, 0.1]],
                    },
                })
            )
            try:
                state.controller = _SequenceController()
                state.sequences_dir = sequences
                state.waypoints_dir = waypoints
                state.joint_names = ["j1", "j2"]
                state.exec_running = False
                fake_thread = mock.Mock()
                with mock.patch.object(
                    recordings.threading, "Thread", return_value=fake_thread
                ) as thread:
                    result = recordings.reach_run_sequence(
                        {"file": "sequence.json"}
                    )
                self.assertTrue(result["ok"])
                kwargs = thread.call_args.kwargs["kwargs"]
                np.testing.assert_allclose(
                    kwargs["command_start_q"], [0.02, -0.01]
                )
                self.assertEqual(
                    kwargs["command_handoff"]["source"], "sequence_replay"
                )
                fake_thread.start.assert_called_once()
            finally:
                for name, value in saved.items():
                    setattr(state, name, value)


if __name__ == "__main__":
    unittest.main()
