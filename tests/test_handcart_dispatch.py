from __future__ import annotations

import unittest
from unittest import mock

from fastapi.responses import JSONResponse

from api import dispatch
from api.handcart_client import HandcartClient


class HandcartDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        with dispatch._lock:
            self.old_task = dispatch._task
            self.old_check = dispatch._check
            self.old_stats = dict(dispatch._task_stats)
            dispatch._task = None
            dispatch._check = None
            dispatch._task_stats.update(
                accepted=0, succeeded=0, failed=0, rejected_busy=0
            )

    def tearDown(self) -> None:
        with dispatch._lock:
            dispatch._task = self.old_task
            dispatch._check = self.old_check
            dispatch._task_stats.clear()
            dispatch._task_stats.update(self.old_stats)

    def test_right_counterclockwise_maps_to_motor_left(self) -> None:
        with mock.patch.object(dispatch.threading, "Thread") as thread:
            response = dispatch.task_submit({
                "hand": "right",
                "task": "counterclockwise",
            })

        self.assertTrue(response["ok"])
        self.assertEqual(dispatch._task["backend"], "handcart_8876")
        self.assertEqual(dispatch._task["motor_action"], "left")
        thread.return_value.start.assert_called_once_with()

    def test_right_clockwise_maps_to_motor_right(self) -> None:
        with mock.patch.object(dispatch.threading, "Thread"):
            dispatch.task_submit({"hand": "right", "task": "clockwise"})
        self.assertEqual(dispatch._task["motor_action"], "right")

    def test_second_task_is_rejected_while_handcart_runs(self) -> None:
        dispatch._task = {
            "id": "busy-task",
            "state": "running",
            "backend": "handcart_8876",
        }
        response = dispatch.task_submit({
            "hand": "right",
            "task": "clockwise",
        })
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(dispatch._task_stats["rejected_busy"], 1)

    def test_handcart_worker_normalizes_done(self) -> None:
        client = mock.Mock()
        client.start.return_value = {"job_id": "job-7", "state": "WAITING"}
        client.status.side_effect = [
            {"job_id": "job-7", "state": "RUNNING"},
            {"job_id": "job-7", "state": "DONE", "message": "完成"},
        ]
        task = {
            "state": "starting",
            "motor_action": "left",
            "log": [],
            "result": None,
            "finished_at": None,
            "stats_counted": False,
        }
        dispatch._task_stats["accepted"] = 1

        with (
            mock.patch.object(dispatch, "HandcartClient", return_value=client),
            mock.patch.object(dispatch.time, "sleep"),
        ):
            dispatch._run_handcart_task(task)

        client.start.assert_called_once_with("left")
        self.assertEqual(task["downstream_job_id"], "job-7")
        self.assertEqual(task["state"], "done")
        self.assertTrue(task["result"]["ok"])
        self.assertEqual(task["result"]["code_name"], "SUCCESS")
        self.assertEqual(dispatch._task_stats["succeeded"], 1)

    def test_status_exposes_unified_hand_and_task(self) -> None:
        dispatch._task = {
            "id": "task-1",
            "state": "running",
            "hand": "right",
            "public_task": "clockwise",
            "backend": "handcart_8876",
            "started_at": "now",
            "finished_at": None,
            "downstream_job_id": "job-1",
            "downstream": {"state": "RUNNING"},
            "result": None,
            "log": [],
        }
        status = dispatch.task_status()
        self.assertEqual(status["hand"], "right")
        self.assertEqual(status["task"], "clockwise")
        self.assertEqual(status["state"], "running")

    def test_hand_and_task_are_both_required(self) -> None:
        response = dispatch.task_submit({"hand": "left"})
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 422)

    def test_client_sends_fixed_process_restart_false(self) -> None:
        response = mock.Mock()
        response.ok = True
        response.json.return_value = {"job_id": "job-2"}
        client = HandcartClient("http://127.0.0.1:8876")
        client._session.post = mock.Mock(return_value=response)

        result = client.start("left")

        self.assertEqual(result["job_id"], "job-2")
        client._session.post.assert_called_once_with(
            "http://127.0.0.1:8876/v1/handcart/jobs",
            json={"process_restart": False, "motor_action": "left"},
            timeout=10.0,
        )


if __name__ == "__main__":
    unittest.main()
