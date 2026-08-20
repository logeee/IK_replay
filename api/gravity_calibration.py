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
import statistics
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_ROOT = ROOT / "data" / "gravity_calibration"
WAYPOINTS_DIR = DATA_ROOT / "waypoints"
RUNS_DIR = DATA_ROOT / "runs"
BATCHES_DIR = DATA_ROOT / "batches"

app = FastAPI(title="gravity-calibration")

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


def _list_runs(*, include_samples: bool = False) -> list[dict[str, Any]]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for path in RUNS_DIR.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                continue
            if not include_samples:
                value = {k: v for k, v in value.items() if k != "samples"}
            runs.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(runs, key=lambda item: str(item.get("started_at", "")), reverse=True)


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


@app.get("/")
def page():
    return FileResponse(WEB_DIR / "gravity.html")


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
        "points": points,
        "runs": runs[:30],
        "summary": {
            "points": len(points),
            "completed_points": len(completed_ids),
            "completed_runs": sum(run.get("status") == "completed" for run in runs),
            "failed_runs": sum(run.get("status") == "failed" for run in runs),
        },
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
        _run_cancel.clear()
        result = _request_reach(
            "POST", "/api/reach/hand_move", body={"on": bool(body.get("on"))}
        )
        return {"ok": True, "reach": result}
    except GravityServiceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.post("/api/gravity/stop")
def stop_execution():
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
        if isinstance(collision, dict) and collision.get("status") == "collision":
            detail = collision.get("rrt_error") or "规划轨迹存在碰撞"
            raise GravityServiceError(str(detail))
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
            "created_at": _now(),
            "duration_s": duration,
            "max_speed_rad_s": max_speed,
            "intermediate_stops": intermediate_stops,
            "planner": planned.get("planner"),
            "waypoint_count": len(waypoints),
            "collision": collision,
            "waypoints": waypoints,
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


def _save_run(run: dict[str, Any]) -> None:
    _atomic_json(RUNS_DIR / f"{run['id']}.json", run)


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
) -> None:
    deadline = time.monotonic() + duration_s + 40.0
    while time.monotonic() < deadline:
        if _run_cancel.is_set():
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
) -> dict[str, Any]:
    number = int(segment["sequence"])
    settle_started = time.monotonic()
    while time.monotonic() - settle_started < settle_s:
        if _run_cancel.is_set():
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
        if _run_cancel.is_set():
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
) -> None:
    try:
        segments = _split_plan_waypoints(plan["waypoints"], intermediate_stops)
        sample_points: list[dict[str, Any]] = []
        total_frames = 0
        for index, segment in enumerate(segments):
            if _run_cancel.is_set():
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
            )
            sample_point = _settle_and_sample(
                segment=segment,
                segment_count=len(segments),
                settle_s=settle_s,
                sample_s=sample_s,
                sample_hz=sample_hz,
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
    path = RUNS_DIR / f"{safe}.json"
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "实验记录不存在"}, status_code=404)
    try:
        return {"ok": True, "run": json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError) as exc:
        return JSONResponse({"ok": False, "error": f"实验记录损坏: {exc}"}, status_code=500)


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
