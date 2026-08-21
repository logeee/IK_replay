"""Gravity-calibration waypoint library and experiment runner (port 18002).

This service never opens DDS and never controls the robot directly.  It stores
its own waypoints/runs and delegates planning and execution to reach_server on
18001, preserving one hardware-control owner.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.gravity_profiles import (
    DEFAULT_GRAVITY_PROFILES_PATH,
    VERSION_PATTERN,
    activate_profile,
    create_profile,
    load_registry,
)


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_ROOT = ROOT / "data" / "gravity_calibration"
WAYPOINTS_DIR = DATA_ROOT / "waypoints"
RUNS_DIR = DATA_ROOT / "runs"
BATCHES_DIR = DATA_ROOT / "batches"
IK_VALIDATIONS_DIR = DATA_ROOT / "ik_validation"
REGULAR_WAYPOINTS_DIR = ROOT / "data" / "waypoints"
SEQUENCES_DIR = ROOT / "data" / "sequences"
GRAVITY_PROFILES_PATH = DEFAULT_GRAVITY_PROFILES_PATH
IK_START_MATCH_MAX_ERROR_RAD = 0.15

app = FastAPI(title="gravity-calibration")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="gravity-web")
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="gravity-assets")

_http = requests.Session()
_http.trust_env = False
_reach_base = "http://127.0.0.1:18001"
_lock = threading.RLock()
_plan: dict[str, Any] | None = None
_batch: dict[str, Any] | None = None
_run_cancel = threading.Event()
_operation: dict[str, Any] = {
    "phase": "idle",
    "message": "等待选择位点",
    "point_id": None,
    "plan_id": None,
    "run_id": None,
    "progress": 0.0,
    "error": None,
}


class GravityServiceError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_id(value: str) -> str:
    point_id = str(value or "")
    if not point_id or any(ch not in "0123456789abcdef" for ch in point_id):
        raise GravityServiceError("位点编号非法")
    return point_id


def _point_path(point_id: str) -> Path:
    return WAYPOINTS_DIR / f"{_safe_id(point_id)}.json"


def _load_point(point_id: str) -> dict[str, Any]:
    path = _point_path(point_id)
    if not path.is_file():
        raise GravityServiceError("位点不存在")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GravityServiceError(f"位点文件损坏: {exc}") from exc
    if not isinstance(value, dict):
        raise GravityServiceError("位点文件格式错误")
    return value


def _list_points() -> list[dict[str, Any]]:
    WAYPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    points: list[dict[str, Any]] = []
    for path in WAYPOINTS_DIR.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                points.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(
        points,
        key=lambda item: (
            int(item.get("order", 1_000_000)),
            str(item.get("created_at", "")),
        ),
    )


def _regular_waypoint_path(filename: str) -> Path:
    name = str(filename or "")
    if (
        not name.endswith(".json")
        or "/" in name
        or "\\" in name
        or ".." in name
        or Path(name).name != name
    ):
        raise GravityServiceError("原位点文件名非法")
    return REGULAR_WAYPOINTS_DIR / name


def _load_regular_waypoint(filename: str) -> dict[str, Any]:
    path = _regular_waypoint_path(filename)
    if not path.is_file():
        raise GravityServiceError(f"原位点不存在: {filename}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GravityServiceError(f"原位点文件损坏 {filename}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GravityServiceError(f"原位点格式错误: {filename}")
    named = payload.get("named_joints")
    if not isinstance(named, dict) or not named:
        raise GravityServiceError(f"原位点缺少关节角: {filename}")
    try:
        joints = {str(name): float(value) for name, value in named.items()}
    except (TypeError, ValueError) as exc:
        raise GravityServiceError(f"原位点关节角非法: {filename}") from exc
    if not all(math.isfinite(value) for value in joints.values()):
        raise GravityServiceError(f"原位点包含非有限关节角: {filename}")
    return {
        "file": path.name,
        "name": str(payload.get("name") or path.stem),
        "chain_id": str(payload.get("chain_id") or "right_arm"),
        "robot": str(payload.get("robot") or "h2"),
        "created_at": payload.get("created_at"),
        "named_joints": joints,
        "joint_names": list(joints),
    }


def _list_regular_waypoints() -> list[dict[str, Any]]:
    if not REGULAR_WAYPOINTS_DIR.is_dir():
        return []
    imported = {
        str(point.get("source_waypoint_file"))
        for point in _list_points()
        if point.get("source_waypoint_file")
    }
    items: list[dict[str, Any]] = []
    for path in sorted(
        REGULAR_WAYPOINTS_DIR.glob("*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        try:
            waypoint = _load_regular_waypoint(path.name)
        except GravityServiceError:
            continue
        waypoint["already_imported"] = path.name in imported
        items.append(waypoint)
    return items


def _sequence_path(filename: str) -> Path:
    name = str(filename or "")
    if (
        not name.endswith(".json")
        or "/" in name
        or "\\" in name
        or ".." in name
        or Path(name).name != name
    ):
        raise GravityServiceError("轨迹文件名非法")
    return SEQUENCES_DIR / name


def _sequence_recorded_progress(
    trajectory: dict[str, Any],
    frame_count: int,
    key: str,
) -> list[float]:
    """Use recorded normalized timing when present; otherwise use frame time."""
    fallback = np.linspace(0.0, 1.0, frame_count).tolist()
    raw = trajectory.get(key)
    if not isinstance(raw, list) or len(raw) != frame_count:
        return fallback
    try:
        progress = [float(value) for value in raw]
    except (TypeError, ValueError):
        return fallback
    if (
        not all(math.isfinite(value) for value in progress)
        or any(a > b for a, b in zip(progress, progress[1:]))
        or abs(progress[0]) > 1e-6
        or abs(progress[-1] - 1.0) > 1e-6
    ):
        return fallback
    return progress


def _load_sequence_preview(
    filename: str, *, include_tool_visualization: bool = True
) -> dict[str, Any]:
    path = _sequence_path(filename)
    if not path.is_file():
        raise GravityServiceError(f"轨迹不存在: {filename}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GravityServiceError(f"轨迹文件损坏 {filename}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GravityServiceError(f"轨迹格式错误: {filename}")
    trajectory = payload.get("trajectory")
    if not isinstance(trajectory, dict):
        raise GravityServiceError(f"轨迹缺少 trajectory: {filename}")
    raw_names = trajectory.get("joint_names")
    raw_frames = trajectory.get("frames")
    if not isinstance(raw_names, list) or not raw_names:
        raise GravityServiceError(f"轨迹缺少关节名称: {filename}")
    joint_names = [str(name) for name in raw_names]
    if len(set(joint_names)) != len(joint_names) or any(not name for name in joint_names):
        raise GravityServiceError(f"轨迹关节名称非法: {filename}")
    def parse_frames(raw: Any, label: str) -> list[dict[str, float]]:
        if not isinstance(raw, list) or not raw:
            raise GravityServiceError(f"轨迹没有可用{label}: {filename}")
        parsed: list[dict[str, float]] = []
        for index, raw_frame in enumerate(raw):
            if not isinstance(raw_frame, list) or len(raw_frame) != len(joint_names):
                raise GravityServiceError(
                    f"轨迹{label}第{index + 1}帧维度与"
                    f"{len(joint_names)}个关节不一致"
                )
            try:
                values = [float(value) for value in raw_frame]
            except (TypeError, ValueError) as exc:
                raise GravityServiceError(
                    f"轨迹{label}第{index + 1}帧包含非法关节角"
                ) from exc
            if not all(math.isfinite(value) for value in values):
                raise GravityServiceError(
                    f"轨迹{label}第{index + 1}帧包含非有限关节角"
                )
            parsed.append(dict(zip(joint_names, values)))
        return parsed

    frames = parse_frames(raw_frames, "执行帧")
    raw_comparison_frames = trajectory.get("comparison_frames")
    comparison_frames = (
        parse_frames(raw_comparison_frames, "对比帧")
        if raw_comparison_frames is not None
        else frames
    )

    raw_duration = trajectory.get("duration_s", payload.get("duration_s", 6.0))
    try:
        duration_s = float(raw_duration)
    except (TypeError, ValueError) as exc:
        raise GravityServiceError(f"轨迹回放时长非法: {filename}") from exc
    if not math.isfinite(duration_s) or duration_s <= 0:
        duration_s = 6.0
    robot = str(payload.get("robot") or "h2")
    chain_id = str(payload.get("chain_id") or "right_arm")
    tool_visualization = (
        _offline_tool_visualization(robot, chain_id)
        if include_tool_visualization
        else {}
    )
    return {
        "file": path.name,
        "name": str(payload.get("name") or path.stem),
        "robot": robot,
        "chain_id": chain_id,
        "created_at": payload.get("created_at"),
        "recorded_at": trajectory.get("recorded_at"),
        "planner": trajectory.get("planner"),
        "source_waypoints": list(payload.get("waypoints") or []),
        "duration_s": min(120.0, max(0.1, duration_s)),
        "frames": frames,
        "comparison_frames": comparison_frames,
        "joint_names": joint_names,
        "comparison_progress": _sequence_recorded_progress(
            trajectory,
            len(comparison_frames),
            "comparison_progress",
        ),
        "execution_progress": _sequence_recorded_progress(
            trajectory,
            len(frames),
            "execution_progress",
        ),
        "sample_fractions": [],
        "tool_visualization": tool_visualization,
        "collision": None,
        "blocked": False,
    }


def _list_sequences() -> list[dict[str, Any]]:
    if not SEQUENCES_DIR.is_dir():
        return []
    sequences: list[dict[str, Any]] = []
    for path in SEQUENCES_DIR.glob("*.json"):
        try:
            preview = _load_sequence_preview(
                path.name, include_tool_visualization=False
            )
        except GravityServiceError:
            continue
        sequences.append(
            {
                key: deepcopy(preview.get(key))
                for key in (
                    "file",
                    "name",
                    "robot",
                    "chain_id",
                    "created_at",
                    "recorded_at",
                    "planner",
                    "source_waypoints",
                    "duration_s",
                    "joint_names",
                )
            }
            | {
                "frame_count": len(preview["frames"]),
                "comparison_frame_count": len(preview["comparison_frames"]),
            }
        )
    return sorted(
        sequences,
        key=lambda item: (
            str(item.get("created_at") or item.get("recorded_at") or ""),
            str(item.get("name") or ""),
        ),
        reverse=True,
    )


def _densify_joint_frames(
    frames: list[np.ndarray],
    *,
    max_joint_step_rad: float = 0.04,
    minimum_subdivisions: int = 1,
) -> tuple[list[np.ndarray], list[float]]:
    dense = [frames[0]]
    progress = [0.0]
    segment_count = max(1, len(frames) - 1)
    for segment_index, (start, end) in enumerate(zip(frames, frames[1:])):
        count = max(
            minimum_subdivisions,
            int(
                math.ceil(
                    float(np.max(np.abs(end - start)))
                    / max_joint_step_rad
                )
            ),
        )
        for step in range(1, count + 1):
            blend = step / count
            dense.append(start + (end - start) * blend)
            progress.append((segment_index + blend) / segment_count)
    return dense, progress


def _retarget_sequence(
    source_filename: str,
    target_name: str,
    forward_offset_m: float,
    *,
    offset_root_m: list[float] | None = None,
    endpoint_name: str | None = None,
    output_timestamp: str | None = None,
) -> dict[str, Any]:
    """Retarget one recorded motion style over its full TCP arc length."""
    clean_name = str(target_name or "").strip()
    if not clean_name or len(clean_name) > 80:
        raise GravityServiceError("新轨迹名称不能为空且不能超过80字")
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", clean_name)
    if not match:
        raise GravityServiceError("新轨迹名称必须以距离开头，例如0.47避障起手式")
    offset = float(forward_offset_m)
    raw_offset = offset_root_m if offset_root_m is not None else [offset, 0.0, 0.0]
    try:
        offset_vector = np.asarray([float(value) for value in raw_offset], dtype=float)
    except (TypeError, ValueError) as exc:
        raise GravityServiceError("末端偏移必须是根坐标系XYZ三维向量") from exc
    offset_norm = float(np.linalg.norm(offset_vector))
    if (
        offset_vector.shape != (3,)
        or not np.all(np.isfinite(offset_vector))
        or offset_norm < 1e-5
        or offset_norm > 0.20
    ):
        raise GravityServiceError("末端偏移长度必须在0.01mm到200mm之间")

    source = _load_sequence_preview(source_filename)
    visual = source.get("tool_visualization") or {}
    tcp_offset = visual.get("tcp_offset")
    if not isinstance(tcp_offset, list) or len(tcp_offset) != 3:
        raise GravityServiceError("源轨迹缺少可用TCP工作点")
    try:
        import app as app_module
        from core.types import IKRequest, Pose

        model = app_module.robots[source["robot"]]
        solver = app_module.solvers[source["robot"]]["numerical"]
        checker = app_module.collision_checkers[source["robot"]]
        chain_id = source["chain_id"]
        joint_names = model.joint_names(chain_id)
        tool = Pose(xyz=[float(value) for value in tcp_offset])
        original_frames = source["comparison_frames"]
        original_poses = [
            model.tcp_pose(frame, chain_id, tool) for frame in original_frames
        ]
    except Exception as exc:
        raise GravityServiceError(f"无法加载机器人模型或源轨迹: {exc}") from exc

    positions = np.asarray([pose.xyz for pose in original_poses], dtype=float)
    segment_lengths = np.linalg.norm(np.diff(positions, axis=0), axis=1)
    cumulative = np.concatenate([[0.0], np.cumsum(segment_lengths)])
    total_length = float(cumulative[-1])
    if total_length <= 1e-9:
        raise GravityServiceError("源轨迹TCP路径长度为零")
    progress = cumulative / total_length
    weights = 6.0 * progress**5 - 15.0 * progress**4 + 10.0 * progress**3

    retargeted: list[np.ndarray] = []
    position_priority_frames: list[int] = []
    previous_named = original_frames[0]
    retargeted.append(
        np.asarray([float(previous_named[name]) for name in joint_names], dtype=float)
    )
    for index in range(1, len(original_frames)):
        pose = original_poses[index]
        target_xyz = np.asarray(pose.xyz, dtype=float)
        target_xyz += offset_vector * float(weights[index])
        request_kwargs = {
            "chain_id": chain_id,
            "current_joints": previous_named,
            "target_pose": Pose(xyz=target_xyz.tolist(), rpy=list(pose.rpy)),
            "tcp_offset": tool,
            "base_link": model.base_link(chain_id),
            "end_link": model.end_link(chain_id),
            "joint_names": joint_names,
            "seed": previous_named,
        }
        result = solver.solve(
            IKRequest(
                **request_kwargs,
                solver_options={
                    "solve_orientation": True,
                    "tolerance_mm": 2.0,
                    "rotation_tolerance_deg": 2.0,
                    "regularization_weight": 0.001,
                },
            )
        )
        if not result.success:
            result = solver.solve(
                IKRequest(
                    **request_kwargs,
                    solver_options={
                        "solve_orientation": True,
                        "tolerance_mm": 2.0,
                        "rotation_tolerance_deg": 8.0,
                        "rotation_weight": 0.05,
                        "regularization_weight": 0.0002,
                        "max_iterations": 320,
                    },
                )
            )
            if result.success:
                position_priority_frames.append(index)
        if not result.success:
            raise GravityServiceError(
                f"第{index + 1}/{len(original_frames)}帧重定向IK失败："
                f"{result.error_mm:.2f}mm，"
                f"{math.degrees(result.error_rotation):.2f}°"
            )
        previous_named = result.named_target_joints
        retargeted.append(np.asarray(result.target_joints, dtype=float))

    dense_frames, dense_progress = _densify_joint_frames(
        retargeted,
        max_joint_step_rad=0.04,
        minimum_subdivisions=1,
    )

    checks = [
        checker.check_state(frame.tolist(), chain_id, tool)
        for frame in dense_frames
    ]
    collision = next(
        (check for check in checks if check.get("status") == "collision"),
        None,
    )
    if collision is not None:
        pair = collision.get("pair") or {}
        raise GravityServiceError(
            "重定向轨迹发生模型碰撞："
            f"{pair.get('a', '?')} ↔ {pair.get('b', '?')}，"
            f"{collision.get('min_distance_mm', 0):.1f}mm"
        )

    timestamp = output_timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    distance_prefix = match.group(1)
    resolved_endpoint_name = str(endpoint_name or f"{distance_prefix}终点").strip()
    endpoint_file = f"{resolved_endpoint_name}_{timestamp}.json"
    sequence_file = f"{clean_name}_{timestamp}.json"
    endpoint_named = {
        name: float(value)
        for name, value in zip(joint_names, dense_frames[-1])
    }
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    endpoint_payload = {
        "name": resolved_endpoint_name,
        "chain_id": chain_id,
        "named_joints": endpoint_named,
        "created_at": now_text,
        "generated_from": source_filename,
        "forward_offset_m": offset,
        "offset_root_m": offset_vector.tolist(),
    }
    source_waypoints = source.get("source_waypoints") or []
    if not source_waypoints:
        raise GravityServiceError("源轨迹缺少起点路点引用")
    travel = sum(
        float(np.max(np.abs(end - start)))
        for start, end in zip(dense_frames, dense_frames[1:])
    )
    duration_s = max(1.0, travel / 0.35)
    sequence_payload = {
        "name": clean_name,
        "chain_id": chain_id,
        "waypoints": [str(source_waypoints[0]), endpoint_file],
        "created_at": now_text,
        "trajectory": {
            "frames": [
                [round(float(value), 5) for value in frame]
                for frame in dense_frames
            ],
            "comparison_frames": [
                [round(float(value), 5) for value in frame]
                for frame in retargeted
            ],
            "joint_names": joint_names,
            "comparison_progress": [
                round(float(value), 8)
                for value in np.linspace(0.0, 1.0, len(retargeted))
            ],
            "execution_progress": [
                round(float(value), 8) for value in dense_progress
            ],
            "duration_s": round(duration_s, 3),
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "planner": "task-space-style-retarget",
            "retarget": {
                "source_sequence": source_filename,
                "forward_offset_m": offset,
                "offset_root_m": offset_vector.tolist(),
                "axis_root": (
                    offset_vector / offset_norm
                ).tolist(),
                "progress": "quintic-smoothstep-over-tcp-arclength",
                "timing": "source-phase-recorded-separately",
                "posture_reference": "previous-planned-frame",
                "preserve_tcp_orientation": True,
                "position_priority_frames": position_priority_frames,
                "position_priority_rotation_tolerance_deg": (
                    8.0 if position_priority_frames else None
                ),
                "max_joint_step_rad": 0.04,
            },
        },
    }
    _atomic_json(REGULAR_WAYPOINTS_DIR / endpoint_file, endpoint_payload)
    _atomic_json(SEQUENCES_DIR / sequence_file, sequence_payload)
    minimum_clearance = min(
        float(check.get("min_distance_mm") or math.inf) for check in checks
    )
    return {
        "sequence_file": sequence_file,
        "endpoint_file": endpoint_file,
        "name": clean_name,
        "frame_count": len(dense_frames),
        "comparison_frame_count": len(retargeted),
        "duration_s": round(duration_s, 3),
        "forward_offset_m": offset,
        "offset_root_m": offset_vector.tolist(),
        "minimum_model_clearance_mm": minimum_clearance,
        "source_file": source_filename,
    }


def _list_runs(*, include_samples: bool = False) -> list[dict[str, Any]]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for path in RUNS_DIR.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                continue
            value["storage_version"] = (
                path.parent.name if path.parent != RUNS_DIR else "legacy_flat"
            )
            value["storage_relative_path"] = str(path.relative_to(RUNS_DIR))
            if not include_samples:
                value = {k: v for k, v in value.items() if k != "samples"}
            runs.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(runs, key=lambda item: str(item.get("started_at", "")), reverse=True)


def _offline_tool_visualization(robot: str, chain_id: str) -> dict[str, Any]:
    """Use the newest locally saved calibrated tool points, without 18001."""
    for run in _list_runs():
        if str(run.get("robot") or "h2") != robot:
            continue
        if str(run.get("chain_id") or "right_arm") != chain_id:
            continue
        raw = run.get("tool_visualization")
        if not isinstance(raw, dict):
            continue
        try:
            tcp = [float(value) for value in raw.get("tcp_offset") or []]
            if len(tcp) != 3 or not all(math.isfinite(value) for value in tcp):
                continue
            markers: dict[str, list[float]] = {}
            for name, raw_point in (raw.get("markers") or {}).items():
                point = [float(value) for value in raw_point]
                if len(point) == 3 and all(math.isfinite(value) for value in point):
                    markers[str(name)] = point
        except (TypeError, ValueError):
            continue
        return {
            "tcp_offset": tcp,
            "markers": markers,
            "reference_marker": raw.get("reference_marker"),
            "wrist_link": raw.get("wrist_link"),
            "source": "saved_gravity_run",
            "source_run_id": run.get("id"),
        }

    try:
        import app as app_module

        model = app_module.robots[robot]
        return {
            "tcp_offset": list(model.tcp_offset(chain_id).xyz),
            "markers": {},
            "reference_marker": None,
            "wrist_link": model.end_link(chain_id),
            "source": "robot_config_fallback",
            "source_run_id": None,
        }
    except Exception:
        return {}


def _ik_validation_version(record: dict[str, Any]) -> str:
    profile = record.get("gravity_profile")
    version = str(profile.get("version") or "") if isinstance(profile, dict) else ""
    return version if VERSION_PATTERN.fullmatch(version) else "unversioned"


def _ik_validation_path(record: dict[str, Any]) -> Path:
    return (
        IK_VALIDATIONS_DIR
        / _ik_validation_version(record)
        / f"{record['id']}.json"
    )


def _save_ik_validation(record: dict[str, Any]) -> None:
    version = _ik_validation_version(record)
    record["storage_version"] = version
    record["storage_relative_path"] = f"{version}/{record['id']}.json"
    _atomic_json(_ik_validation_path(record), record)


def _list_ik_validations(
    *, include_samples: bool = False
) -> list[dict[str, Any]]:
    IK_VALIDATIONS_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for path in IK_VALIDATIONS_DIR.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                continue
            value["storage_version"] = (
                path.parent.name
                if path.parent != IK_VALIDATIONS_DIR
                else "legacy_flat"
            )
            value["storage_relative_path"] = str(
                path.relative_to(IK_VALIDATIONS_DIR)
            )
            if not include_samples:
                value.pop("samples", None)
                value.pop("execution", None)
            records.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(
        records,
        key=lambda item: str(item.get("started_at", "")),
        reverse=True,
    )


def _find_ik_validation_path(validation_id: str) -> Path | None:
    safe_id = _safe_id(validation_id)
    legacy = IK_VALIDATIONS_DIR / f"{safe_id}.json"
    if legacy.is_file():
        return legacy
    matches = list(IK_VALIDATIONS_DIR.glob(f"*/{safe_id}.json"))
    return matches[0] if matches else None


def _load_ik_validation(validation_id: str) -> dict[str, Any]:
    path = _find_ik_validation_path(validation_id)
    if path is None:
        raise GravityServiceError("IK验证记录不存在")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GravityServiceError(f"IK验证记录损坏: {exc}") from exc
    if not isinstance(value, dict):
        raise GravityServiceError("IK验证记录格式错误")
    return value


def _batch_path(batch_id: str) -> Path:
    return BATCHES_DIR / f"{_safe_id(batch_id)}.json"


def _save_batch(batch: dict[str, Any]) -> None:
    batch["updated_at"] = _now()
    _atomic_json(_batch_path(str(batch["id"])), batch)


def _batch_summary(batch: dict[str, Any] | None) -> dict[str, Any] | None:
    if batch is None:
        return None
    return deepcopy(batch)


def _request_reach(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    try:
        response = _http.request(
            method,
            f"{_reach_base}{path}",
            json=body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise GravityServiceError(f"18001不可达: {exc}") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise GravityServiceError(
            f"18001返回非JSON响应（HTTP {response.status_code}）"
        ) from exc
    if response.status_code >= 400 or (isinstance(data, dict) and data.get("ok") is False):
        message = data.get("error") if isinstance(data, dict) else None
        raise GravityServiceError(message or f"18001请求失败（HTTP {response.status_code}）")
    if not isinstance(data, dict):
        raise GravityServiceError("18001返回格式错误")
    return data


def _reach_status() -> dict[str, Any]:
    return _request_reach("GET", "/api/reach/status", timeout=1.5)


def _tool_visualization(reach: dict[str, Any]) -> dict[str, Any]:
    return {
        "tcp_offset": reach.get("p_tool"),
        "markers": reach.get("p_tool_wrist_m_by_marker") or {},
        "reference_marker": reach.get("p_tool_reference_marker"),
        "wrist_link": reach.get("wrist_link"),
    }


def _set_operation(**changes: Any) -> None:
    with _lock:
        _operation.update(changes)
        _operation["updated_at"] = _now()


def _operation_snapshot() -> dict[str, Any]:
    with _lock:
        return deepcopy(_operation)


def _plan_summary(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        key: deepcopy(plan.get(key))
        for key in (
            "id",
            "point_id",
            "point_name",
            "created_at",
            "duration_s",
            "max_speed_rad_s",
            "intermediate_stops",
            "planner",
            "waypoint_count",
            "blocked",
            "blocked_reason",
            "collision",
            "preview",
        )
    }


def _mean_vector(samples: list[dict[str, Any]], path: tuple[str, ...]) -> dict[str, Any] | None:
    vectors: list[list[float]] = []
    for sample in samples:
        value: Any = sample
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if not isinstance(value, list) or not value:
            continue
        try:
            vector = [float(v) for v in value]
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in vector):
            vectors.append(vector)
    if not vectors:
        return None
    width = len(vectors[0])
    vectors = [v for v in vectors if len(v) == width]
    if not vectors:
        return None
    means = [statistics.fmean(v[index] for v in vectors) for index in range(width)]
    stds = [
        statistics.pstdev(v[index] for v in vectors) if len(vectors) > 1 else 0.0
        for index in range(width)
    ]
    return {"mean": means, "std": stds, "count": len(vectors)}


def _aggregate_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    fields = {
        "command_rad": ("arm", "cmd_rad"),
        "measured_rad": ("arm", "measured_rad"),
        "desired_rad": ("arm", "desired_rad"),
        "measured_velocity_rad_s": ("arm", "measured_dq_rad_s"),
        "estimated_joint_torque_nm": ("arm", "tau_est_nm"),
        "gravity_torque_nm": ("arm", "tau_grav_nm"),
        "estimated_pd_support_nm": ("arm", "estimated_pd_support_nm"),
        "feedforward_torque_nm": ("arm", "command_snapshot", "tau_ff_nm"),
        "tcp_command_root_m": ("arm", "tcp_cmd_root_m"),
        "tcp_measured_root_m": ("arm", "tcp_measured_root_m"),
    }
    result = {
        name: summary
        for name, path in fields.items()
        if (summary := _mean_vector(samples, path)) is not None
    }
    measured = result.get("measured_rad", {}).get("mean")
    command = result.get("command_rad", {}).get("mean")
    if measured and command and len(measured) == len(command):
        result["command_minus_measured_rad"] = [
            float(c - m) for c, m in zip(command, measured)
        ]
        result["command_minus_measured_deg"] = [
            math.degrees(c - m) for c, m in zip(command, measured)
        ]
    return result


def _finite_vector(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise GravityServiceError(f"{name}缺失")
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise GravityServiceError(f"{name}格式错误") from exc
    if not all(math.isfinite(item) for item in vector):
        raise GravityServiceError(f"{name}包含非有限数值")
    return vector


def _ik_error_metrics(
    execution: dict[str, Any],
    aggregate: dict[str, Any],
) -> dict[str, Any]:
    tcp = execution.get("tcp") or {}
    pick = execution.get("pick_context") or {}
    target = _finite_vector(
        pick.get("p_root") or tcp.get("pick_target_root"),
        "点云目标TCP",
    )
    planned = _finite_vector(tcp.get("planned_root"), "IK理论TCP")
    measured = _finite_vector(
        (aggregate.get("tcp_measured_root_m") or {}).get("mean"),
        "实测TCP",
    )
    if not (len(target) == len(planned) == len(measured) == 3):
        raise GravityServiceError("TCP坐标必须为三维")

    def delta(destination: list[float], source: list[float]) -> list[float]:
        return [
            (destination_value - source_value) * 1000.0
            for destination_value, source_value in zip(destination, source)
        ]

    def metric(vector: list[float]) -> dict[str, Any]:
        return {
            "delta_mm": vector,
            "norm_mm": float(np.linalg.norm(vector)),
        }

    ik_delta = delta(planned, target)
    tracking_delta = delta(measured, planned)
    total_delta = delta(measured, target)
    return {
        "target_root_m": target,
        "planned_root_m": planned,
        "measured_root_m": measured,
        "ik": metric(ik_delta),
        "tracking": metric(tracking_delta),
        "total": metric(total_delta),
    }


def _sample_ik_execution(
    execution: dict[str, Any],
    *,
    sample_s: float,
    sample_hz: float,
    command_tolerance_rad: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    target = np.asarray(
        _finite_vector(execution.get("target_rad"), "IK理论关节值"),
        dtype=float,
    )
    exec_status = _request_reach(
        "GET", "/api/reach/exec_status", timeout=2.0
    )
    if exec_status.get("running"):
        raise GravityServiceError("18001仍在执行轨迹，请等待主轨迹完成")

    samples: list[dict[str, Any]] = []
    started = time.monotonic()
    interval = 1.0 / sample_hz
    while time.monotonic() - started < sample_s:
        tick = time.monotonic()
        sample = _request_reach(
            "GET", "/api/reach/diagnostics", timeout=2.0
        )
        arm = sample.get("arm") or {}
        if not arm.get("armed"):
            raise GravityServiceError("补采期间手臂未处于接管保持状态")
        command = np.asarray(
            _finite_vector(arm.get("cmd_rad"), "当前指令关节值"),
            dtype=float,
        )
        if command.shape != target.shape:
            raise GravityServiceError("当前指令与IK理论关节维度不一致")
        gap = float(np.max(np.abs(command - target)))
        if gap > command_tolerance_rad:
            raise GravityServiceError(
                "当前保持姿态已不是该次IK终点："
                f"最大指令差 {gap:.4f} rad > {command_tolerance_rad:.4f} rad"
            )
        samples.append(sample)
        time.sleep(max(0.0, interval - (time.monotonic() - tick)))
    minimum = max(3, int(sample_s * sample_hz * 0.5))
    if len(samples) < minimum:
        raise GravityServiceError(
            f"IK验证有效采样不足（仅{len(samples)}帧，需要至少{minimum}帧）"
        )
    return samples, _aggregate_samples(samples)


def _detect_trajectory_start(execution: dict[str, Any]) -> dict[str, Any]:
    """Match the planned trajectory's first frame to a gravity waypoint."""
    values = execution.get("trajectory_start_rad")
    if values is None:
        handoff = execution.get("command_handoff") or {}
        values = (
            handoff.get("planned_start_rad")
            if isinstance(handoff, dict)
            else None
        )
    names = list(execution.get("joint_names") or [])
    try:
        start = np.asarray(values, dtype=float).reshape(-1)
    except (TypeError, ValueError):
        start = np.asarray([], dtype=float)
    if (
        not names
        or start.size != len(names)
        or not np.all(np.isfinite(start))
    ):
        return {
            "source": "trajectory_first_frame",
            "matched": False,
            "label": "未匹配起点",
            "reason": "执行记录缺少有效的轨迹第一帧",
            "threshold_rad": IK_START_MATCH_MAX_ERROR_RAD,
        }

    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for point in _list_points():
        if (
            point.get("chain_id")
            and execution.get("chain_id")
            and point.get("chain_id") != execution.get("chain_id")
        ):
            continue
        named = point.get("named_joints")
        if not isinstance(named, dict):
            continue
        try:
            waypoint = np.asarray(
                [float(named[name]) for name in names], dtype=float
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not np.all(np.isfinite(waypoint)):
            continue
        delta = start - waypoint
        candidates.append(
            (
                float(np.max(np.abs(delta))),
                float(np.sqrt(np.mean(np.square(delta)))),
                point,
            )
        )

    if not candidates:
        return {
            "source": "trajectory_first_frame",
            "matched": False,
            "label": "未匹配起点",
            "reason": "重力位点库中没有可比较的同链位点",
            "threshold_rad": IK_START_MATCH_MAX_ERROR_RAD,
            "trajectory_start_rad": start.tolist(),
        }

    max_error, rms_error, nearest = min(
        candidates, key=lambda item: (item[0], item[1])
    )
    matched = max_error <= IK_START_MATCH_MAX_ERROR_RAD
    nearest_label = str(
        nearest.get("name") or nearest.get("id") or "未命名位点"
    )
    return {
        "source": "trajectory_first_frame",
        "matched": matched,
        "label": nearest_label if matched else "未匹配起点",
        "point_id": nearest.get("id") if matched else None,
        "nearest_label": nearest_label,
        "nearest_point_id": nearest.get("id"),
        "max_error_rad": max_error,
        "max_error_deg": math.degrees(max_error),
        "rms_error_rad": rms_error,
        "rms_error_deg": math.degrees(rms_error),
        "threshold_rad": IK_START_MATCH_MAX_ERROR_RAD,
        "trajectory_start_rad": start.tolist(),
    }


def _ik_candidate_summary(
    execution: dict[str, Any],
    captured_by_execution: dict[str, str],
) -> dict[str, Any]:
    pick = execution.get("pick_context") or {}
    start_detection = _detect_trajectory_start(execution)
    return {
        "id": execution.get("id"),
        "ts": execution.get("ts"),
        "segment": execution.get("segment"),
        "result": execution.get("result"),
        "robot": execution.get("robot"),
        "chain_id": execution.get("chain_id"),
        "gravity_version": (
            execution.get("gravity_profile") or {}
        ).get("version"),
        "source_frame_id": pick.get("source_frame_id"),
        "capture_id": pick.get("capture_id"),
        "pixel": pick.get("pixel"),
        "target_root_m": pick.get("p_root"),
        "start_label": start_detection["label"],
        "start_detection": start_detection,
        "captured_validation_id": captured_by_execution.get(
            str(execution.get("id"))
        ),
    }


@app.get("/")
def page():
    return FileResponse(WEB_DIR / "gravity.html")


@app.get("/api/gravity/sequences")
def offline_sequences():
    return {
        "ok": True,
        "source_directory": str(SEQUENCES_DIR),
        "sequences": _list_sequences(),
    }


@app.post("/api/gravity/sequences/retarget")
def retarget_offline_sequence(body: dict[str, Any]):
    try:
        result = _retarget_sequence(
            str(body.get("source_file") or ""),
            str(body.get("target_name") or ""),
            float(body.get("forward_offset_m")),
        )
        return {"ok": True, "result": result}
    except (GravityServiceError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/gravity/sequences/{filename}/preview")
def offline_sequence_preview(filename: str):
    try:
        sequence = _load_sequence_preview(filename)
        return {
            "ok": True,
            "plan": {
                "id": f"offline:{sequence['file']}",
                "source": "offline_sequence",
                **sequence,
            },
        }
    except GravityServiceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)


@app.get("/api/gravity/robot_metadata")
def gravity_robot_metadata(robot: str = "h2"):
    try:
        import app as app_module

        return {"ok": True, "metadata": app_module.robot_metadata(robot)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/gravity/ik_validation")
def ik_validation_status():
    records = _list_ik_validations()
    captured = {
        str(record.get("execution_id")): str(record.get("id"))
        for record in records
        if record.get("execution_id") and record.get("id")
    }
    try:
        payload = _request_reach(
            "GET",
            "/api/reach/executions?limit=20&pointcloud_only=true",
            timeout=2.0,
        )
        candidates = [
            _ik_candidate_summary(execution, captured)
            for execution in payload.get("executions") or []
            if str(execution.get("segment") or "").startswith("主轨迹")
            and execution.get("result") == "done"
        ]
        reach_error = None
    except GravityServiceError as exc:
        candidates = []
        reach_error = str(exc)
    return {
        "ok": True,
        "candidates": candidates,
        "records": records,
        "reach_error": reach_error,
    }


@app.post("/api/gravity/ik_validation/capture/{execution_id}")
def capture_ik_validation(execution_id: str, body: dict | None = None):
    body = body or {}
    try:
        if _operation_snapshot().get("phase") in {
            "planning", "executing", "settling", "sampling"
        }:
            raise GravityServiceError(
                "重力位点实验正在运行，不能同时补采IK落点"
            )
        safe_execution_id = _safe_id(execution_id)
        existing = next(
            (
                record
                for record in _list_ik_validations()
                if record.get("execution_id") == safe_execution_id
            ),
            None,
        )
        if existing is not None:
            raise GravityServiceError(
                f"该次执行已经保存为IK验证 {existing.get('id')}"
            )
        execution_payload = _request_reach(
            "GET",
            f"/api/reach/executions/{safe_execution_id}",
            timeout=2.0,
        )
        execution = execution_payload.get("execution")
        if not isinstance(execution, dict):
            raise GravityServiceError("18001执行记录格式错误")
        if execution.get("result") != "done":
            raise GravityServiceError("只能验证正常完成的执行")
        if not str(execution.get("segment") or "").startswith("主轨迹"):
            raise GravityServiceError("该记录不是点云取点后的主轨迹")
        pick = execution.get("pick_context") or {}
        if pick.get("selection_mode") != "frozen_rgbd_pointcloud":
            raise GravityServiceError("该主轨迹不是由7005冻结点云选点产生")

        sample_s = min(
            10.0, max(0.5, float(body.get("sample_s", 2.0)))
        )
        sample_hz = min(
            30.0, max(2.0, float(body.get("sample_hz", 10.0)))
        )
        command_tolerance = min(
            0.15,
            max(0.005, float(body.get("command_tolerance_rad", 0.05))),
        )
        start_detection = _detect_trajectory_start(execution)
        start_label = str(start_detection["label"])[:80]
        note = str(body.get("note") or "").strip()[:300]
        started_at = _now()
        samples, aggregate = _sample_ik_execution(
            execution,
            sample_s=sample_s,
            sample_hz=sample_hz,
            command_tolerance_rad=command_tolerance,
        )
        metrics = _ik_error_metrics(execution, aggregate)
        reach = _reach_status()
        validation = {
            "schema": "pointcloud-ik-validation/v1",
            "id": uuid.uuid4().hex[:12],
            "execution_id": safe_execution_id,
            "status": "completed",
            "started_at": started_at,
            "completed_at": _now(),
            "start_label": start_label,
            "start_detection": start_detection,
            "note": note,
            "robot": execution.get("robot") or "h2",
            "chain_id": execution.get("chain_id") or "right_arm",
            "joint_names": list(execution.get("joint_names") or []),
            "theoretical_rad": _finite_vector(
                execution.get("target_rad"), "IK理论关节值"
            ),
            "gravity_profile": deepcopy(
                execution.get("gravity_profile")
                or reach.get("gravity_profile")
                or {}
            ),
            "pick_context": deepcopy(pick),
            "execution": deepcopy(execution),
            "settings": {
                "sample_s": sample_s,
                "sample_hz": sample_hz,
                "command_tolerance_rad": command_tolerance,
            },
            "sample_count": len(samples),
            "samples": samples,
            "aggregate": aggregate,
            "metrics": metrics,
            "tool_visualization": _tool_visualization(reach),
        }
        _save_ik_validation(validation)
        return {
            "ok": True,
            "validation": {
                key: deepcopy(value)
                for key, value in validation.items()
                if key != "samples"
            },
        }
    except (GravityServiceError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )


@app.get("/api/gravity/plans/{plan_id}/preview")
def gravity_plan_preview(plan_id: str):
    with _lock:
        plan = deepcopy(_plan)
    if plan is None or plan.get("id") != plan_id:
        return JSONResponse({"ok": False, "error": "规划不存在或已被替换"}, status_code=404)
    return {
        "ok": True,
        "plan": {
            "id": plan["id"],
            "point_id": plan["point_id"],
            "point_name": plan["point_name"],
            "robot": plan.get("robot") or "h2",
            "chain_id": plan.get("chain_id") or "right_arm",
            "duration_s": plan["duration_s"],
            "planner": plan.get("planner"),
            "blocked": bool(plan.get("blocked")),
            "blocked_reason": plan.get("blocked_reason"),
            "collision": plan.get("collision"),
            "frames": plan["waypoints"],
            "sample_fractions": plan.get("preview", {}).get("sample_fractions", []),
            "tool_visualization": plan.get("tool_visualization") or {},
        },
    }


@app.get("/api/gravity/status")
def gravity_status():
    try:
        reach = _reach_status()
        reach_error = None
    except GravityServiceError as exc:
        reach = None
        reach_error = str(exc)
    points = _list_points()
    runs = _list_runs()
    try:
        gravity_profiles = load_registry(GRAVITY_PROFILES_PATH)
        gravity_profiles_error = None
    except ValueError as exc:
        gravity_profiles = None
        gravity_profiles_error = str(exc)
    completed_ids = {
        str(run.get("point_id"))
        for run in runs
        if run.get("status") == "completed"
    }
    with _lock:
        plan = _plan_summary(_plan)
        batch = _batch_summary(_batch)
    return {
        "ok": True,
        "reach_base": _reach_base,
        "reach": reach,
        "reach_error": reach_error,
        "operation": _operation_snapshot(),
        "plan": plan,
        "batch": batch,
        "gravity_profiles": gravity_profiles,
        "gravity_profiles_error": gravity_profiles_error,
        "points": points,
        "runs": runs[:30],
        "summary": {
            "points": len(points),
            "completed_points": len(completed_ids),
            "completed_runs": sum(run.get("status") == "completed" for run in runs),
            "failed_runs": sum(run.get("status") == "failed" for run in runs),
        },
    }


@app.post("/api/gravity/profiles")
def save_gravity_profile(body: dict[str, Any]):
    try:
        profile = create_profile(
            version=str(body.get("version") or "").strip(),
            label=str(body.get("label") or "").strip(),
            description=str(body.get("description") or "").strip(),
            parameters=body.get("parameters"),
            path=GRAVITY_PROFILES_PATH,
            activate=bool(body.get("activate")),
            source="gravity_calibration",
        )
        registry = load_registry(GRAVITY_PROFILES_PATH)
        return {
            "ok": True,
            "profile": profile,
            "active_version": registry["active_version"],
            "applies_on_next_18001_start": True,
        }
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/gravity/profiles/{version}/activate")
def activate_gravity_profile(version: str):
    try:
        profile = activate_profile(version, GRAVITY_PROFILES_PATH)
        return {
            "ok": True,
            "profile": profile,
            "active_version": profile["version"],
            "applies_on_next_18001_start": True,
            "message": "已切换配置；重启18001后生效，当前手臂前馈未被在线修改",
        }
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/gravity/importable_waypoints")
def importable_waypoints():
    items = _list_regular_waypoints()
    return {
        "ok": True,
        "source_directory": str(REGULAR_WAYPOINTS_DIR),
        "waypoints": items,
        "available_count": sum(not item["already_imported"] for item in items),
    }


@app.post("/api/gravity/waypoints/import")
def import_waypoints(body: dict[str, Any]):
    raw_files = body.get("files") or []
    files = list(dict.fromkeys(str(value) for value in raw_files))
    if not files:
        return JSONResponse({"ok": False, "error": "请至少选择一个原位点"}, status_code=400)
    prefix = str(body.get("name_prefix") or "").strip()
    if len(prefix) > 40:
        return JSONResponse({"ok": False, "error": "名称前缀不能超过40字"}, status_code=400)
    try:
        source_items = [_load_regular_waypoint(filename) for filename in files]
    except GravityServiceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    points = _list_points()
    imported_sources = {
        str(point.get("source_waypoint_file"))
        for point in points
        if point.get("source_waypoint_file")
    }
    next_order = max([int(point.get("order", 0)) for point in points] or [0]) + 1
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for source in source_items:
        if source["file"] in imported_sources:
            skipped.append({"file": source["file"], "reason": "已经导入"})
            continue
        point_id = uuid.uuid4().hex[:12]
        timestamp = _now()
        point = {
            "schema_version": 1,
            "id": point_id,
            "name": f"{prefix}{source['name']}",
            "note": f"从普通位点导入：{source['file']}",
            "order": next_order,
            "chain_id": source["chain_id"],
            "robot": source["robot"],
            "joint_names": source["joint_names"],
            "named_joints": source["named_joints"],
            "created_at": timestamp,
            "updated_at": timestamp,
            "completed_runs": 0,
            "last_completed_at": None,
            "last_run_id": None,
            "source": "regular_waypoint_import",
            "source_waypoint_file": source["file"],
            "source_waypoint_created_at": source["created_at"],
            "imported_at": timestamp,
        }
        _atomic_json(_point_path(point_id), point)
        imported.append(point)
        imported_sources.add(source["file"])
        next_order += 1
    return {
        "ok": True,
        "imported": imported,
        "imported_count": len(imported),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


@app.post("/api/gravity/waypoints")
def save_waypoint(body: dict[str, Any]):
    name = str(body.get("name") or "").strip()
    note = str(body.get("note") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "请输入位点名称"}, status_code=400)
    if len(name) > 80 or len(note) > 500:
        return JSONResponse({"ok": False, "error": "名称或备注过长"}, status_code=400)
    try:
        reach = _reach_status()
        joints = _request_reach("GET", "/api/reach/joints")
    except GravityServiceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    named = joints.get("named_joints")
    if not isinstance(named, dict) or not named:
        return JSONResponse({"ok": False, "error": "18001没有返回有效关节角"}, status_code=502)
    points = _list_points()
    point_id = uuid.uuid4().hex[:12]
    point = {
        "schema_version": 1,
        "id": point_id,
        "name": name,
        "note": note,
        "order": max([int(item.get("order", 0)) for item in points] or [0]) + 1,
        "chain_id": str(reach.get("chain_id") or "right_arm"),
        "robot": str(reach.get("robot") or "h2"),
        "joint_names": list(named),
        "named_joints": {str(k): float(v) for k, v in named.items()},
        "created_at": _now(),
        "updated_at": _now(),
        "completed_runs": 0,
        "last_completed_at": None,
        "last_run_id": None,
    }
    _atomic_json(_point_path(point_id), point)
    return {"ok": True, "point": point}


@app.patch("/api/gravity/waypoints/{point_id}")
def update_waypoint(point_id: str, body: dict[str, Any]):
    try:
        point = _load_point(point_id)
        if "name" in body:
            name = str(body["name"] or "").strip()
            if not name or len(name) > 80:
                raise GravityServiceError("位点名称不能为空且不能超过80字")
            point["name"] = name
        if "note" in body:
            note = str(body["note"] or "").strip()
            if len(note) > 500:
                raise GravityServiceError("备注不能超过500字")
            point["note"] = note
        if "order" in body:
            point["order"] = int(body["order"])
        point["updated_at"] = _now()
        _atomic_json(_point_path(point_id), point)
        return {"ok": True, "point": point}
    except (GravityServiceError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.delete("/api/gravity/waypoints/{point_id}")
def delete_waypoint(point_id: str):
    try:
        path = _point_path(point_id)
    except GravityServiceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    with _lock:
        if _operation.get("point_id") == point_id and _operation.get("phase") in {
            "executing", "settling", "sampling"
        }:
            return JSONResponse({"ok": False, "error": "该位点正在执行，不能删除"}, status_code=409)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "位点不存在"}, status_code=404)
    path.unlink()
    return {"ok": True}


@app.post("/api/gravity/arm")
def arm_control(body: dict[str, Any]):
    try:
        if bool(body.get("on")):
            result = _request_reach("POST", "/api/reach/arm", body={})
        else:
            result = _request_reach("POST", "/api/reach/disarm", body={})
        return {"ok": True, "reach": result}
    except GravityServiceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.post("/api/gravity/hand_move")
def hand_move(body: dict[str, Any]):
    try:
        result = _request_reach(
            "POST", "/api/reach/hand_move", body={"on": bool(body.get("on"))}
        )
        return {"ok": True, "reach": result}
    except GravityServiceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.post("/api/gravity/stop")
def stop_execution():
    with _lock:
        _run_cancel.set()
    try:
        result = _request_reach("POST", "/api/reach/stop", body={})
    except GravityServiceError as exc:
        result = {"error": str(exc)}
    _set_operation(
        phase="error",
        message="实验已由操作员停止",
        error=result.get("error"),
        progress=0.0,
    )
    with _lock:
        if _batch is not None:
            _batch["pause_requested"] = True
            _batch["state"] = "paused"
            _save_batch(_batch)
    return {"ok": "error" not in result, "reach": result}


@app.post("/api/gravity/plan/{point_id}")
def plan_waypoint(point_id: str, body: dict[str, Any] | None = None):
    global _plan
    body = body or {}
    with _lock:
        if _operation.get("phase") in {"executing", "settling", "sampling"}:
            return JSONResponse({"ok": False, "error": "实验正在运行，不能重新规划"}, status_code=409)
    try:
        point = _load_point(point_id)
        reach = _reach_status()
        joints = _request_reach("GET", "/api/reach/joints")
        duration = min(30.0, max(2.0, float(body.get("duration_s", 6.0))))
        max_speed = min(0.5, max(0.05, float(body.get("max_speed_rad_s", 0.2))))
        intermediate_stops = min(8, max(0, int(body.get("intermediate_stops", 0))))
        payload = {
            "robot": point.get("robot") or reach.get("robot") or "h2",
            "chain_id": point.get("chain_id") or reach.get("chain_id") or "right_arm",
            "current_joints": joints["named_joints"],
            "target_joints": point["named_joints"],
            "duration": duration,
            "steps": min(300, max(40, int(body.get("steps", 120)))),
            "planner_type": str(body.get("planner_type") or "linear"),
            "check_collision": bool(body.get("check_collision", True)),
        }
        _set_operation(
            phase="planning",
            point_id=point_id,
            plan_id=None,
            run_id=None,
            message=f"正在规划到「{point['name']}」",
            error=None,
            progress=0.0,
        )
        planned = _request_reach(
            "POST", "/api/trajectory/plan", body=payload, timeout=45.0
        )
        collision = planned.get("collision")
        blocked = bool(
            isinstance(collision, dict) and collision.get("status") == "collision"
        )
        blocked_reason = (
            str(collision.get("rrt_error") or "规划轨迹存在碰撞")
            if blocked
            else None
        )
        waypoints = [
            item["named_joints"]
            for item in planned.get("waypoints") or []
            if isinstance(item, dict) and isinstance(item.get("named_joints"), dict)
        ]
        if len(waypoints) < 2:
            raise GravityServiceError("规划器没有返回有效轨迹")
        tcp_path = []
        for index, item in enumerate(planned.get("waypoints") or []):
            if index % max(1, len(waypoints) // 30) != 0 and index != len(waypoints) - 1:
                continue
            pose = item.get("tcp_pose") if isinstance(item, dict) else None
            if isinstance(pose, dict) and isinstance(pose.get("xyz"), list):
                tcp_path.append(pose["xyz"])
        plan = {
            "id": uuid.uuid4().hex[:12],
            "point_id": point_id,
            "point_name": point["name"],
            "robot": payload["robot"],
            "chain_id": payload["chain_id"],
            "created_at": _now(),
            "duration_s": duration,
            "max_speed_rad_s": max_speed,
            "intermediate_stops": intermediate_stops,
            "planner": planned.get("planner"),
            "waypoint_count": len(waypoints),
            "blocked": blocked,
            "blocked_reason": blocked_reason,
            "collision": collision,
            "waypoints": waypoints,
            "tool_visualization": _tool_visualization(reach),
            "preview": {
                "tcp_path_root_m": tcp_path,
                "start_joints": waypoints[0],
                "target_joints": waypoints[-1],
                "sample_fractions": [
                    index / (intermediate_stops + 1)
                    for index in range(1, intermediate_stops + 1)
                ] + [1.0],
            },
        }
        with _lock:
            _plan = plan
        if blocked:
            _set_operation(
                phase="error",
                plan_id=plan["id"],
                message="规划被碰撞检查阻止，可在预览中显示碰撞体",
                error=blocked_reason,
                progress=0.0,
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": blocked_reason,
                    "plan": _plan_summary(plan),
                },
                status_code=409,
            )
        _set_operation(
            phase="ready",
            plan_id=plan["id"],
            message=f"规划完成，共{len(waypoints)}帧；等待真机执行确认",
            progress=0.0,
        )
        return {"ok": True, "plan": _plan_summary(plan)}
    except (GravityServiceError, KeyError, TypeError, ValueError) as exc:
        _set_operation(phase="error", message="规划失败", error=str(exc), progress=0.0)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _run_version(run: dict[str, Any]) -> str:
    profile = run.get("gravity_profile")
    version = str(profile.get("version") or "") if isinstance(profile, dict) else ""
    return version if VERSION_PATTERN.fullmatch(version) else "unversioned"


def _run_path(run: dict[str, Any]) -> Path:
    return RUNS_DIR / _run_version(run) / f"{run['id']}.json"


def _save_run(run: dict[str, Any]) -> None:
    version = _run_version(run)
    run["storage_version"] = version
    run["storage_relative_path"] = f"{version}/{run['id']}.json"
    _atomic_json(_run_path(run), run)


def _mark_point_completed(point_id: str, run_id: str, completed_at: str) -> None:
    point = _load_point(point_id)
    point["completed_runs"] = int(point.get("completed_runs") or 0) + 1
    point["last_run_id"] = run_id
    point["last_completed_at"] = completed_at
    point["updated_at"] = completed_at
    _atomic_json(_point_path(point_id), point)


def _split_plan_waypoints(
    waypoints: list[dict[str, float]], intermediate_stops: int
) -> list[dict[str, Any]]:
    """Split one continuous plan into overlapping hold-and-sample segments."""
    if len(waypoints) < 2:
        raise GravityServiceError("轨迹至少需要两个路点")
    stop_count = min(max(0, int(intermediate_stops)), len(waypoints) - 2)
    last = len(waypoints) - 1
    boundaries = [
        round(index * last / (stop_count + 1))
        for index in range(1, stop_count + 1)
    ] + [last]
    segments: list[dict[str, Any]] = []
    start = 0
    for sequence, end in enumerate(boundaries, 1):
        if end <= start:
            continue
        segments.append(
            {
                "sequence": sequence,
                "start_index": start,
                "end_index": end,
                "fraction": end / last,
                "waypoints": waypoints[start : end + 1],
                "final": end == last,
            }
        )
        start = end
    return segments


def _wait_for_segment(
    *,
    segment_number: int,
    segment_count: int,
    duration_s: float,
    cancel_event: threading.Event,
) -> None:
    deadline = time.monotonic() + duration_s + 40.0
    while time.monotonic() < deadline:
        if cancel_event.is_set():
            raise GravityServiceError("实验已由操作员停止")
        status = _request_reach("GET", "/api/reach/exec_status", timeout=2.0)
        local = min(1.0, max(0.0, float(status.get("progress") or 0.0)))
        _set_operation(
            phase="executing",
            message=(
                f"第{segment_number}/{segment_count}段 · "
                f"{status.get('message') or '轨迹执行中'}"
            ),
            progress=((segment_number - 1) + local * 0.65) / segment_count,
        )
        if not status.get("running"):
            message = str(status.get("message") or "")
            if not message.startswith("完成"):
                raise GravityServiceError(message or "轨迹未正常完成")
            return
        time.sleep(0.2)
    raise GravityServiceError(f"等待第{segment_number}段轨迹完成超时")


def _settle_and_sample(
    *,
    segment: dict[str, Any],
    segment_count: int,
    settle_s: float,
    sample_s: float,
    sample_hz: float,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    number = int(segment["sequence"])
    settle_started = time.monotonic()
    while time.monotonic() - settle_started < settle_s:
        if cancel_event.is_set():
            raise GravityServiceError("实验已由操作员停止")
        elapsed = time.monotonic() - settle_started
        _set_operation(
            phase="settling",
            message=(
                f"第{number}/{segment_count}个采样姿态 · "
                f"稳定等待 {max(0.0, settle_s - elapsed):.1f}s"
            ),
            progress=(
                (number - 1)
                + 0.65
                + 0.15 * min(1.0, elapsed / max(settle_s, 1e-6))
            ) / segment_count,
        )
        time.sleep(min(0.1, max(0.0, settle_s - elapsed)))

    samples: list[dict[str, Any]] = []
    sample_started = time.monotonic()
    interval = 1.0 / sample_hz
    while time.monotonic() - sample_started < sample_s:
        if cancel_event.is_set():
            raise GravityServiceError("实验已由操作员停止")
        tick = time.monotonic()
        sample = _request_reach("GET", "/api/reach/diagnostics", timeout=2.0)
        arm = sample.get("arm") or {}
        if not arm.get("armed"):
            raise GravityServiceError("采样期间手臂已释放")
        sample["trajectory_fraction"] = float(segment["fraction"])
        sample["sample_point_index"] = number
        sample["sample_point_type"] = "final" if segment["final"] else "intermediate"
        samples.append(sample)
        elapsed = time.monotonic() - sample_started
        _set_operation(
            phase="sampling",
            message=f"第{number}/{segment_count}个姿态 · 已采样 {len(samples)} 帧",
            progress=(
                (number - 1)
                + 0.8
                + 0.2 * min(1.0, elapsed / max(sample_s, 1e-6))
            ) / segment_count,
        )
        time.sleep(max(0.0, interval - (time.monotonic() - tick)))
    if len(samples) < max(3, int(sample_s * sample_hz * 0.5)):
        raise GravityServiceError(f"第{number}个姿态有效采样不足（仅{len(samples)}帧）")
    return {
        "index": number,
        "type": "final" if segment["final"] else "intermediate",
        "trajectory_fraction": float(segment["fraction"]),
        "planned_named_joints": segment["waypoints"][-1],
        "sample_count": len(samples),
        "samples": samples,
        "aggregate": _aggregate_samples(samples),
        "completed_at": _now(),
    }


def _monitor_and_sample(
    plan: dict[str, Any],
    run: dict[str, Any],
    *,
    settle_s: float,
    sample_s: float,
    sample_hz: float,
    intermediate_stops: int = 0,
    cancel_event: threading.Event,
) -> None:
    try:
        segments = _split_plan_waypoints(plan["waypoints"], intermediate_stops)
        sample_points: list[dict[str, Any]] = []
        total_frames = 0
        for index, segment in enumerate(segments):
            if cancel_event.is_set():
                raise GravityServiceError("实验已由操作员停止")
            segment_duration = max(
                0.2,
                float(plan["duration_s"])
                * (segment["end_index"] - segment["start_index"])
                / (len(plan["waypoints"]) - 1),
            )
            if index > 0:
                result = _request_reach(
                    "POST",
                    "/api/reach/execute",
                    body={
                        "waypoints": segment["waypoints"],
                        "duration": segment_duration,
                        "max_speed_rad_s": plan["max_speed_rad_s"],
                        "label": f"gravity_{plan['point_id'][:8]}_s{index + 1}",
                    },
                    timeout=10.0,
                )
                if not result.get("running"):
                    raise GravityServiceError(
                        str(result.get("message") or f"第{index + 1}段未启动")
                    )
            _wait_for_segment(
                segment_number=index + 1,
                segment_count=len(segments),
                duration_s=segment_duration,
                cancel_event=cancel_event,
            )
            sample_point = _settle_and_sample(
                segment=segment,
                segment_count=len(segments),
                settle_s=settle_s,
                sample_s=sample_s,
                sample_hz=sample_hz,
                cancel_event=cancel_event,
            )
            sample_points.append(sample_point)
            total_frames += int(sample_point["sample_count"])

        completed_at = _now()
        all_samples = [
            sample
            for sample_point in sample_points
            for sample in sample_point["samples"]
        ]
        run.update(
            status="completed",
            completed_at=completed_at,
            sample_count=total_frames,
            samples=all_samples,
            sample_points=sample_points,
            # Keep the top-level aggregate tied to one static pose. Combining
            # different poses would make the mean physically meaningless.
            aggregate=sample_points[-1]["aggregate"],
        )
        _save_run(run)
        _mark_point_completed(plan["point_id"], run["id"], completed_at)
        _set_operation(
            phase="completed",
            message=(
                f"「{plan['point_name']}」完成："
                f"{len(sample_points)}个姿态，共{total_frames}帧"
            ),
            error=None,
            progress=1.0,
        )
    except Exception as exc:
        run.update(status="failed", completed_at=_now(), error=str(exc))
        try:
            _save_run(run)
        except Exception:
            pass
        _set_operation(
            phase="error",
            message=f"实验失败：{exc}",
            error=str(exc),
            progress=0.0,
        )


@app.post("/api/gravity/execute/{point_id}")
def execute_waypoint(point_id: str, body: dict[str, Any]):
    global _run_cancel
    if body.get("confirm") is not True:
        return JSONResponse(
            {"ok": False, "error": "真机执行必须显式传 confirm=true"},
            status_code=400,
        )
    try:
        settle_s = min(20.0, max(0.5, float(body.get("settle_s", 3.0))))
        sample_s = min(20.0, max(0.5, float(body.get("sample_s", 2.0))))
        sample_hz = min(50.0, max(2.0, float(body.get("sample_hz", 10.0))))
        raw_stops = body.get("intermediate_stops")
        intermediate_stops = (
            None if raw_stops is None else min(8, max(0, int(raw_stops)))
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"采样参数非法: {exc}"}, status_code=400)
    with _lock:
        plan = deepcopy(_plan)
        phase = _operation.get("phase")
    if phase in {"executing", "settling", "sampling"}:
        return JSONResponse({"ok": False, "error": "已有实验正在运行"}, status_code=409)
    if plan is None or plan.get("point_id") != point_id:
        return JSONResponse({"ok": False, "error": "请先为该位点重新规划"}, status_code=409)
    if str(body.get("plan_id") or "") != plan.get("id"):
        return JSONResponse({"ok": False, "error": "规划编号已变化，请重新确认"}, status_code=409)
    if plan.get("blocked"):
        return JSONResponse(
            {
                "ok": False,
                "error": str(plan.get("blocked_reason") or "规划存在碰撞，禁止执行"),
            },
            status_code=409,
        )
    try:
        if intermediate_stops is None:
            intermediate_stops = min(8, max(0, int(plan.get("intermediate_stops", 0))))
        segments = _split_plan_waypoints(plan["waypoints"], intermediate_stops)
        first_segment = segments[0]
        first_duration = max(
            0.2,
            float(plan["duration_s"])
            * (first_segment["end_index"] - first_segment["start_index"])
            / (len(plan["waypoints"]) - 1),
        )
        reach = _reach_status()
        if not reach.get("armed"):
            raise GravityServiceError("手臂尚未接管")
        if reach.get("hand_move"):
            raise GravityServiceError("手臂仍在卸力拖动模式，请先恢复刚性保持")
        if (reach.get("exec") or {}).get("running"):
            raise GravityServiceError("18001已有轨迹正在执行")
        # A stop belongs only to the run that was active when it was issued.
        # Give each accepted execution its own event so an old stop cannot
        # reject the next run, while an old monitor retains its cancelled event.
        with _lock:
            cancel_event = threading.Event()
            _run_cancel = cancel_event
        run_id = uuid.uuid4().hex[:12]
        run = {
            "schema_version": 1,
            "id": run_id,
            "point_id": point_id,
            "point_name": plan["point_name"],
            "plan_id": plan["id"],
            "batch_id": body.get("batch_id"),
            "started_at": _now(),
            "status": "running",
            "gravity_profile": deepcopy(reach.get("gravity_profile") or {}),
            "tool_visualization": _tool_visualization(reach),
            "robot": plan.get("robot") or "h2",
            "chain_id": plan.get("chain_id") or "right_arm",
            "target_named_joints": plan["waypoints"][-1],
            "settings": {
                "duration_s": plan["duration_s"],
                "max_speed_rad_s": plan["max_speed_rad_s"],
                "settle_s": settle_s,
                "sample_s": sample_s,
                "sample_hz": sample_hz,
                "intermediate_stops": intermediate_stops,
                "sample_point_count": len(segments),
                "planner": plan.get("planner"),
            },
        }
        result = _request_reach(
            "POST",
            "/api/reach/execute",
            body={
                "waypoints": first_segment["waypoints"],
                "duration": first_duration,
                "max_speed_rad_s": plan["max_speed_rad_s"],
                "label": f"gravity_{point_id[:8]}_s1",
            },
            timeout=10.0,
        )
        _set_operation(
            phase="executing",
            point_id=point_id,
            plan_id=plan["id"],
            run_id=run_id,
            message=str(result.get("message") or "真机轨迹已启动"),
            error=None,
            progress=0.0,
        )
        threading.Thread(
            target=_monitor_and_sample,
            args=(plan, run),
            kwargs={
                "settle_s": settle_s,
                "sample_s": sample_s,
                "sample_hz": sample_hz,
                "intermediate_stops": intermediate_stops,
                "cancel_event": cancel_event,
            },
            daemon=True,
            name=f"gravity-run-{run_id}",
        ).start()
        return {"ok": True, "run_id": run_id, "operation": _operation_snapshot()}
    except GravityServiceError as exc:
        _set_operation(phase="error", message="执行启动失败", error=str(exc), progress=0.0)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)


