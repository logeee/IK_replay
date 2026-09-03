"""18000 hand selection + 7015 mount + 18089 state bridge for reach."""
from __future__ import annotations

import json
import math
import ssl
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


FetchJson = Callable[[str, float, bool], Any]
PostJson = Callable[[str, dict, float, bool], Any]


_BRAINCO_PREVIEW = {
    "model_root": "/assets/brainco_hand",
    "urdf": "brainco_{side}.urdf",
    "side_prefix": {"left": "left", "right": "right"},
    "material_mode": "brainco",
    "joints": [
        {"index": 0, "targets": [
            {"suffix": "thumb_proximal_joint", "lower": 0.0, "upper": 1.0472},
        ]},
        {"index": 1, "targets": [
            {"suffix": "thumb_metacarpal_joint", "lower": 0.0, "upper": 1.5184},
        ]},
        {"index": 2, "targets": [
            {"suffix": "index_proximal_joint", "lower": 0.0, "upper": 1.4661},
        ]},
        {"index": 3, "targets": [
            {"suffix": "middle_proximal_joint", "lower": 0.0, "upper": 1.4661},
        ]},
        {"index": 4, "targets": [
            {"suffix": "ring_proximal_joint", "lower": 0.0, "upper": 1.4661},
        ]},
        {"index": 5, "targets": [
            {"suffix": "pinky_proximal_joint", "lower": 0.0, "upper": 1.4661},
        ]},
    ],
}

_INSPIRE_PREVIEW = {
    "model_root": "/assets/inspire_hand",
    "urdf": "inspire_hand_{side}.urdf",
    "side_prefix": {"left": "L", "right": "R"},
    "material_mode": "inspire",
    "joints": [
        {"index": 0, "targets": [
            {"suffix": "thumb_proximal_pitch_joint", "lower": 0.0, "upper": 0.5},
            {"suffix": "thumb_intermediate_joint", "lower": 0.0, "upper": 0.8},
            {"suffix": "thumb_distal_joint", "lower": 0.0, "upper": 1.2},
        ]},
        {"index": 1, "targets": [
            {"suffix": "thumb_proximal_yaw_joint", "lower": -0.1, "upper": 1.3},
        ]},
        {"index": 2, "targets": [
            {"suffix": "index_proximal_joint", "lower": 0.0, "upper": 1.7},
            {"suffix": "index_intermediate_joint", "lower": 0.0, "upper": 1.7},
        ]},
        {"index": 3, "targets": [
            {"suffix": "middle_proximal_joint", "lower": 0.0, "upper": 1.7},
            {"suffix": "middle_intermediate_joint", "lower": 0.0, "upper": 1.7},
        ]},
        {"index": 4, "targets": [
            {"suffix": "ring_proximal_joint", "lower": 0.0, "upper": 1.7},
            {"suffix": "ring_intermediate_joint", "lower": 0.0, "upper": 1.7},
        ]},
        {"index": 5, "targets": [
            {"suffix": "pinky_proximal_joint", "lower": 0.0, "upper": 1.7},
            {"suffix": "pinky_intermediate_joint", "lower": 0.0, "upper": 1.7},
        ]},
    ],
}

_DEFAULT_PREVIEWS = {
    "brainco_revo2": _BRAINCO_PREVIEW,
    "inspire_dfx": _INSPIRE_PREVIEW,
    "inspire_ftp": _INSPIRE_PREVIEW,
}


@dataclass(frozen=True)
class HandRuntimeConfig:
    arm: str
    hand_id: str
    hand_name: str
    side: str
    device_id: str
    tcp_point_id: str
    wrist_link: str
    T_wrist2hand: list[list[float]]
    p_tool_wrist_m: list[float] | None
    service_url: str
    assets_root: Path


def _default_fetch_json(url: str, timeout: float, verify_tls: bool) -> Any:
    request = Request(url, headers={"Accept": "application/json"})
    context = None
    if url.lower().startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()
    with urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def _default_post_json(url: str, payload: dict, timeout: float,
                       verify_tls: bool) -> Any:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    context = None
    if url.lower().startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()
    with urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def _matrix4(value: Any, field: str) -> list[list[float]]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(row, list) or len(row) != 4 for row in value)
    ):
        raise ValueError(f"{field} 必须是 4x4 数组")
    result = [[float(item) for item in row] for row in value]
    if not all(math.isfinite(item) for row in result for item in row):
        raise ValueError(f"{field} 包含非有限数值")
    if any(abs(result[3][idx] - expected) > 1e-8
           for idx, expected in enumerate((0.0, 0.0, 0.0, 1.0))):
        raise ValueError(f"{field} 最后一行必须是 [0, 0, 0, 1]")
    return result


