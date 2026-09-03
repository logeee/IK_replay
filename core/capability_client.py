"""启动拜访 18000 能力中心：HTTP 拉取能力注册表快照。

约定（全项目统一）：
· 每个服务（17001 调度 / 18001 reach / 7002 确认台 / 7004 YOLO / 7005 点云
  ……）启动时调用 fetch_snapshot()，拿不到就抛 CapabilityUnavailable，
  由各入口打印后以非零码退出——服务必须在 18000 可达时才启动。
· 自动拉起 18000 是**启动脚本**（prepare.sh / capability.sh 等）的职责；
  服务进程内只确认可达，不做拉起。
· 快照语义：进程生命周期内配置以启动时刻的快照为准；改 18000 配置后
  重启对应服务生效（与既有「重启生效」约定一致），不做热切换。
· 服务不再直接读 config/capability_registry.json——那是 18000 的存储，
  唯一入口是 18000 的 HTTP 接口。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from core.capability_registry import ARM_LABELS, find_hand, validate_registry

DEFAULT_CAPABILITY_URL = "http://127.0.0.1:18000"
REGISTRY_ENDPOINT = "/api/capability/registry"


class CapabilityUnavailable(RuntimeError):
    """18000 不可达 / 返回异常 / 注册表内容不合法。"""


def fetch_snapshot(
    base_url: str | None = None,
    *,
    timeout_s: float = 3.0,
    attempts: int = 3,
) -> dict[str, Any]:
    """GET /api/capability/registry，返回完整 payload（registry 已本地重校验）。

    payload 结构与 18000 返回一致：
        {"ok": true, "registry": {...}, "calibrations": [...], "meta": {...}}
    网络失败重试 attempts 次；内容不合法不重试直接抛。
    """
    base = (base_url or DEFAULT_CAPABILITY_URL).rstrip("/")
    url = base + REGISTRY_ENDPOINT
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(0.5)
        try:
            request = urllib.request.Request(
                url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                payload = json.loads(
                    response.read().decode("utf-8", errors="replace"))
        except (OSError, ValueError) as exc:   # URLError/超时/连接拒绝/坏 JSON
            last_error = exc
            continue
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise CapabilityUnavailable(
                f"18000 返回异常（{url}）: {str(payload)[:200]}")
        try:
            payload["registry"] = validate_registry(payload.get("registry"))
        except ValueError as exc:
            raise CapabilityUnavailable(
                f"18000 注册表内容不合法: {exc}") from exc
        return payload
    raise CapabilityUnavailable(
        f"访问不到 18000 能力中心（{url}）: {last_error}。"
        "请先运行 IK_replay/capability.sh（prepare.sh 等启动脚本会自动拉起）。")


def describe_active(payload: dict[str, Any]) -> str:
    """启动日志统一的一行描述：激活组合 + 该组合标定状态。"""
    registry = payload.get("registry") or {}
    active = registry.get("active")
    if not active:
        return "18000 未设置激活组合（臂+手型号）"
    hand = find_hand(registry, active["hand_id"]) or {}
    status = next(
        (item.get("status") for item in payload.get("calibrations") or []
         if item.get("arm") == active.get("arm")
         and item.get("hand_id") == active.get("hand_id")),
        "missing",
    )
    return (f"激活组合: {ARM_LABELS.get(active.get('arm'), active.get('arm'))}"
            f" + {hand.get('name') or active.get('hand_id')}（标定 {status}）")
