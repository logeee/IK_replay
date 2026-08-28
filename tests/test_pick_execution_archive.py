from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.pick_execution_archive import (
    append_execution,
    backfill_executions,
    load_embedded_executions,
    load_record_executions,
)
from tools import picks_server


class PickExecutionArchiveTests(unittest.TestCase):
    def test_execution_is_embedded_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            history = Path(temporary)
            record = "20260828_165318_e5e46dac"
            record_dir = history / record
            record_dir.mkdir()
            execution = {
                "id": "exec-1",
                "pick_context": {
                    "record": record,
                    "capture_id": "capture-1",
                },
                "torso_trace": [{"t": 0.0}],
            }

            first = append_execution(history, execution)
            second = append_execution(history, execution)

            self.assertEqual(first, record_dir / "executions.jsonl")
            self.assertEqual(second, first)
            self.assertEqual(load_record_executions(record_dir), [execution])
            self.assertEqual(load_embedded_executions(history), [execution])

    def test_backfill_matches_old_log_by_capture_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            history = Path(temporary)
            record = "20260828_165318_e5e46dac"
            record_dir = history / record
            record_dir.mkdir()
            (record_dir / "meta.json").write_text(
                json.dumps({"capture_id": "capture-old"}),
                encoding="utf-8",
            )
            execution = {
                "id": "exec-old",
                "pick_context": {"capture_id": "capture-old"},
                "torso_drift": {"target_shift_mm": 12.3},
            }

            first = backfill_executions(history, [execution])
            second = backfill_executions(history, [execution])

            self.assertEqual(first, (1, 0))
            self.assertEqual(second, (0, 0))
            self.assertEqual(load_record_executions(record_dir), [execution])

    def test_unrelated_execution_is_not_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            history = Path(temporary)
            history.joinpath("20260828_165318_e5e46dac").mkdir()

            result = backfill_executions(
                history,
                [{"id": "other", "pick_context": {"capture_id": "unknown"}}],
            )

            self.assertEqual(result, (0, 1))
            self.assertEqual(load_embedded_executions(history), [])

    def test_history_api_reads_embedded_record_without_central_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            history = root / "pick_history"
            record = "20260828_165318_e5e46dac"
            record_dir = history / record
            record_dir.mkdir(parents=True)
            execution = {
                "id": "portable-exec",
                "ts": "2026-08-28T16:54:00",
                "result": "done",
                "pick_context": {"capture_id": "portable-capture"},
                "tcp": {},
                "torso_drift": {},
                "torso_trace": [{"t": 0.0}, {"t": 0.2}],
            }
            append_execution(
                history,
                {
                    **execution,
                    "pick_context": {
                        **execution["pick_context"],
                        "record": record,
                    },
                },
            )
            previous_history = picks_server.PICK_HISTORY_DIR
            previous_logs = picks_server.REACH_LOG_DIR
            try:
                picks_server.PICK_HISTORY_DIR = history
                picks_server.REACH_LOG_DIR = root / "missing-reach-logs"
                response = picks_server.executions_list(
                    capture_id="portable-capture"
                )
            finally:
                picks_server.PICK_HISTORY_DIR = previous_history
                picks_server.REACH_LOG_DIR = previous_logs

            self.assertTrue(response["ok"])
            self.assertEqual(len(response["records"]), 1)
            self.assertEqual(response["records"][0]["id"], "portable-exec")
            self.assertEqual(response["records"][0]["trace_len"], 2)


if __name__ == "__main__":
    unittest.main()
