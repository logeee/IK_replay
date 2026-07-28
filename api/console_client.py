"""7002 人工确认台的阻塞式客户端（流程侧用）。

flow 里对未部署步骤调用 ask()，函数会一直等到网页上有人回答才返回。
网页点了「中止流程」→ 抛 ConsoleAbort，flow 捕获后按 ABORTED 结束。
"""

from __future__ import annotations

import time
from typing import Any

import requests


class ConsoleAbort(Exception):
    """操作员在确认台上点了「中止流程」。"""


class ConsoleClient:
    def __init__(self, base_url: str = "http://127.0.0.1:7002",
                 answer_timeout_s: float | None = None):
        """answer_timeout_s=None 表示无限等人回答（现场操作节奏不可预估）。"""
        self.base = base_url.rstrip("/")
        self.answer_timeout_s = answer_timeout_s
        self._session = requests.Session()
        self._session.trust_env = False   # 本机服务，不走系统代理

    def ask(self, kind: str, prompt: str,
            options: list[str] | None = None) -> Any:
        r = self._session.post(f"{self.base}/api/console/ask",
                               json={"kind": kind, "prompt": prompt,
                                     "options": options or []},
                               timeout=10.0)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"确认台拒绝提问: {data.get('error')}")
        qid = data["id"]
        deadline = (time.monotonic() + self.answer_timeout_s
                    if self.answer_timeout_s else None)
        try:
            while True:
                if deadline and time.monotonic() > deadline:
                    raise TimeoutError(f"确认台 {self.answer_timeout_s}s 无人回答")
                r = self._session.get(f"{self.base}/api/console/wait",
                                      params={"id": qid, "timeout_s": 25},
                                      timeout=30.0)
                res = r.json()
                if res.get("done"):
                    value = res.get("value")
                    if isinstance(value, dict) and value.get("__abort__"):
                        raise ConsoleAbort(prompt)
                    return value
                if res.get("gone"):
                    raise RuntimeError("问题被确认台撤销")
        except (TimeoutError, RuntimeError):
            self._session.post(f"{self.base}/api/console/cancel",
                               json={"id": qid}, timeout=5.0)
            raise

    # ---- 便捷封装 ----

    def yesno(self, prompt: str) -> bool:
        return bool(self.ask("yesno", prompt))

    def choice(self, prompt: str, options: list[str]) -> str:
        return str(self.ask("choice", prompt, options))

    def points(self, prompt: str) -> list[dict]:
        pts = self.ask("points", prompt)
        return [{"u": int(p["u"]), "v": int(p["v"])} for p in pts]

    def confirm(self, prompt: str) -> None:
        self.ask("confirm", prompt)

    def alive(self) -> bool:
        try:
            self._session.get(f"{self.base}/api/console/pending", timeout=3.0)
            return True
        except requests.RequestException:
            return False
