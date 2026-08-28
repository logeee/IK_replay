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

    def test_manual_reverse_path_records_remote_to_close(self):
        yolo = mock.Mock()
        yolo.scene.side_effect = [
            {"ok": True, "scene": "远方", "jpeg_b64": "head",
             "wrist_jpeg_b64": "wrist"},
            {"ok": True, "scene": "就地", "jpeg_b64": "after"},
        ]
        with (
            mock.patch.object(flip_verification, "YoloClient", return_value=yolo),
            mock.patch.object(
                flip_verification,
                "save_flip_evidence",
                return_value={"head_saved": True, "wrist_saved": True},
            ) as save,
        ):
            before = flip_verification.capture_manual_before(
                {"record": "20260828_120000_1234abcd"}
            )
            after = flip_verification.verify_manual_after(before)

        self.assertEqual(
            (before["flip_from"], before["flip_to"]), ("远方", "就地")
        )
        self.assertTrue(after["success"])
        self.assertEqual(save.call_args_list[0].kwargs["flip_from"], "远方")
        self.assertEqual(save.call_args_list[1].kwargs["flip_to"], "就地")

    def test_manual_failures_are_persisted_for_history_viewer(self):
        yolo = mock.Mock()
        yolo.scene.return_value = {
            "ok": False,
            "error": "YOLO 服务不可达",
        }
        with (
            mock.patch.object(flip_verification, "YoloClient", return_value=yolo),
            mock.patch.object(
                flip_verification,
                "save_flip_evidence",
                return_value={"head_saved": False, "wrist_saved": False},
            ) as save,
            mock.patch.object(flip_verification.time, "sleep"),
        ):
            result = flip_verification.capture_manual_before(
                {
                    "record": "20260828_120000_1234abcd",
                    "flip_from": "就地",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "YOLO 服务不可达")
        self.assertEqual(save.call_args.args[1], "before")
        persisted = save.call_args.args[2]
        self.assertFalse(persisted["ok"])
        self.assertEqual(persisted["error"], result["error"])

    def test_indeterminate_after_stage_retains_image_and_error(self):
        yolo = mock.Mock()
        yolo.scene.return_value = {
            "ok": True,
            "scene": None,
            "jpeg_b64": "head",
        }
        with (
            mock.patch.object(flip_verification, "YoloClient", return_value=yolo),
            mock.patch.object(
                flip_verification,
                "save_flip_evidence",
                return_value={"head_saved": True, "wrist_saved": False},
            ) as save,
            mock.patch.object(flip_verification.time, "sleep"),
        ):
            result = flip_verification.verify_manual_after(
                {
                    "record": "20260828_120000_1234abcd",
                    "flip_from": "就地",
                    "flip_to": "远方",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "拨动后 YOLO 无结论")
        self.assertEqual(save.call_args.args[1], "after")
        persisted = save.call_args.args[2]
        self.assertEqual(persisted["jpeg_b64"], "head")
        self.assertFalse(persisted["ok"])
        self.assertEqual(persisted["error"], result["error"])

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
                {"ok": False, "error": "YOLO 服务不可达"},
            )
            result = json.loads((record_dir / "flip_result.json").read_text())
            self.assertFalse(result["after"]["ok"])
            self.assertEqual(result["after"]["error"], "YOLO 服务不可达")

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

    def test_flow_context_is_attached_to_pick_meta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = "20260828_120000_1234abcd"
            record_dir = root / record
            record_dir.mkdir()
            (record_dir / "meta.json").write_text(
                json.dumps({"adjustment_wall_mm": {"x": 25, "y": 10, "z": -20}}),
                encoding="utf-8",
            )

            flow = object.__new__(SwitchFlow)
            flow._PICK_HISTORY_DIR = root
            flow._measured_distance_m = 0.53
            flow._current_pose = {
                "name": "0.49-起手式新",
                "file": "0.49-起手式新_20260822_031632.json",
                "manual": False,
                "min_distance_m": 0.49,
            }
            flow.max_flip_rounds = 3
            flow.lift_base_m = 0.01
            flow.lift_step_m = 0.01
            flow.lift_max_m = 0.03
            flow.lift_m = 0.02
            flow.approach_offset_m = 0.0
            flow._log = mock.Mock()

            flow._save_pick_flow_context(
                {
                    "record": record,
                    "p_root": [0.1, 0.2, 0.3],
                },
                round_no=2,
                target_lift_m=0.02,
                effective_target_root_m=[0.1, 0.2, 0.32],
            )

            meta = json.loads((record_dir / "meta.json").read_text())
            self.assertEqual(meta["adjustment_wall_mm"]["x"], 25)
            context = meta["flow_context"]
            self.assertEqual(context["distance_m"], 0.53)
            self.assertEqual(context["opening_pose"]["name"], "0.49-起手式新")
            self.assertEqual(context["opening_pose"]["min_distance_m"], 0.49)
            self.assertEqual(context["round"], 2)
            self.assertEqual(context["target_lift_m"], 0.02)
            self.assertEqual(context["effective_target_root_m"], [0.1, 0.2, 0.32])

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
