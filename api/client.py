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
        self._session.trust_env = False   # 本机服务，不走系统代理

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

    def align_yaw_start(self, dmin: float = 0.4, dmax: float = 1.0,
                        tol_deg: float | None = None, target_deg: float = 0.0,
                        mode: str | None = None) -> dict:
        """闭环转身把 yaw 收进 target_deg±tol_deg。mode="hold" 用新对中（打杆式）。"""
        body: dict = {"start": True, "dmin": dmin, "dmax": dmax,
                      "target_deg": target_deg}
        if tol_deg is not None:
            body["tol_deg"] = tol_deg
        if mode:
            body["mode"] = mode
        return self.post("/align_yaw", body)

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
        """像素取点 → 3D 目标（p_root 等），不含规划。"""
        return self.post("/pick", {"u": u, "v": v, **kwargs}, timeout_s=30.0)

    def plan_axis_last(self, start_joints: dict, target_root: list,
                       **kwargs: Any) -> dict:
        """「平移在先、进出在后」主段规划，返回 waypoints 供 execute。"""
        return self.post("/plan_axis_last",
                         {"start_joints": start_joints,
                          "target_root": target_root, **kwargs},
                         timeout_s=45.0)

    def plan_cartesian(self, start_joints: dict, direction_root: list,
                       distance_m: float, **kwargs: Any) -> dict:
        """指尖沿直线平移的规划（收回/横移用）。"""
        return self.post("/plan_cartesian",
                         {"start_joints": start_joints,
                          "direction_root": direction_root,
                          "distance_m": distance_m, **kwargs},
                         timeout_s=45.0)

    def sequences(self) -> dict:
        return self.get("/sequences")

    def waypoints(self) -> dict:
        return self.get("/waypoints")

    def run_sequence(self, file: str, **kwargs: Any) -> dict:
        """一键执行已保存序列。首次调用只规划并回传 preview，
        再次调用才真机回放（详见服务端 /sequences/run）。"""
        return self.post("/sequences/run", {"file": file, **kwargs},
                         timeout_s=90.0)

    def execute(self, **kwargs: Any) -> dict:
        """执行最近一次预演轨迹（需已接管）。"""
        return self.post("/execute", kwargs, timeout_s=30.0)

    def exec_status(self) -> dict:
        return self.get("/exec_status")

    def joints(self) -> dict:
        return self.get("/joints")

    def motors(self, ids: str | None = None) -> dict:
        """全身电机角度（只读）。缺省 = 左右腿俯仰/偏航 + 腰偏航 5 个。"""
        return self.get("/motors", **({"ids": ids} if ids else {}))

    def stop(self) -> dict:
        """急停。"""
        return self.post("/stop")
