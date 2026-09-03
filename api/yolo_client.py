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
        self._session.trust_env = False   # 本机服务，不走系统代理

    def scene(
        self,
        include_image: bool = False,
        include_wrist: bool = False,
    ) -> dict:
        """{"ok": True, "scene": "远方就地左"|"远方就地右"|None, "conf": ..., "boxes": [...]}

        include_image=True 时返回体多 jpeg_b64（头部判定帧）；
        include_wrist=True 时再附带右腕核验帧，仅供横移拨动前留档。
        """
        try:
            params = {}
            if include_image:
                params["include_image"] = "true"
            if include_wrist:
                params["include_wrist"] = "true"
            r = self._session.get(
                f"{self.base}/api/yolo/scene",
                params=params or None,
                timeout=30.0,
            )
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