def _json_response_error(response: JSONResponse) -> str:
    try:
        payload = json.loads(bytes(response.body))
        return str(payload.get("error") or payload.get("detail") or "请求失败")
    except Exception:
        return "请求失败"


def _next_batch_item(batch: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (item for item in batch.get("items") or [] if item.get("status") == "pending"),
        None,
    )


@app.post("/api/gravity/batches")
def create_batch(body: dict[str, Any]):
    global _batch
    raw_ids = body.get("point_ids") or []
    point_ids = list(dict.fromkeys(str(value) for value in raw_ids))
    if not point_ids:
        return JSONResponse({"ok": False, "error": "请至少选择一个实验位点"}, status_code=400)
    try:
        points = [_load_point(point_id) for point_id in point_ids]
        settings = {
            "duration_s": min(30.0, max(2.0, float(body.get("duration_s", 6.0)))),
            "max_speed_rad_s": min(
                0.5, max(0.05, float(body.get("max_speed_rad_s", 0.2)))
            ),
            "settle_s": min(20.0, max(0.5, float(body.get("settle_s", 3.0)))),
            "sample_s": min(20.0, max(0.5, float(body.get("sample_s", 2.0)))),
            "sample_hz": min(50.0, max(2.0, float(body.get("sample_hz", 10.0)))),
            "intermediate_stops": min(
                8, max(0, int(body.get("intermediate_stops", 0)))
            ),
        }
    except (GravityServiceError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    with _lock:
        if _operation.get("phase") in {"executing", "settling", "sampling"}:
            return JSONResponse({"ok": False, "error": "当前实验运行中"}, status_code=409)
        batch = {
            "schema_version": 1,
            "id": uuid.uuid4().hex[:12],
            "name": str(body.get("name") or f"实验批次 {datetime.now():%m-%d %H:%M}"),
            "created_at": _now(),
            "updated_at": _now(),
            "state": "ready",
            "mode": "manual",
            "pause_requested": False,
            "settings": settings,
            "items": [
                {
                    "point_id": point["id"],
                    "point_name": point["name"],
                    "status": "pending",
                    "attempts": 0,
                    "run_id": None,
                    "error": None,
                }
                for point in points
            ],
        }
        _batch = batch
        _save_batch(batch)
    return {"ok": True, "batch": _batch_summary(batch)}


def _run_batch(batch_id: str, *, automatic: bool) -> None:
    global _batch
    while True:
        with _lock:
            if _batch is None or _batch.get("id") != batch_id:
                return
            batch = _batch
            if batch.get("pause_requested"):
                batch["state"] = "paused"
                _save_batch(batch)
                return
            item = _next_batch_item(batch)
            if item is None:
                batch["state"] = "completed"
                _save_batch(batch)
                return
            item["status"] = "planning"
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["error"] = None
            batch["state"] = "running"
            batch["mode"] = "automatic" if automatic else "manual"
            settings = deepcopy(batch["settings"])
            point_id = str(item["point_id"])
            _save_batch(batch)
        try:
            planned = plan_waypoint(
                point_id,
                {
                    "duration_s": settings["duration_s"],
                    "max_speed_rad_s": settings["max_speed_rad_s"],
                    "intermediate_stops": settings["intermediate_stops"],
                    "steps": 120,
                    "planner_type": "linear",
                    "check_collision": True,
                },
            )
            if isinstance(planned, JSONResponse):
                raise GravityServiceError(_json_response_error(planned))
            plan_id = str(planned["plan"]["id"])
            with _lock:
                if _batch is None or _batch.get("id") != batch_id:
                    return
                item["status"] = "executing"
                _save_batch(_batch)
            executed = execute_waypoint(
                point_id,
                {
                    "confirm": True,
                    "plan_id": plan_id,
                    "settle_s": settings["settle_s"],
                    "sample_s": settings["sample_s"],
                    "sample_hz": settings["sample_hz"],
                    "intermediate_stops": settings["intermediate_stops"],
                    "batch_id": batch_id,
                },
            )
            if isinstance(executed, JSONResponse):
                raise GravityServiceError(_json_response_error(executed))
            run_id = str(executed["run_id"])
            while True:
                operation = _operation_snapshot()
                if operation.get("run_id") != run_id:
                    raise GravityServiceError("实验状态被另一项操作替换")
                if operation.get("phase") not in {"executing", "settling", "sampling"}:
                    break
                time.sleep(0.2)
            if operation.get("phase") != "completed":
                raise GravityServiceError(str(operation.get("error") or operation.get("message")))
            with _lock:
                if _batch is None or _batch.get("id") != batch_id:
                    return
                item["status"] = "completed"
                item["run_id"] = run_id
                item["completed_at"] = _now()
                _save_batch(_batch)
        except Exception as exc:
            with _lock:
                if _batch is None or _batch.get("id") != batch_id:
                    return
                item["status"] = "failed"
                item["error"] = str(exc)
                item["run_id"] = _operation.get("run_id")
                _batch["state"] = "paused"
                _batch["pause_requested"] = True
                _save_batch(_batch)
            return
        if not automatic:
            with _lock:
                if _batch is not None and _batch.get("id") == batch_id:
                    _batch["state"] = (
                        "completed" if _next_batch_item(_batch) is None else "ready"
                    )
                    _save_batch(_batch)
            return


def _start_batch_worker(batch_id: str, *, automatic: bool) -> dict[str, Any] | JSONResponse:
    with _lock:
        if _batch is None or _batch.get("id") != batch_id:
            return JSONResponse({"ok": False, "error": "批次不存在或未载入"}, status_code=404)
        if _operation.get("phase") in {"executing", "settling", "sampling", "planning"}:
            return JSONResponse({"ok": False, "error": "当前已有操作正在运行"}, status_code=409)
        if _next_batch_item(_batch) is None:
            return JSONResponse({"ok": False, "error": "批次中没有待执行位点"}, status_code=409)
        _batch["pause_requested"] = False
        _batch["state"] = "running"
        _batch["mode"] = "automatic" if automatic else "manual"
        _save_batch(_batch)
    threading.Thread(
        target=_run_batch,
        args=(batch_id,),
        kwargs={"automatic": automatic},
        daemon=True,
        name=f"gravity-batch-{batch_id}",
    ).start()
    return {"ok": True, "batch": _batch_summary(_batch)}


@app.post("/api/gravity/batches/{batch_id}/next")
def run_batch_next(batch_id: str, body: dict[str, Any]):
    if body.get("confirm") is not True:
        return JSONResponse({"ok": False, "error": "必须确认真机执行安全条件"}, status_code=400)
    return _start_batch_worker(batch_id, automatic=False)


@app.post("/api/gravity/batches/{batch_id}/auto")
def run_batch_auto(batch_id: str, body: dict[str, Any]):
    if body.get("confirm") is not True:
        return JSONResponse({"ok": False, "error": "必须确认自动批量执行安全条件"}, status_code=400)
    return _start_batch_worker(batch_id, automatic=True)


@app.post("/api/gravity/batches/{batch_id}/pause")
def pause_batch(batch_id: str):
    with _lock:
        if _batch is None or _batch.get("id") != batch_id:
            return JSONResponse({"ok": False, "error": "批次不存在或未载入"}, status_code=404)
        _batch["pause_requested"] = True
        if _operation.get("phase") not in {"executing", "settling", "sampling"}:
            _batch["state"] = "paused"
        _save_batch(_batch)
        return {
            "ok": True,
            "message": "将在当前位点完成后暂停；如需立即停机请使用急停",
            "batch": _batch_summary(_batch),
        }


@app.post("/api/gravity/batches/{batch_id}/skip")
def skip_batch_item(batch_id: str):
    with _lock:
        if _batch is None or _batch.get("id") != batch_id:
            return JSONResponse({"ok": False, "error": "批次不存在或未载入"}, status_code=404)
        if _operation.get("phase") in {"executing", "settling", "sampling"}:
            return JSONResponse({"ok": False, "error": "运动或采样中不能跳过，请先急停"}, status_code=409)
        item = next(
            (
                candidate
                for candidate in _batch.get("items") or []
                if candidate.get("status") == "failed"
            ),
            None,
        ) or _next_batch_item(_batch)
        if item is None:
            return JSONResponse({"ok": False, "error": "没有待跳过位点"}, status_code=409)
        item["status"] = "skipped"
        item["completed_at"] = _now()
        _batch["state"] = "ready"
        _save_batch(_batch)
        return {"ok": True, "batch": _batch_summary(_batch)}


@app.post("/api/gravity/batches/{batch_id}/retry")
def retry_batch_item(batch_id: str, body: dict[str, Any]):
    point_id = str(body.get("point_id") or "")
    with _lock:
        if _batch is None or _batch.get("id") != batch_id:
            return JSONResponse({"ok": False, "error": "批次不存在或未载入"}, status_code=404)
        item = next(
            (
                candidate
                for candidate in _batch.get("items") or []
                if candidate.get("point_id") == point_id
                and candidate.get("status") in {"failed", "skipped"}
            ),
            None,
        )
        if item is None:
            return JSONResponse({"ok": False, "error": "该位点当前不可重试"}, status_code=409)
        item.update(status="pending", error=None, run_id=None)
        _batch["state"] = "ready"
        _batch["pause_requested"] = False
        _save_batch(_batch)
        return {"ok": True, "batch": _batch_summary(_batch)}


@app.post("/api/gravity/batches/{batch_id}/resume")
def resume_batch(batch_id: str):
    global _batch
    try:
        batch = json.loads(_batch_path(batch_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "批次记录不存在"}, status_code=404)
    except (GravityServiceError, OSError, json.JSONDecodeError) as exc:
        return JSONResponse({"ok": False, "error": f"批次记录损坏: {exc}"}, status_code=400)
    with _lock:
        if _operation.get("phase") in {"executing", "settling", "sampling"}:
            return JSONResponse({"ok": False, "error": "当前实验运行中"}, status_code=409)
        batch["state"] = "ready"
        batch["pause_requested"] = False
        _batch = batch
        _save_batch(batch)
    return {"ok": True, "batch": _batch_summary(batch)}


@app.get("/api/gravity/runs/{run_id}")
def run_detail(run_id: str):
    try:
        safe = _safe_id(run_id)
    except GravityServiceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    path = _find_run_path(safe)
    if path is None:
        return JSONResponse({"ok": False, "error": "实验记录不存在"}, status_code=404)
    try:
        return {"ok": True, "run": json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError) as exc:
        return JSONResponse({"ok": False, "error": f"实验记录损坏: {exc}"}, status_code=500)


def _find_run_path(run_id: str) -> Path | None:
    legacy = RUNS_DIR / f"{run_id}.json"
    if legacy.is_file():
        return legacy
    matches = list(RUNS_DIR.glob(f"*/{run_id}.json"))
    return matches[0] if matches else None


def _load_run(run_id: str) -> dict[str, Any]:
    path = _find_run_path(_safe_id(run_id))
    if path is None:
        raise GravityServiceError("实验记录不存在")
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GravityServiceError(f"实验记录损坏: {exc}") from exc
    if not isinstance(run, dict):
        raise GravityServiceError("实验记录格式错误")
    return run


def _pose_comparison(run: dict[str, Any], sample_index: int) -> dict[str, Any]:
    sample_points = run.get("sample_points") or []
    if not sample_points:
        raise GravityServiceError("该实验没有分姿态采样数据")
    selected = next(
        (point for point in sample_points if int(point.get("index", -1)) == sample_index),
        None,
    )
    if selected is None:
        raise GravityServiceError(f"采样姿态 {sample_index} 不存在")
    aggregate = selected.get("aggregate") or {}
    command = (aggregate.get("command_rad") or {}).get("mean")
    measured = (aggregate.get("measured_rad") or {}).get("mean")
    samples = selected.get("samples") or []
    arm = (samples[0].get("arm") or {}) if samples else {}
    names = list(arm.get("joint_names") or [])
    if not names:
        names = list((selected.get("planned_named_joints") or {}).keys())
    if not command or not measured or len(names) != len(command) or len(names) != len(measured):
        raise GravityServiceError("该采样姿态缺少完整的命令/实测关节数据")

    import app as app_module

    robot_id = str(run.get("robot") or "h2")
    chain_id = str(run.get("chain_id") or "right_arm")
    if robot_id not in app_module.robots:
        raise GravityServiceError(f"找不到机器人模型 {robot_id}")
    model = app_module.robots[robot_id]
    chain = model.chain_config(chain_id)
    command_named = dict(zip(names, [float(value) for value in command]))
    measured_named = dict(zip(names, [float(value) for value in measured]))
    display_links = list(chain.display_links)
    if not display_links:
        display_links = [chain.base_link] + [
            model.joints[name].child for name in model.joint_names(chain_id)
        ]
    command_fk = model.forward_kinematics(command_named, only_links=display_links)
    measured_fk = model.forward_kinematics(measured_named, only_links=display_links)

    def links(transforms: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "xyz": [float(value) for value in transforms[name][:3, 3]],
            }
            for name in display_links
            if name in transforms
        ]

    command_tcp = (aggregate.get("tcp_command_root_m") or {}).get("mean")
    measured_tcp = (aggregate.get("tcp_measured_root_m") or {}).get("mean")
    if not command_tcp:
        command_tcp = model.tcp_pose(
            command_named, chain_id, model.tcp_offset(chain_id)
        ).xyz
    if not measured_tcp:
        measured_tcp = model.tcp_pose(
            measured_named, chain_id, model.tcp_offset(chain_id)
        ).xyz
    command_tcp = [float(value) for value in command_tcp]
    measured_tcp = [float(value) for value in measured_tcp]
    tcp_delta_mm = [
        (measured_value - command_value) * 1000.0
        for command_value, measured_value in zip(command_tcp, measured_tcp)
    ]
    joint_error_deg = [
        math.degrees(command_value - measured_value)
        for command_value, measured_value in zip(command, measured)
    ]
    return {
        "run_id": run["id"],
        "point_name": run.get("point_name"),
        "gravity_version": (run.get("gravity_profile") or {}).get("version"),
        "robot": robot_id,
        "chain_id": chain_id,
        "sample_index": sample_index,
        "sample_type": selected.get("type"),
        "trajectory_fraction": selected.get("trajectory_fraction"),
        "tool_visualization": deepcopy(run.get("tool_visualization") or {}),
        "available_samples": [
            {
                "index": int(point.get("index", index + 1)),
                "type": point.get("type"),
                "trajectory_fraction": point.get("trajectory_fraction"),
                "sample_count": point.get("sample_count"),
            }
            for index, point in enumerate(sample_points)
        ],
        "joint_names": names,
        "joint_error_deg": joint_error_deg,
        "theoretical": {
            "named_joints": command_named,
            "links": links(command_fk),
            "tcp_root_m": command_tcp,
        },
        "measured": {
            "named_joints": measured_named,
            "links": links(measured_fk),
            "tcp_root_m": measured_tcp,
        },
        "tcp_delta_mm": tcp_delta_mm,
        "tcp_error_mm": float(np.linalg.norm(tcp_delta_mm)),
    }


