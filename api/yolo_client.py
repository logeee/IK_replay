"""7004 YOLO 推理服务的客户端（流程侧用，fastapi 环境）。

所有方法失败不抛异常，返回 {"ok": False, "error": ...}——
流程拿到失败结果后自行决定转人工确认台还是报错。
"""

from __future__ import annotations

import requests


class YoloClient:
    def __init__(self, base_url: str = "http://127.0.0.1:7004"):
        self.base = base_url.rstrip("/")
        self._session = requests.Session()

    def scene(self) -> dict:
        """{"ok": True, "scene": "就地"|"远方"|None, "conf": ..., "boxes": [...]}"""
        try:
            r = self._session.get(f"{self.base}/api/yolo/scene", timeout=30.0)
            return r.json()
        except requests.RequestException as exc:
            return {"ok": False, "error": f"YOLO 服务不可达: {exc}"}

    def infer(self) -> dict:
        try:
            r = self._session.post(f"{self.base}/api/yolo/infer", json={},
                                   timeout=30.0)
            return r.json()
        except requests.RequestException as exc:
            return {"ok": False, "error": f"YOLO 服务不可达: {exc}"}

    def alive(self) -> bool:
        try:
            r = self._session.get(f"{self.base}/api/yolo/status", timeout=3.0)
            return bool(r.json().get("ok"))
        except requests.RequestException:
            return False
