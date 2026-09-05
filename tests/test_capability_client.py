"""core.capability_client 启动拜访 18000 的客户端行为。"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from core import capability_registry as reg
from core.capability_client import (
    CapabilityUnavailable,
    claim_sequence,
    describe_active,
    fetch_snapshot,
)


def _payload(registry: dict) -> dict:
    return {
        "ok": True,
        "registry": registry,
        "calibrations": [{
            "arm": "right_arm",
            "hand_id": "yinshi-1-right",
            "status": "ready",
        }],
        "meta": {},
    }


class _Server:
    """本地起一个最小 HTTP 服务模拟 18000。body 可换、可返回坏内容。"""

    def __init__(self, body: bytes, status: int = 200):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(outer.body)

            def do_GET(self):   # noqa: N802
                self._reply()

            def do_POST(self):   # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                outer.last_post = json.loads(
                    self.rfile.read(length).decode("utf-8") or "{}")
                self._reply()

            def log_message(self, *_):
                pass

        self.body = body
        self.status = status
        self.last_post: dict | None = None
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FetchSnapshotTests(unittest.TestCase):
    def test_valid_payload_roundtrip_with_local_validation(self):
        seed = reg.seed_registry()
        server = _Server(json.dumps(_payload(seed)).encode())
        self.addCleanup(server.close)
        payload = fetch_snapshot(server.url, attempts=1)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["registry"]["active"],
                         {"arm": "right_arm", "hand_id": "yinshi-1-right",
                          "motion_backend": "legacy"})
        # registry 经过本地 validate_registry 重校验（缺省参数已补齐）
        cap = payload["registry"]["capabilities"][0]
        self.assertIn("sidestep_cm", cap["method_params"])

    def test_unreachable_raises_with_hint(self):
        with self.assertRaisesRegex(CapabilityUnavailable, "capability.sh"):
            fetch_snapshot("http://127.0.0.1:1", attempts=1, timeout_s=0.5)

    def test_not_ok_payload_raises(self):
        server = _Server(json.dumps({"ok": False, "error": "x"}).encode())
        self.addCleanup(server.close)
        with self.assertRaisesRegex(CapabilityUnavailable, "返回异常"):
            fetch_snapshot(server.url, attempts=1)

    def test_invalid_registry_content_raises(self):
        bad = _payload({"schema_version": 99})
        server = _Server(json.dumps(bad).encode())
        self.addCleanup(server.close)
        with self.assertRaisesRegex(CapabilityUnavailable, "不合法"):
            fetch_snapshot(server.url, attempts=1)

    def test_bad_json_raises_unavailable(self):
        server = _Server(b"<html>oops</html>")
        self.addCleanup(server.close)
        with self.assertRaises(CapabilityUnavailable):
            fetch_snapshot(server.url, attempts=1)


class ClaimSequenceTests(unittest.TestCase):
    def test_claim_posts_combo_and_name(self):
        server = _Server(json.dumps({
            "ok": True, "claimed_to": [
                {"capability_id": "cap-cw-twist", "label": "扭旋钮·twist",
                 "already_claimed": False},
            ],
        }).encode())
        self.addCleanup(server.close)
        result = claim_sequence(server.url, "right_arm", "qiangnao-1-right",
                                "扭旋钮-起手式")
        self.assertTrue(result["ok"])
        self.assertEqual(result["claimed_to"][0]["capability_id"],
                         "cap-cw-twist")
        self.assertEqual(server.last_post, {
            "arm": "right_arm", "hand_id": "qiangnao-1-right",
            "name": "扭旋钮-起手式",
        })

    def test_rejection_maps_error_message(self):
        server = _Server(json.dumps({
            "ok": False, "error": "手型号「ghost」不存在",
        }).encode(), status=404)
        self.addCleanup(server.close)
        with self.assertRaisesRegex(CapabilityUnavailable, "ghost"):
            claim_sequence(server.url, "right_arm", "ghost", "x")

    def test_unreachable_raises(self):
        with self.assertRaises(CapabilityUnavailable):
            claim_sequence("http://127.0.0.1:1", "right_arm",
                           "qiangnao-1-right", "x", timeout_s=0.5)


class DescribeActiveTests(unittest.TestCase):
    def test_active_combo_with_calibration_status(self):
        line = describe_active(_payload(reg.seed_registry()))
        self.assertIn("右臂", line)
        self.assertIn("因时-右-1", line)
        self.assertIn("ready", line)

    def test_no_active_combo(self):
        registry = reg.seed_registry()
        registry["active"] = None
        line = describe_active(_payload(registry))
        self.assertIn("未设置激活组合", line)


if __name__ == "__main__":
    unittest.main()
