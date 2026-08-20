from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.responses import FileResponse

from api import dispatch


class DispatchDashboardTests(unittest.TestCase):
    def test_root_serves_workflow_dashboard(self):
        response = dispatch.index()
        self.assertIsInstance(response, FileResponse)
        self.assertTrue(str(response.path).endswith("web/dispatch.html"))
        html = dispatch.WEB_DIR.joinpath("dispatch.html").read_text(encoding="utf-8")
        self.assertIn("拨闸任务流程监控", html)
        self.assertIn('fetch("/task/status"', html)

    def test_dashboard_inline_javascript_is_valid(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        html = dispatch.WEB_DIR.joinpath("dispatch.html").read_text(encoding="utf-8")
        match = re.search(r"<script>(.*)</script>", html, flags=re.DOTALL)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "dispatch.js"
            script.write_text(match.group(1), encoding="utf-8")
            result = subprocess.run(
                [node, "--check", str(script)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_finished_task_is_counted_exactly_once(self):
        with dispatch._lock:
            original = dict(dispatch._task_stats)
            try:
                dispatch._task_stats.update(
                    accepted=1, succeeded=0, failed=0, rejected_busy=0
                )
                task = {"result": {"ok": True}, "stats_counted": False}
                dispatch._count_finished_task_locked(task)
                dispatch._count_finished_task_locked(task)
                self.assertEqual(dispatch._task_stats["succeeded"], 1)
                self.assertEqual(dispatch._task_stats["failed"], 0)
            finally:
                dispatch._task_stats.clear()
                dispatch._task_stats.update(original)

    def test_idle_status_exposes_service_metrics_for_dashboard(self):
        with dispatch._lock:
            original_task = dispatch._task
            original_check = dispatch._check
            dispatch._task = None
            dispatch._check = None
        try:
            with patch.object(dispatch, "_reach_alive", return_value=False):
                status = dispatch.task_status()
        finally:
            with dispatch._lock:
                dispatch._task = original_task
                dispatch._check = original_check
        self.assertEqual(status["state"], "idle")
        self.assertIn("service", status)
        self.assertIn("succeeded", status["service"])
        self.assertEqual(status["log"], [])


if __name__ == "__main__":
    unittest.main()
