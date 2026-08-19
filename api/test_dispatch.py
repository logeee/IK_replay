"""17001 API 联调服务：收到拨闸指令后执行一个固定关节路点。

本模块只用于外部接口和真机动作链联调，不执行站位检查、YOLO 或拨闸流程。
接口形状保持与生产 dispatch 一致：

    POST /check/flip
    POST /task/flip
    GET  /task/status
    POST /task/abort
    POST /emergency/stop
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .client import ReachClient


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WAYPOINT = ROOT / "data" / "waypoints" / "起手点测试_20260721_042250.json"
DEFAULT_CALIB = (
    Path("/home/robot/yx/project/calib/hand_eye_3D")
    / "handeye3d_data"
    / "biaoding"
    / "handeye3d_result.json"
)

LANGUAGE_TASKS = {
    "change the switch from close to remote": "close_to_remote",
    "change the switch from remote to close": "remote_to_close",
}
SUPPORTED_LANGUAGES = [
    "Change the switch from close to remote",
    "Change the switch from remote to close",
]

app = FastAPI(title="flip-api-test")

_args: argparse.Namespace | None = None
_task: dict[str, Any] | None = None
_lock = threading.RLock()
_abort = threading.Event()
_http = requests.Session()
_http.trust_env = False


def _parse_language(text: str) -> str | None:
    normalized = " ".join(text.lower().replace(".", " ").split())
    return LANGUAGE_TASKS.get(normalized)


def _language_or_error(body: dict | None) -> tuple[str, str] | JSONResponse:
    language = str((body or {}).get("language") or "").strip()
    if not language:
        return JSONResponse(
            {"ok": False, "error": "缺少必填字段 language",
             "supported": SUPPORTED_LANGUAGES},
            status_code=422,
        )
    kind = _parse_language(language)
    if kind is None:
        return JSONResponse(
            {"ok": False, "error": f"无法识别的指令: {language!r}",
             "supported": SUPPORTED_LANGUAGES},
            status_code=422,
        )
    return language, kind


def _reach_alive(timeout_s: float = 2.0) -> bool:
    if _args is None:
        return False
    try:
        response = _http.get(
            f"{_args.reach_base}/api/reach/status", timeout=timeout_s
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def _spawn_reach(task: dict[str, Any]) -> None:
    assert _args is not None
    log_dir = ROOT / "logs" / "reach"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"test_reach_{datetime.now():%Y%m%d_%H%M%S}.log"
    cmd = [
        sys.executable,
        str(ROOT / "reach_server.py"),
        "--robot-only",
        "--port",
        str(_args.reach_port),
        "--network-interface",
        _args.network_interface,
        "--calib",
        str(_args.calib),
        "--tool-out-mm",
        str(_args.tool_out_mm),
    ]
    task["log"].append(f"启动 robot-only reach_server: {' '.join(cmd[1:])}")
    task["reach_log"] = str(log_path)
    task["reach_proc"] = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=log_path.open("ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _wait_reach_ready(task: dict[str, Any]) -> None:
    assert _args is not None
    proc: subprocess.Popen = task["reach_proc"]
    deadline = time.monotonic() + _args.reach_timeout
    while time.monotonic() < deadline:
        if _abort.is_set():
            raise RuntimeError("收到停止指令")
        if proc.poll() is not None:
            tail = ""
            try:
                tail = Path(task["reach_log"]).read_text(errors="replace")[-1000:]
            except OSError:
                pass
            raise RuntimeError(
                f"reach_server 启动即退出（exit={proc.returncode}）。日志尾：\n{tail}"
            )
        if _reach_alive():
            task["log"].append("robot-only reach_server 就绪")
            return
        time.sleep(0.5)
    raise RuntimeError(f"reach_server {_args.reach_timeout:.0f}s 内未就绪")


def _stop_reach(task: dict[str, Any]) -> None:
    proc: subprocess.Popen | None = task.get("reach_proc")
    if proc is None or proc.poll() is not None:
        task["reach_proc"] = None
        return
    task["log"].append("关闭本测试启动的 reach_server…")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15.0)
        task["log"].append("reach_server 已退出")
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3.0)
        task["log"].append("⚠ reach_server 15s 未退出，已强制终止")
    finally:
        task["reach_proc"] = None


def _load_target(path: Path) -> tuple[str, dict[str, float]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"读取测试路点失败: {path}: {exc}") from exc
    if data.get("chain_id") != "right_arm":
        raise RuntimeError(
            f"测试路点 chain_id 必须是 right_arm，实际为 {data.get('chain_id')!r}"
        )
    raw = data.get("named_joints")
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError("测试路点缺少 named_joints")
    try:
        target = {str(name): float(value) for name, value in raw.items()}
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"测试路点关节数据非法: {exc}") from exc
    return str(data.get("name") or path.stem), target


def _wait_execution(client: ReachClient, task: dict[str, Any]) -> None:
    assert _args is not None
    deadline = time.monotonic() + _args.exec_timeout
    while time.monotonic() < deadline:
        if _abort.is_set():
            raise RuntimeError("收到停止指令")
        status = client.exec_status()
        if not status.get("running"):
            message = str(status.get("message") or "")
            if message.startswith("完成"):
                task["log"].append(f"关节插值完成：{message}")
                return
            raise RuntimeError(f"关节插值未正常完成: {message or status}")
        time.sleep(0.25)
    raise RuntimeError(f"关节插值 {_args.exec_timeout:.0f}s 内未完成")


def _stop_and_release(client: ReachClient, task: dict[str, Any]) -> list[str]:
    """测试取得控制权后，无论成功失败都尽力停轨迹并释放。"""
    errors: list[str] = []
    if not task.get("owns_arm"):
        return errors
    released = False
    try:
        status = client.exec_status()
        if status.get("running"):
            client.stop()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not client.exec_status().get("running"):
                    break
                time.sleep(0.2)
    except Exception as exc:
        errors.append(f"停止轨迹失败: {exc}")
    try:
        result = client.disarm()
        if not result.get("ok"):
            errors.append(f"释放手臂失败: {result.get('error') or result}")
        else:
            task["log"].append("手臂已释放，控制权交还本体")
            released = True
    except Exception as exc:
        errors.append(f"释放手臂失败: {exc}")
    finally:
        task["owns_arm"] = not released
    return errors


def _run_task(task: dict[str, Any]) -> None:
    assert _args is not None
    client = ReachClient(_args.reach_base, timeout_s=10.0)
    cleanup_errors: list[str] = []
    try:
        if _reach_alive():
            task["reach_external"] = True
            task["log"].append("复用已运行的 reach_server，任务结束后不关闭")
        else:
            task["reach_external"] = False
            _spawn_reach(task)
            _wait_reach_ready(task)

        task["state"] = "running"
        status = client.status()
        if not status.get("enabled") or not status.get("arm_supported"):
            raise RuntimeError("reach_server 没有真机手臂控制能力")
        if status.get("armed"):
            raise RuntimeError("手臂已被其他流程接管；测试拒绝复用未知控制权")

        name, target = _load_target(_args.waypoint)
        joints = client.joints()
        if not joints.get("ok"):
            raise RuntimeError(f"读取真机关节失败: {joints.get('error')}")
        current = joints["named_joints"]
        missing = [joint for joint in target if joint not in current]
        if missing:
            raise RuntimeError(f"真机关节数据缺少: {missing}")

        task["log"].append("接管右臂并在当前姿态保持")
        # 预检查已确认未接管；即使 arm 请求响应丢失，也要在 finally 尝试释放。
        task["owns_arm"] = True
        armed = client.arm()
        if not armed.get("ok"):
            raise RuntimeError(f"接管手臂失败: {armed.get('error')}")

        travel = max(abs(target[joint] - float(current[joint])) for joint in target)
        duration = max(1.5, travel / _args.max_speed)
        task["log"].append(
            f"关节线性插值到「{name}」：最大关节差 {travel:.3f} rad，"
            f"计划时长 {duration:.2f}s"
        )
        executed = client.execute(
            waypoints=[current, target],
            duration=duration,
            max_speed_rad_s=_args.max_speed,
            label="api_test_goto",
        )
        if not executed.get("ok"):
            raise RuntimeError(f"关节插值被拒绝: {executed.get('error')}")
        _wait_execution(client, task)

        task["log"].append(f"到位后保持 {_args.hold_seconds:g}s")
        hold_deadline = time.monotonic() + _args.hold_seconds
        while time.monotonic() < hold_deadline:
            if _abort.is_set():
                raise RuntimeError("保持期间收到停止指令")
            time.sleep(min(0.1, max(hold_deadline - time.monotonic(), 0.0)))

        task["result"] = {
            "ok": True,
            "code": 0,
            "code_name": "OK",
            "message": f"已到达「{name}」，保持 {_args.hold_seconds:g}s 后释放手臂",
            "detail": {
                "waypoint": str(_args.waypoint),
                "max_joint_travel_rad": round(travel, 4),
                "motion_duration_s": round(duration, 2),
                "hold_seconds": _args.hold_seconds,
            },
        }
    except Exception as exc:
        task["log"].append(f"✘ API 联调动作失败: {exc}")
        task["result"] = {
            "ok": False,
            "code": -1,
            "code_name": "TEST_FAILED",
            "message": str(exc),
            "detail": {},
        }
    finally:
        cleanup_errors.extend(_stop_and_release(client, task))
        for error in cleanup_errors:
            task["log"].append(f"⚠ {error}")
        if cleanup_errors and task.get("result", {}).get("ok"):
            task["result"] = {
                "ok": False,
                "code": -1,
                "code_name": "RELEASE_FAILED",
                "message": "动作完成，但手臂释放失败",
                "detail": {"errors": cleanup_errors},
            }
        if not task.get("reach_external"):
            try:
                _stop_reach(task)
            except Exception as exc:
                task["log"].append(f"⚠ 关闭 reach_server 失败: {exc}")
        task["state"] = "done"
        task["finished_at"] = datetime.now().isoformat(timespec="seconds")


@app.post("/check/flip")
def check_flip(body: dict | None = None):
    parsed = _language_or_error(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    language, _ = parsed
    with _lock:
        if _task is not None and _task["state"] != "done":
            return JSONResponse(
                {"ok": False, "error": "API 联调任务执行中",
                 "task_id": _task["id"], "state": _task["state"]},
                status_code=409,
            )
    return {
        "ok": True,
        "passed": True,
        "need_flip": True,
        "failed_step": None,
        "message": "API 联调模式：跳过现场站位检查，可以调用 /task/flip",
        "steps": [
            {
                "step": 1,
                "name": "API 联通检查",
                "passed": True,
                "message": f"已识别指令：{language}",
            }
        ],
        "camera_kept": False,
        "duration_s": 0.0,
        "log": ["测试模式不执行距离、朝向、站姿和 YOLO 检查"],
        "test_mode": True,
    }


@app.post("/task/flip")
def task_submit(body: dict | None = None):
    global _task
    parsed = _language_or_error(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    language, kind = parsed
    try:
        retries = int((body or {}).get("retries") or 3)
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "retries 必须是整数"}, status_code=422
        )
    if not 1 <= retries <= 20:
        return JSONResponse(
            {"ok": False, "error": "retries 取值范围 1~20"}, status_code=422
        )
    with _lock:
        if _task is not None and _task["state"] != "done":
            return JSONResponse(
                {"ok": False, "error": "已有任务在执行",
                 "task_id": _task["id"], "state": _task["state"]},
                status_code=409,
            )
        _abort.clear()
        now = datetime.now().isoformat(timespec="seconds")
        _task = {
            "id": uuid.uuid4().hex[:10],
            "state": "starting",
            "language": language,
            "kind": kind,
            "retries": retries,
            "started_at": now,
            "finished_at": None,
            "result": None,
            "log": [f"API 联调指令: {language}"],
            "reach_proc": None,
            "reach_external": False,
            "owns_arm": False,
        }
        if kind == "remote_to_close":
            _task["state"] = "done"
            _task["finished_at"] = now
            _task["result"] = {
                "ok": False,
                "code": 1,
                "code_name": "NOT_IMPLEMENTED",
                "message": "「远方 → 就地」暂未支持",
                "detail": {},
            }
            return {"ok": True, "task_id": _task["id"]}
        threading.Thread(target=_run_task, args=(_task,), daemon=True).start()
        return {"ok": True, "task_id": _task["id"]}


@app.get("/task/status")
def task_status():
    with _lock:
        task = _task
    if task is None:
        return {
            "ok": True,
            "state": "idle",
            "task_id": None,
            "reach_alive": _reach_alive(0.5),
            "test_mode": True,
        }
    return {
        "ok": True,
        "state": task["state"],
        "task_id": task["id"],
        "language": task.get("language"),
        "retries": task.get("retries"),
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
        "result": task["result"],
        "log": list(task["log"])[-60:],
        "test_mode": True,
    }


def _emergency_stop(reason: str) -> dict:
    _abort.set()
    with _lock:
        task = _task
    actions: list[str] = []
    if task is not None and task.get("owns_arm") and _args is not None:
        client = ReachClient(_args.reach_base, timeout_s=5.0)
        errors = _stop_and_release(client, task)
        actions.append("急停轨迹并释放手臂")
        actions.extend(errors)
    else:
        actions.append("当前测试没有持有手臂控制权")
    if task is not None and not task.get("reach_external"):
        try:
            _stop_reach(task)
            actions.append("关闭本测试启动的 reach_server")
        except Exception as exc:
            actions.append(f"关闭 reach_server 失败: {exc}")
    return {
        "ok": True,
        "reason": reason,
        "actions": actions,
        "arm_released": not bool(task and task.get("owns_arm")),
        "task_state": task["state"] if task else "idle",
        "test_mode": True,
    }


@app.post("/emergency/stop")
def emergency_stop(body: dict | None = None):
    reason = str((body or {}).get("reason") or "外部强制停止")
    return _emergency_stop(reason)


@app.post("/task/abort")
def task_abort():
    return _emergency_stop("task/abort")


@app.get("/")
def index():
    return {
        "service": "flip-api-test",
        "test_mode": True,
        "warning": "调用 /task/flip 会接管并真实移动右臂",
        "usage": {
            "check": 'POST /check/flip body={"language": "..."}',
            "start": 'POST /task/flip body={"language": "..."}',
            "status": "GET /task/status",
            "abort": "POST /task/abort",
        },
    }


@app.on_event("shutdown")
def shutdown_cleanup() -> None:
    result = _emergency_stop("测试 API 服务退出")
    print(f"[test-dispatch] 退出清理: {result['actions']}")


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
    global _args
    import uvicorn

    parser = argparse.ArgumentParser(description="拨闸 API 真机联调服务（17001）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=17001)
    parser.add_argument("--reach-base", default="http://127.0.0.1:18001")
    parser.add_argument("--reach-port", type=int, default=18001)
    parser.add_argument("--network-interface", default="enp86s0")
    parser.add_argument("--calib", type=Path, default=DEFAULT_CALIB)
    parser.add_argument("--tool-out-mm", type=float, default=15.0)
    parser.add_argument("--waypoint", type=Path, default=DEFAULT_WAYPOINT)
    parser.add_argument("--max-speed", type=float, default=0.2)
    parser.add_argument("--hold-seconds", type=float, default=2.0)
    parser.add_argument("--reach-timeout", type=float, default=30.0)
    parser.add_argument("--exec-timeout", type=float, default=120.0)
    _args = parser.parse_args()
    _args.reach_base = _args.reach_base.rstrip("/")
    if not 0.05 <= _args.max_speed <= 0.5:
        parser.error("--max-speed 必须在 0.05~0.5 rad/s")
    if _args.hold_seconds < 0:
        parser.error("--hold-seconds 不能为负数")
    if not _args.waypoint.is_file():
        parser.error(f"测试路点不存在: {_args.waypoint}")
    if not _args.calib.is_file():
        parser.error(f"标定文件不存在: {_args.calib}")

    print(f"[test-dispatch] API 联调服务: http://{_lan_ip()}:{_args.port}/")
    print(f"[test-dispatch] 真机测试路点: {_args.waypoint}")
    print("[test-dispatch] ⚠ POST /task/flip 会接管并真实移动右臂")
    uvicorn.run(app, host=_args.host, port=_args.port, log_level="warning")


if __name__ == "__main__":
    main()
