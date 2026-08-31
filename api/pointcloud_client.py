"""7005 语义点云服务的最小 HTTP 客户端。

流程用它走"拍一下 → 算法找点 → 确认下发"三步：冻结同帧 RGB-D、
按墙面坐标系 + 面板中心（粉点）+ 固定模型偏移算出目的点，最后把
（可带人工修正的）相机系目标交给 18001 生成与 /pick 同构的规划目标。
"""

from __future__ import annotations

import requests


class PointcloudClient:
    def __init__(self, base: str = "http://127.0.0.1:7005") -> None:
        self.base = base.rstrip("/")
        self._session = requests.Session()
        self._session.trust_env = False

    def _post(self, path: str, body: dict | None, timeout_s: float) -> dict:
        try:
            r = self._session.post(f"{self.base}{path}", json=body,
                                   timeout=timeout_s)
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            return {"ok": False, "error": f"点云服务不可达: {exc}"}

    def capture(self, **body) -> dict:
        """冻结一帧同步 RGB-D 并跑 YOLO 分割，返回 capture_id 等元数据。"""
        return self._post("/api/pointcloud/capture", body or {}, 60.0)

    def auto_target(self, capture_id: str) -> dict:
        """在冻结帧上建墙面系、拟合面板、按 0.2.0-s 模型推算目的点。"""
        return self._post(f"/api/pointcloud/auto-target/{capture_id}",
                          None, 60.0)

    def save_scene_mismatch(self, capture_id: str, body: dict) -> dict:
        """保存识别类别与任务预期不一致的训练样本。"""
        return self._post(
            f"/api/pointcloud/training-sample/scene-mismatch/{capture_id}",
            body,
            15.0,
        )

    def confirm(self, capture_id: str, body: dict) -> dict:
        """把相机系目标交 18001 确认，返回与 /pick 同构的 p_root/plane。"""
        return self._post(f"/api/pointcloud/confirm/{capture_id}", body, 30.0)

    def alive(self) -> bool:
        try:
            r = self._session.get(f"{self.base}/api/pointcloud/status",
                                  timeout=3.0)
            return bool(r.json().get("ok"))
        except (requests.RequestException, ValueError):
            return False
