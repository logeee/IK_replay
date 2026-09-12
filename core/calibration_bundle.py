"""Load a calib-manifest/2 binding and compose the legacy runtime view.

The workstation stores camera extrinsic, hand mount and TCP profile as separate
artifacts.  Runtime code still consumes one dictionary, so this module verifies
the bound packages and composes that dictionary in memory without recreating a
second authoritative calibration file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.capability_registry import calibration_binding

REQUIRED_RUNTIME_TYPES = ("extrinsic", "hand_mount", "tcp_profile")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取{label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是 JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_artifact(
    descriptor: dict[str, Any], robot: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    root = Path(str(descriptor.get("local_path") or "")).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError(
            f"产物 {descriptor.get('artifact_id')} 的 local_path 不可用: {root}")
    root = root.resolve()
    manifest = _read_json(root / "manifest.json", "manifest")
    for field in (
        "artifact_id", "unit_code", "vendor", "robot_model", "type", "subject_key",
        "run_id", "status"
    ):
        if str(manifest.get(field) or "") != str(descriptor.get(field) or ""):
            raise ValueError(
                f"产物 {descriptor.get('artifact_id')} 的 manifest.{field} 与 18000 登记不一致")
    if manifest.get("unit_code") != robot["unit_code"]:
        raise ValueError(
            f"产物 {descriptor.get('artifact_id')} 属于 {manifest.get('unit_code')}，"
            f"当前机器人是 {robot['unit_code']}")
    if str(manifest.get("robot_model") or "").lower() != robot["model"]:
        raise ValueError(
            f"产物 {descriptor.get('artifact_id')} 型号与当前机器人不一致")
    if str(manifest.get("vendor") or "").lower() != robot["vendor"].lower():
        raise ValueError(
            f"产物 {descriptor.get('artifact_id')} 厂家与当前机器人不一致")
    subject = manifest.get("subject") or {}
    if subject.get("unit_code") != robot["unit_code"]:
        raise ValueError(
            f"产物 {descriptor.get('artifact_id')} 的 subject.unit_code 与当前机器人不一致")
    primary_name = str(manifest.get("primary_file") or "").strip()
    if not primary_name:
        raise ValueError(f"产物 {descriptor.get('artifact_id')} 缺少 primary_file")
    primary = (root / primary_name).resolve()
    if primary.parent != root or not primary.is_file():
        raise ValueError(f"产物主文件越界或不存在: {primary_name}")
    entry = next((item for item in manifest.get("files") or []
                  if item.get("name") == primary_name), None)
    if not isinstance(entry, dict):
        raise ValueError(f"manifest.files 未登记主文件 {primary_name}")
    expected_hash = str(entry.get("sha256") or "")
    if expected_hash and _sha256(primary) != expected_hash:
        raise ValueError(f"产物主文件校验失败: {primary}")
    return manifest, _read_json(primary, f"{manifest.get('type')} 主文件"), primary


def compose_bound_calibration(
    registry: dict[str, Any],
    arm: str,
    hand_id: str,
    camera_role: str = "head",
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return ``(runtime_calibration, provenance)`` or ``None`` when unbound."""
    binding = calibration_binding(registry, arm, hand_id, camera_role)
    if binding is None:
        return None
    robot = registry.get("robot")
    if not isinstance(robot, dict):
        raise ValueError("18000 独立标定绑定缺少整机身份，请先由 calib_workstation 登记")
    missing = [kind for kind in REQUIRED_RUNTIME_TYPES
               if kind not in binding.get("resolved", {})]
    if missing:
        raise ValueError(f"18000 标定绑定不完整，缺少: {', '.join(missing)}")

    loaded: dict[str, tuple[dict[str, Any], dict[str, Any], Path]] = {}
    for kind, descriptor in binding["resolved"].items():
        loaded[kind] = _load_artifact(descriptor, robot)

    extrinsic_manifest, extrinsic, _ = loaded["extrinsic"]
    mount_manifest, mount, _ = loaded["hand_mount"]
    tcp_manifest, tcp, _ = loaded["tcp_profile"]
    for kind in REQUIRED_RUNTIME_TYPES:
        if loaded[kind][0].get("status") != "active":
            raise ValueError(f"绑定的 {kind} 产物不是 active 状态")
    mount_inputs = {
        str(item.get("artifact_id") or "")
        for item in mount_manifest.get("dependencies") or []
        if item.get("relation") == "solved_with"
    }
    if extrinsic_manifest["artifact_id"] not in mount_inputs:
        raise ValueError("hand_mount 不是使用当前绑定 extrinsic 解算的产物")
    tcp_inputs = {
        str(item.get("artifact_id") or "")
        for item in tcp_manifest.get("dependencies") or []
        if item.get("relation") == "derived_from"
    }
    if mount_manifest["artifact_id"] not in tcp_inputs:
        raise ValueError("tcp_profile 不是从当前绑定 hand_mount 派生的产物")
    if extrinsic.get("T_cam2base") is None:
        raise ValueError("extrinsic 主文件缺少 T_cam2base")
    if mount.get("T_wrist2hand") is None:
        raise ValueError("hand_mount 主文件缺少 T_wrist2hand")
    tcp_points = tcp.get("tcp_points_wrist_m")
    if not isinstance(tcp_points, list) or not tcp_points:
        raise ValueError("tcp_profile 主文件缺少 tcp_points_wrist_m")
    default_tcp_id = str(tcp.get("default_tcp_point_id") or "")
    selected_tcp = next(
        (point for point in tcp_points
         if not default_tcp_id or str(point.get("id") or "") == default_tcp_id),
        None,
    )
    if selected_tcp is None:
        raise ValueError(f"tcp_profile 找不到默认 TCP 点 {default_tcp_id!r}")
    xyz = selected_tcp.get("p_wrist_m")
    if not isinstance(xyz, list) or len(xyz) != 3:
        raise ValueError("默认 TCP 点缺少三维 p_wrist_m")

    camera_subject = extrinsic_manifest.get("subject") or {}
    camera_compat = extrinsic_manifest.get("compatibility") or {}
    camera = {
        "serial": camera_subject.get("camera_serial") or extrinsic_manifest.get("camera_serial"),
        "width": camera_compat.get("width"),
        "height": camera_compat.get("height"),
        "camera_role": camera_role,
    }
    runtime = {
        **extrinsic,
        "arm": arm,
        "hand_id": hand_id,
        "T_wrist2hand": mount["T_wrist2hand"],
        "wrist_link": mount.get("wrist_link") or extrinsic.get("tip_link"),
        "hand_base_link": mount.get("hand_base_link"),
        "tcp_points_wrist_m": tcp_points,
        "p_tool_wrist_m": [float(value) for value in xyz],
        "p_tool_reference": selected_tcp.get("id"),
        "camera": camera,
        "mount_calib_camera": camera,
        "residual_mm": mount.get("residual_mm") or {},
        "solved_at": mount.get("solved_at"),
        "num_samples": mount.get("num_samples"),
    }
    provenance = {
        "mode": "manifest_binding",
        "unit_code": robot["unit_code"],
        "robot_model": robot["model"],
        "arm": arm,
        "hand_id": hand_id,
        "camera_role": camera_role,
        "artifact_ids": dict(binding.get("artifacts") or {}),
        "paths": {kind: str(values[2]) for kind, values in loaded.items()},
        "mount_artifact_id": mount_manifest.get("artifact_id"),
        "tcp_artifact_id": tcp_manifest.get("artifact_id"),
    }
    runtime["calibration_bundle"] = provenance
    return runtime, provenance
