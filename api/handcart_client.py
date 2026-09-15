"""8876 手车电机服务的薄 HTTP 客户端。"""

from __future__ import annotations

from typing import Any

import requests


class HandcartClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8876",
                 timeout_s: float = 10.0):
        self.base = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._session = requests.Session()
        self._session.trust_env = False

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise RuntimeError(
                f"8876 返回了非 JSON 响应: {response.text[:200]}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"8876 返回格式错误: {data!r}")
        if not response.ok:
            raise RuntimeError(
                str(data.get("error") or data.get("message") or data)
            )
        return data

    def start(self, motor_action: str) -> dict[str, Any]:
        response = self._session.post(
            f"{self.base}/v1/handcart/jobs",
            json={"process_restart": False, "motor_action": motor_action},
            timeout=self.timeout_s,
        )
        return self._json(response)

    def status(self, job_id: str) -> dict[str, Any]:
        response = self._session.get(
            f"{self.base}/v1/handcart/jobs/{job_id}",
            timeout=self.timeout_s,
        )
        return self._json(response)

    def terminate(self, job_id: str) -> dict[str, Any]:
        response = self._session.post(
            f"{self.base}/v1/handcart/jobs/{job_id}/terminate",
            json={},
            timeout=self.timeout_s,
        )
        return self._json(response)