def _ik_validation_comparison(
    validation: dict[str, Any],
) -> dict[str, Any]:
    theoretical = _finite_vector(
        validation.get("theoretical_rad"), "IK理论关节值"
    )
    aggregate = validation.get("aggregate") or {}
    measured = _finite_vector(
        (aggregate.get("measured_rad") or {}).get("mean"),
        "实测关节均值",
    )
    names = list(validation.get("joint_names") or [])
    if (
        not names
        or len(names) != len(theoretical)
        or len(names) != len(measured)
    ):
        raise GravityServiceError("IK验证缺少完整的关节名称或关节数据")

    import app as app_module

    robot_id = str(validation.get("robot") or "h2")
    chain_id = str(validation.get("chain_id") or "right_arm")
    if robot_id not in app_module.robots:
        raise GravityServiceError(f"找不到机器人模型 {robot_id}")
    model = app_module.robots[robot_id]
    chain = model.chain_config(chain_id)
    theoretical_named = dict(zip(names, theoretical))
    measured_named = dict(zip(names, measured))
    display_links = list(chain.display_links)
    if not display_links:
        display_links = [chain.base_link] + [
            model.joints[name].child for name in model.joint_names(chain_id)
        ]
    theoretical_fk = model.forward_kinematics(
        theoretical_named, only_links=display_links
    )
    measured_fk = model.forward_kinematics(
        measured_named, only_links=display_links
    )

    def links(transforms: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "xyz": [float(value) for value in transforms[name][:3, 3]],
            }
            for name in display_links
            if name in transforms
        ]

    metrics = validation.get("metrics") or {}
    tracking = metrics.get("tracking") or {}
    tcp_delta = _finite_vector(
        tracking.get("delta_mm"), "TCP跟踪误差"
    )
    joint_error_deg = [
        math.degrees(theory - actual)
        for theory, actual in zip(theoretical, measured)
    ]
    return {
        "run_id": validation["id"],
        "point_name": (
            f"IK验证 · {validation.get('start_label') or '未标注起点'}"
        ),
        "gravity_version": (
            validation.get("gravity_profile") or {}
        ).get("version"),
        "robot": robot_id,
        "chain_id": chain_id,
        "sample_index": 1,
        "sample_type": "ik_validation",
        "trajectory_fraction": 1.0,
        "tool_visualization": deepcopy(
            validation.get("tool_visualization") or {}
        ),
        "available_samples": [
            {
                "index": 1,
                "type": "ik_validation",
                "trajectory_fraction": 1.0,
                "sample_count": validation.get("sample_count"),
            }
        ],
        "joint_names": names,
        "joint_error_deg": joint_error_deg,
        "theoretical": {
            "named_joints": theoretical_named,
            "links": links(theoretical_fk),
            "tcp_root_m": _finite_vector(
                (metrics.get("planned_root_m")), "IK理论TCP"
            ),
        },
        "measured": {
            "named_joints": measured_named,
            "links": links(measured_fk),
            "tcp_root_m": _finite_vector(
                (metrics.get("measured_root_m")), "实测TCP"
            ),
        },
        "tcp_delta_mm": tcp_delta,
        "tcp_error_mm": float(
            tracking.get("norm_mm") or np.linalg.norm(tcp_delta)
        ),
        "error_breakdown": deepcopy(metrics),
        "validation_kind": "pointcloud_ik",
        "start_label": validation.get("start_label"),
        "execution_id": validation.get("execution_id"),
        "sample_count": validation.get("sample_count"),
    }


