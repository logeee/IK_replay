from __future__ import annotations

import unittest

from api import yolo_server


class YoloDegradedModeTests(unittest.TestCase):
    def setUp(self):
        self.old_model = yolo_server._model
        self.old_model_name = yolo_server._model_name
        self.old_model_error = yolo_server._model_error

    def tearDown(self):
        yolo_server._model = self.old_model
        yolo_server._model_name = self.old_model_name
        yolo_server._model_error = self.old_model_error

    def test_missing_model_keeps_health_endpoint_available(self):
        yolo_server._model = None
        yolo_server._model_name = "missing.pt"
        yolo_server._model_error = "YOLO 模型不存在: missing.pt"

        status = yolo_server.status()
        result = yolo_server._grab_and_infer()

        self.assertTrue(status["ok"])
        self.assertFalse(status["model_available"])
        self.assertEqual(status["model_error"], yolo_server._model_error)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], yolo_server._model_error)


if __name__ == "__main__":
    unittest.main()
