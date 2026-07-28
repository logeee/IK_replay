"""reach_server 的薄 HTTP 客户端。

只做转发和最基本的错误归一化，不含任何流程逻辑；
流程编排见 flow.py。服务端各接口的语义以 adapters/reach.py 为准。
"""

from __future__ import annotations

from typing import Any

import requests


class ReachClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8001", timeout_s: float = 10.0):
        self.base = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._session = requests.Session()

    # ---- 通用 ----

    def get(self, path: str, **params: Any) -> dict:
        r = self._session.get(f"{self.base}/api/reach{path}",
                              params=params or None, timeout=self.timeout_s)
        return self._unwrap(r)

    def post(self, path: str, body: dict | None = None,
             timeout_s: float | None = None) -> dict:
        r = self._session.post(f"{self.base}/api/reach{path}",
                               json=body or {}, timeout=timeout_s or self.timeout_s)
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r: requests.Response) -> dict:
        try:
            data = r.json()
        except ValueError:
            r.raise_for_status()
            raise RuntimeError(f"非 JSON 响应: {r.text[:200]}")
        # 服务端约定：业务失败也带 ok=False 返回，HTTP 码只是辅助
        if not isinstance(data, dict):
            raise RuntimeError(f"意外响应: {data!r}")
        return data

    # ---- 流程用到的具体接口 ----

    def status(self) -> dict:
        return self.get("/status")

    def perpendicular(self, dmin: float = 0.4, dmax: float = 1.0) -> dict:
        """柜面平面拟合：yaw_err_deg（平面指数）、distance_m、align 状态等。"""
        return self.get("/perpendicular", dmin=dmin, dmax=dmax)

    def align_yaw_start(self, dmin: float = 0.4, dmax: float = 1.0) -> dict:
        return self.post("/align_yaw", {"start": True, "dmin": dmin, "dmax": dmax})

    def align_yaw_stop(self) -> dict:
        return self.post("/align_yaw", {"stop": True})

    def turn_jog(self, delta_deg: float) -> dict:
        """原地转身点动（±10° 内定长脉冲）。"""
        return self.post("/turn", {"delta_deg": delta_deg})

    def turn_stop(self) -> dict:
        return self.post("/turn", {"stop": True})

    def arm(self) -> dict:
        """接管手臂（真机执行的前提）。"""
        return self.post("/arm")

    def disarm(self) -> dict:
        return self.post("/disarm")

    def pick(self, u: int, v: int, **kwargs: Any) -> dict:
        """像素取点 → 3D 目标 → IK 预演。"""
        return self.post("/pick", {"u": u, "v": v, **kwargs}, timeout_s=30.0)

    def execute(self, **kwargs: Any) -> dict:
        """执行最近一次预演轨迹（需已接管）。"""
        return self.post("/execute", kwargs, timeout_s=30.0)

    def exec_status(self) -> dict:
        return self.get("/exec_status")

    def joints(self) -> dict:
        return self.get("/joints")

    def stop(self) -> dict:
        """急停。"""
        return self.post("/stop")
