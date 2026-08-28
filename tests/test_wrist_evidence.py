from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.responses import Response

from adapters.reach import flip_verification, perception
from api import yolo_server
from api.flow import SwitchFlow


class WristEvidenceTest(unittest.TestCase):
    def test_manual_path_captures_before_and_verifies_after(self):
        yolo = mock.Mock()
        yolo.scene.side_effect = [
            {"ok": True, "scene": "就地", "jpeg_b64": "head", "wrist_jpeg_b64": "wrist"},
            {"ok": True, "scene": "就地", "jpeg_b64": "after-1"},
            {"ok": True, "scene": "远方", "jpeg_b64": "after-2"},
        ]
        with (
            mock.patch.object(flip_verification, "YoloClient", return_value=yolo),
            mock.patch.object(
                flip_verification,
                "save_flip_evidence",
                return_value={"head_saved": True, "wrist_saved": True},
            ) as save,
            mock.patch.object(flip_verification.time, "sleep"),
        ):
            before = flip_verification.capture_manual_before(
                {"record": "20260828_120000_1234abcd"}
            )
            after = flip_verification.verify_manual_after(before)

        self.assertTrue(before["ok"])
        self.assertEqual(before["flip_from"], "就地")
        self.assertEqual(before["flip_to"], "远方")
        self.assertTrue(after["ok"])
        self.assertTrue(after["success"])
        self.assertEqual(save.call_args_list[0].args[1], "before")
        self.assertEqual(save.call_args_list[1].args[1], "after")
        yolo.scene.assert_any_call(include_image=True, include_wrist=True)

    def test_7005_record_name_attaches_to_current_18001_pick(self):
        previous_context = perception.state.pick_context
        previous_revision = perception.state.pick_revision
        perception.state.pick_context = {"capture_id": "capture-1"}
        perception.state.pick_revision = 4
        try:
            result = perception.attach_pick_record(
                {
                    "capture_id": "capture-1",
                    "record": "20260828_120000_1234abcd",
                }
            )
            self.assertTrue(result["ok"])
            self.assertEqual(perception.state.pick_context["record"], result["record"])
            self.assertEqual(result["revision"], 5)
        finally:
            perception.state.pick_context = previous_context
            perception.state.pick_revision = previous_revision

    def test_before_capture_requests_right_wrist_once(self):
        flow = object.__new__(SwitchFlow)
        flow.yolo = mock.Mock()
        flow.yolo.scene.return_value = {"ok": True}
        flow._last_pick_record = "20260828_120000_1234abcd"
        flow._save_flip_evidence = mock.Mock()
        flow._log = mock.Mock()

        flow._flip_evidence_before()

        flow.yolo.scene.assert_called_once_with(
            include_image=True,
            include_wrist=True,
        )
        flow._save_flip_evidence.assert_called_once_with(
            "before",
            flow.yolo.scene.return_value,
        )

    def test_flow_saves_head_and_wrist_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = "20260828_120000_1234abcd"
            (root / record).mkdir()
            flow = object.__new__(SwitchFlow)
            flow._PICK_HISTORY_DIR = root
            flow._last_pick_record = record
            flow._last_flip_round = 2
            flow.flip_from = "就地"
            flow.flip_to = "远方"
            flow._log = mock.Mock()

            flow._save_flip_evidence(
                "before",
                {
                    "scene": "就地",
                    "conf": 0.91,
                    "boxes": [],
                    "jpeg_b64": base64.b64encode(b"head-jpeg").decode(),
                    "wrist_jpeg_b64": base64.b64encode(b"wrist-jpeg").decode(),
                },
            )

            record_dir = root / record
            self.assertEqual((record_dir / "flip_before.jpg").read_bytes(), b"head-jpeg")
            self.assertEqual(
                (record_dir / "flip_before_wrist.jpg").read_bytes(), b"wrist-jpeg"
            )
            result = json.loads((record_dir / "flip_result.json").read_text())
            self.assertTrue(result["before"]["has_image"])
            self.assertTrue(result["before"]["has_wrist_image"])

            flow._save_flip_evidence(
                "after",
                {
                    "scene": "远方",
                    "jpeg_b64": base64.b64encode(b"head-after").decode(),
                    "wrist_jpeg_b64": base64.b64encode(b"must-not-save").decode(),
                },
                success=True,
            )
            self.assertFalse((record_dir / "flip_after_wrist.jpg").exists())
            result = json.loads((record_dir / "flip_result.json").read_text())
            self.assertNotIn("has_wrist_image", result["after"])

    def test_yolo_scene_adds_wrist_frame_without_using_it_for_decision(self):
        head = b"\xff\xd8head\xff\xd9"
        wrist = b"\xff\xd8wrist\xff\xd9"
        with (
            mock.patch.object(
                yolo_server,
                "_grab_and_infer",
                return_value={"ok": True, "boxes": [], "jpeg": head},
            ),
            mock.patch.object(yolo_server, "_grab_wrist_jpeg", return_value=wrist),
        ):
            result = yolo_server.scene(include_image=True, include_wrist=True)
        self.assertIsNone(result["scene"])
        self.assertEqual(base64.b64decode(result["jpeg_b64"]), head)
        self.assertEqual(base64.b64decode(result["wrist_jpeg_b64"]), wrist)

    def test_wrist_snapshot_returns_latest_jpeg(self):
        camera = mock.Mock()
        camera.get_jpeg.return_value = b"\xff\xd8frame\xff\xd9"
        previous = perception.state.wrist_camera
        perception.state.wrist_camera = camera
        try:
            response = perception.wrist_snapshot()
        finally:
            perception.state.wrist_camera = previous
        self.assertIsInstance(response, Response)
        self.assertEqual(response.body, b"\xff\xd8frame\xff\xd9")
        self.assertEqual(response.media_type, "image/jpeg")


if __name__ == "__main__":
    unittest.main()