@app.get("/api/gravity/ik_validation/{validation_id}/comparison")
def ik_validation_comparison(validation_id: str):
    try:
        validation = _load_ik_validation(validation_id)
        comparison = _ik_validation_comparison(validation)
        if not (comparison.get("tool_visualization") or {}).get("tcp_offset"):
            try:
                comparison["tool_visualization"] = _tool_visualization(
                    _reach_status()
                )
            except GravityServiceError:
                pass
        return {"ok": True, "comparison": comparison}
    except (GravityServiceError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )


@app.get("/api/gravity/ik_validation/{validation_id}")
def ik_validation_detail(validation_id: str):
    try:
        return {
            "ok": True,
            "validation": _load_ik_validation(validation_id),
        }
    except GravityServiceError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=404,
        )


@app.get("/api/gravity/runs/{run_id}/comparison")
def run_comparison(run_id: str, sample_index: int = 1):
    try:
        run = _load_run(run_id)
        comparison = _pose_comparison(run, int(sample_index))
        if not (comparison.get("tool_visualization") or {}).get("tcp_offset"):
            try:
                comparison["tool_visualization"] = _tool_visualization(_reach_status())
            except GravityServiceError:
                pass
        return {
            "ok": True,
            "comparison": comparison,
        }
    except (GravityServiceError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _lan_ip() -> str:
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> None:
    global _reach_base
    import uvicorn

    parser = argparse.ArgumentParser(description="重力补偿标定位点与实验运行台")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18002)
    parser.add_argument("--reach-base", default="http://127.0.0.1:18001")
    args = parser.parse_args()
    _reach_base = args.reach_base.rstrip("/")
    WAYPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[gravity] 18001来源: {_reach_base}")
    print(f"[gravity] 数据目录: {DATA_ROOT}")
    print(f"[gravity] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