def _find_hand(registry: dict[str, Any], hand_id: str) -> dict[str, Any]:
    for hand in registry.get("hands", []):
        if hand.get("id") == hand_id:
            return hand
    raise ValueError(f"18000 激活的手型号 {hand_id!r} 不存在")


def _selected_tcp(calibration: dict[str, Any], point_id: str) -> list[float] | None:
    if point_id:
        for point in calibration.get("tcp_points_wrist_m", []):
            if point.get("id") == point_id:
                xyz = point.get("p_wrist_m")
                if isinstance(xyz, list) and len(xyz) == 3:
                    result = [float(value) for value in xyz]
                    if all(math.isfinite(value) for value in result):
                        return result
        raise ValueError(f"标定文件不含 18000 配置的 TCP 特征点 {point_id!r}")
    xyz = calibration.get("p_tool_wrist_m")
    if isinstance(xyz, list) and len(xyz) == 3:
        return [float(value) for value in xyz]
    return None


def _valid_preview(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not str(value.get("model_root") or "") or not str(value.get("urdf") or ""):
        return False
    joints = value.get("joints")
    if not isinstance(joints, list) or not joints:
        return False
    try:
        for mapping in joints:
            if int(mapping["index"]) < 0 or not isinstance(mapping["targets"], list):
                return False
            for target in mapping["targets"]:
                if not str(target["suffix"]):
                    return False
                if not all(math.isfinite(float(target[key]))
                           for key in ("lower", "upper")):
                    return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def build_hand_runtime_config(
    *,
    registry: dict[str, Any],
    calibration: dict[str, Any],
    chain_id: str,
    expected_wrist_link: str,
    service_url: str,
    assets_root: Path,
) -> HandRuntimeConfig | None:
    """Build a runtime only when the active hand is explicitly bound to 18089."""
    active = registry.get("active")
    if not isinstance(active, dict):
        return None
    active_arm = str(active.get("arm") or "")
    hand_id = str(active.get("hand_id") or "")
    if active_arm != chain_id:
        raise ValueError(
            f"18000 激活臂 {active_arm!r} 与 18001 启动链 {chain_id!r} 不一致")
    hand = _find_hand(registry, hand_id)
    side = str(hand.get("design_side") or "")
    expected_side = "right" if chain_id.startswith("right_") else "left"
    if side != expected_side:
        raise ValueError(
            f"18000 手型号设计侧 {side!r} 与激活臂 {chain_id!r} 不一致")

    calib_arm = str(calibration.get("arm") or "")
    calib_hand = str(calibration.get("hand_id") or "")
    wrist_link = str(
        calibration.get("wrist_link") or calibration.get("tip_link") or "")
    if calib_arm != active_arm:
        raise ValueError(
            f"标定 arm={calib_arm!r} 与 18000 active.arm={active_arm!r} 不一致")
    if calib_hand != hand_id:
        raise ValueError(
            f"标定 hand_id={calib_hand!r} 与 18000 active.hand_id={hand_id!r} 不一致")
    if wrist_link != expected_wrist_link:
        raise ValueError(
            f"标定 wrist_link={wrist_link!r} 与机器人末端 "
            f"{expected_wrist_link!r} 不一致")

    device_id = str(hand.get("hand_web_device_id") or "").strip()
    if not device_id:
        return None
    if device_id not in _DEFAULT_PREVIEWS:
        raise ValueError(f"18000 配置了不支持的 18089 设备 {device_id!r}")
    expected_hand_base = (
        "base_link"
        if device_id == "brainco_revo2"
        else f"{'R' if side == 'right' else 'L'}_hand_base_link"
    )
    calibrated_hand_base = str(calibration.get("hand_base_link") or "")
    if calibrated_hand_base != expected_hand_base:
        raise ValueError(
            f"标定 hand_base_link={calibrated_hand_base!r} 与 "
            f"{device_id} {side} 模型根 {expected_hand_base!r} 不一致")
    tcp_point_id = str(hand.get("tcp_point_id") or "").strip()
    return HandRuntimeConfig(
        arm=active_arm,
        hand_id=hand_id,
        hand_name=str(hand.get("name") or hand_id),
        side=side,
        device_id=device_id,
        tcp_point_id=tcp_point_id,
        wrist_link=wrist_link,
        T_wrist2hand=_matrix4(
            calibration.get("T_wrist2hand"), "T_wrist2hand"),
        p_tool_wrist_m=_selected_tcp(calibration, tcp_point_id),
        service_url=service_url.rstrip("/") + "/",
        assets_root=Path(assets_root).expanduser().resolve(),
    )


class HandRuntime:
    def __init__(
        self,
        config: HandRuntimeConfig,
        *,
        fetch_json: FetchJson | None = None,
        post_json: PostJson | None = None,
        timeout_s: float = 0.8,
        connect_timeout_s: float = 5.0,
        verify_tls: bool = False,
    ) -> None:
        self.config = config
        self.fetch_json = fetch_json or _default_fetch_json
        self.post_json = post_json or _default_post_json
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.verify_tls = verify_tls
        self._catalog: Any = None
        self._catalog_at = 0.0
        self._catalog_lock = threading.Lock()

    def _fetch(self, path: str) -> Any:
        return self.fetch_json(
            urljoin(self.config.service_url, path.lstrip("/")),
            self.timeout_s,
            self.verify_tls,
        )

    def _catalog_device(self) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._catalog_lock:
            if self._catalog is None or now - self._catalog_at > 60.0:
                self._catalog_at = now
                try:
                    self._catalog = self._fetch("/api/devices")
                except Exception:
                    self._catalog = {}
                    raise
            body = self._catalog
        devices = body.get("devices", []) if isinstance(body, dict) else body
        if not isinstance(devices, list):
            return None
        return next(
            (item for item in devices
             if isinstance(item, dict) and item.get("id") == self.config.device_id),
            None,
        )

    def _model(self, catalog_device: dict[str, Any] | None) -> dict[str, Any]:
        preview = deepcopy(_DEFAULT_PREVIEWS[self.config.device_id])
        live_preview = (catalog_device or {}).get("preview")
        if _valid_preview(live_preview):
            preview = deepcopy(live_preview)
        asset_dir = Path(str(preview.get("model_root") or "")).name
        urdf_template = Path(str(preview.get("urdf") or "")).name
        if not asset_dir or not urdf_template:
            raise ValueError("18089 设备目录缺少 preview.model_root/urdf")
        urdf = urdf_template.format(side=self.config.side)
        base_url = f"/api/reach/hand/assets/{asset_dir}/"
        preview["model_root"] = base_url.rstrip("/")
        preview["urdf"] = urdf
        return {
            "key": f"{self.config.device_id}:{self.config.side}:{urdf}",
            "device_id": self.config.device_id,
            "urdf_url": f"{base_url}{urdf}",
            "mesh_base_url": base_url,
            "preview": preview,
        }

    def snapshot(self) -> dict[str, Any]:
        catalog_device = None
        catalog_error = None
        try:
            catalog_device = self._catalog_device()
        except Exception as exc:  # 18089 unavailable: local fallback model remains usable.
            catalog_error = str(exc) or type(exc).__name__
        model = self._model(catalog_device)

        status = None
        service_error = None
        try:
            status = self._fetch("/api/status")
        except Exception as exc:
            service_error = str(exc) or type(exc).__name__
        status = status if isinstance(status, dict) else {}
        actual_device = str(status.get("device_id") or "")
        available = service_error is None
        compatible = available and (
            not actual_device or actual_device == self.config.device_id)
        connected = (
            compatible
            and actual_device == self.config.device_id
            and bool(status.get("connected"))
        )
        positions = None
        if connected:
            hands = status.get("hands")
            hand_state = (
                hands.get(self.config.side) or {}
                if isinstance(hands, dict)
                else {}
            )
            raw_positions = (
                hand_state.get("positions")
                if isinstance(hand_state, dict)
                else None
            )
            if isinstance(raw_positions, list):
                try:
                    values = [float(value) for value in raw_positions]
                    required = 1 + max(
                        int(item.get("index", -1))
                        for item in model["preview"].get("joints", [])
                    )
                    if (
                        len(values) >= required
                        and all(math.isfinite(value) and 0.0 <= value <= 1.0
                                for value in values)
                    ):
                        positions = values
                except (TypeError, ValueError):
                    pass

        error = None
        if service_error:
            error = f"18089 不可用: {service_error}"
        elif not compatible:
            error = (
                f"18000 期望设备 {self.config.device_id}，"
                f"18089 当前设备 {actual_device or '未设置'}")
        elif not connected:
            error = "18089 灵巧手未连接"
        elif positions is None:
            error = f"18089 没有 {self.config.side} 手的有效关节数据"
        elif catalog_error:
            error = f"18089 设备目录不可用，使用内置映射: {catalog_error}"

        return {
            "enabled": True,
            "arm": self.config.arm,
            "hand_id": self.config.hand_id,
            "hand_name": self.config.hand_name,
            "side": self.config.side,
            "wrist_link": self.config.wrist_link,
            "T_wrist2hand": self.config.T_wrist2hand,
            "tcp_point_id": self.config.tcp_point_id,
            "p_tool_wrist_m": self.config.p_tool_wrist_m,
            "model": model,
            "service": {
                "url": self.config.service_url.rstrip("/"),
                "available": available,
                "connected": connected,
                "compatible": compatible,
                "expected_device_id": self.config.device_id,
                "actual_device_id": actual_device or None,
                "transport": status.get("transport"),
                "error": error,
            },
            "positions": positions,
        }

    def connect(self) -> dict[str, Any]:
        """让 18089 连接激活组合绑定的设备（已连同设备时 18089 复用通道）。

        接管手臂时顺带调用；设备被其他控制源占用时 18089 返回 409，
        这里只透传错误、绝不抢占。
        """
        base = {
            "device_id": self.config.device_id,
            "side": self.config.side,
            "hand_name": self.config.hand_name,
        }
        url = urljoin(self.config.service_url, "api/connect")
        try:
            body = self.post_json(
                url, {"device_id": self.config.device_id},
                self.connect_timeout_s, self.verify_tls)
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8", errors="replace"))
                message = str(detail.get("error") or f"HTTP {exc.code}")
            except (ValueError, OSError):
                message = f"HTTP {exc.code}"
            return {**base, "ok": False, "error": f"18089: {message}"}
        except OSError as exc:
            return {**base, "ok": False, "error": f"18089 不可达: {exc}"}
        except ValueError as exc:
            return {**base, "ok": False, "error": f"18089 返回非 JSON: {exc}"}
        if not isinstance(body, dict) or body.get("ok") is False:
            error = (body or {}).get("error") if isinstance(body, dict) else None
            return {**base, "ok": False,
                    "error": str(error or "18089 拒绝连接")}
        return {**base, "ok": True}

    def asset_path(self, relative_path: str) -> Path:
        root = self.config.assets_root
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError("hand asset path escapes root") from exc
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        return path


_runtime: HandRuntime | None = None
_runtime_lock = threading.Lock()


def configure_hand_runtime(runtime: HandRuntime | None) -> None:
    global _runtime
    with _runtime_lock:
        _runtime = runtime


def hand_snapshot() -> dict[str, Any]:
    with _runtime_lock:
        runtime = _runtime
    if runtime is None:
        return {"enabled": False}
    return runtime.snapshot()


def hand_asset_path(relative_path: str) -> Path:
    with _runtime_lock:
        runtime = _runtime
    if runtime is None:
        raise FileNotFoundError("hand runtime is not configured")
    return runtime.asset_path(relative_path)


def hand_connect() -> dict[str, Any]:
    """按 18000 激活组合连接 18089 灵巧手（接管手臂时顺带调用）。

    当前手型号没绑 18089 设备时返回 enabled=False（不算错误）。
    """
    with _runtime_lock:
        runtime = _runtime
    if runtime is None:
        return {"ok": False, "enabled": False,
                "error": "当前组合未绑定 18089 灵巧手"}
    return {"enabled": True, **runtime.connect()}
