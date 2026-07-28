"""17001 任务调度服务——外部系统触发拨闸流程的唯一入口。

部署形态（谁常驻、谁按需）：
    · 本服务 17001：常驻。外部系统（导航栈把机器人开到电柜前后）只需
      POST /task/flip，然后轮询 GET /task/status 拿结果。
    · yolo_server 7004 / console 7002：常驻（不占相机）。
    · reach_server 8001：独占相机，平时关着。本服务收到任务时子进程拉起，
      任务结束（无论成败）后 SIGINT 优雅关掉释放相机——reach_server 自己
      会在退出时释放手臂、停相机。

若收到任务时 8001 已经在跑（比如你手动开着调试），直接复用，任务结束
后也不关它——谁启动的谁负责关。

启动（fastapi 环境）：
    /home/robot/miniconda3/envs/fastapi/bin/python -m api.dispatch

外部对接：
    POST /task/flip    → {"ok": true, "task_id": "..."}；已有任务在跑 → 409
    GET  /task/status  → 状态机 idle/starting/running/done + 流程日志尾部
                          + 最终结果（错误码见 api.flow.ErrorCode）
    POST /task/abort   → 急停正在执行的动作并强制结束任务
"""

from __future__ import annotations

import argparse
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
from .console_client import ConsoleClient
from .flow import SwitchFlow
from .yolo_client import YoloClient

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="flip-dispatch")

_http = requests.Session()
_http.trust_env = False   # 只连本机服务，不走系统代理

_args: argparse.Namespace | None = None
_lock = threading.Lock()
_task: dict[str, Any] | None = None   # 当前/最近一次任务


# ------------------------------------------------------------ reach 生命周期


def _reach_alive(timeout_s: float = 2.0) -> bool:
    try:
        r = _http.get(f"{_args.reach_base}/api/reach/status", timeout=timeout_s)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _spawn_reach(task: dict) -> None:
    """子进程拉起 reach_server，输出落到日志文件。"""
    log_dir = ROOT / "reach_logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"dispatch_reach_{datetime.now():%Y%m%d_%H%M%S}.log"
    cmd = [sys.executable, str(ROOT / "reach_server.py"),
           "--camera-serial", _args.camera_serial,
           "--network-interface", _args.network_interface]
    task["log"].append(f"启动 reach_server: {' '.join(cmd[1:])}")
    task["reach_log"] = str(log_path)
    task["reach_proc"] = subprocess.Popen(
        cmd, cwd=ROOT, stdout=log_path.open("ab"),
        stderr=subprocess.STDOUT, start_new_session=True)


def _wait_reach_ready(task: dict, timeout_s: float = 45.0) -> None:
    """等相机+DDS 初始化完、HTTP 可用；子进程提前退出则带日志尾报错。"""
    proc: subprocess.Popen = task["reach_proc"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = ""
            try:
                tail = Path(task["reach_log"]).read_text(errors="replace")[-800:]
            except OSError:
                pass
            raise RuntimeError(
                f"reach_server 启动即退出（exit={proc.returncode}）。日志尾：\n{tail}")
        if _reach_alive():
            task["log"].append("reach_server 就绪")
            time.sleep(1.0)   # 状态接口通了再缓一拍，等各线程稳定
            return
        time.sleep(0.5)
    raise RuntimeError(f"reach_server {timeout_s:.0f}s 内未就绪")


def _stop_reach(task: dict) -> None:
    """SIGINT 优雅关停（reach_server 退出时自己释放手臂/相机），拖住兜底 kill。"""
    proc: subprocess.Popen | None = task.get("reach_proc")
    if proc is None or proc.poll() is not None:
        return
    task["log"].append("关闭 reach_server 释放相机…")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15.0)
        task["log"].append("reach_server 已退出")
    except subprocess.TimeoutExpired:
        proc.kill()
        task["log"].append("⚠ reach_server 15s 未退出，已强杀")


# ------------------------------------------------------------------ 任务执行


def _run_task(task: dict) -> None:
    try:
        if _reach_alive():
            task["reach_external"] = True
            task["log"].append("reach_server 已在运行（外部启动），复用且任务后不关闭")
        else:
            task["reach_external"] = False
            task["state"] = "starting"
            _spawn_reach(task)
            _wait_reach_ready(task)

        console = None
        if not _args.no_console:
            console = ConsoleClient(_args.console)
            if not console.alive():
                task["log"].append(f"⚠ 确认台不可达（{_args.console}），"
                                   f"本次任务不带人工兜底")
                console = None
        yolo = None if _args.no_yolo else YoloClient(_args.yolo)

        flow = SwitchFlow(client=ReachClient(_args.reach_base),
                          console=console, yolo=yolo)
        task["flow"] = flow
        task["state"] = "running"
        result = flow.run()
        task["result"] = {"ok": result.ok, "code": int(result.code),
                          "code_name": result.code.name,
                          "message": result.message, "detail": result.detail}
    except Exception as exc:   # 调度层自身的失败（拉不起 reach 等）
        task["log"].append(f"✘ 调度失败: {exc}")
        task["result"] = {"ok": False, "code": -1, "code_name": "DISPATCH_ERROR",
                          "message": str(exc), "detail": {}}
    finally:
        if not task.get("reach_external"):
            try:
                _stop_reach(task)
            except Exception as exc:
                task["log"].append(f"⚠ 关闭 reach_server 出错: {exc}")
        task["state"] = "done"
        task["finished_at"] = datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------- 接口


@app.post("/task/flip")
def task_flip():
    global _task
    with _lock:
        if _task is not None and _task["state"] != "done":
            return JSONResponse(
                {"ok": False, "error": "已有任务在执行",
                 "task_id": _task["id"], "state": _task["state"]},
                status_code=409)
        _task = {"id": uuid.uuid4().hex[:10], "state": "starting",
                 "started_at": datetime.now().isoformat(timespec="seconds"),
                 "finished_at": None, "result": None, "flow": None,
                 "log": [], "reach_proc": None, "reach_external": False}
        threading.Thread(target=_run_task, args=(_task,), daemon=True).start()
        return {"ok": True, "task_id": _task["id"]}


@app.get("/task/status")
def task_status():
    with _lock:
        t = _task
    if t is None:
        return {"ok": True, "state": "idle", "task_id": None,
                "reach_alive": _reach_alive(0.5)}
    flow: SwitchFlow | None = t.get("flow")
    log = list(t["log"]) + (list(flow.log_lines) if flow is not None else [])
    return {"ok": True, "state": t["state"], "task_id": t["id"],
            "started_at": t["started_at"], "finished_at": t["finished_at"],
            "result": t["result"], "log": log[-60:]}


@app.post("/task/abort")
def task_abort():
    with _lock:
        t = _task
    if t is None or t["state"] == "done":
        return JSONResponse({"ok": False, "error": "没有正在执行的任务"},
                            status_code=409)
    t["log"].append("收到外部中止请求")
    # 先急停正在执行的动作，再关掉 reach（流程的 HTTP 调用会随之失败退出）
    try:
        _http.post(f"{_args.reach_base}/api/reach/stop", timeout=3.0)
    except requests.RequestException:
        pass
    if not t.get("reach_external"):
        try:
            _stop_reach(t)
        except Exception as exc:
            t["log"].append(f"⚠ 中止时关闭 reach_server 出错: {exc}")
    return {"ok": True, "message": "已急停并开始收尾，稍后轮询 /task/status"}


@app.get("/")
def index():
    return {"service": "flip-dispatch",
            "usage": {"start": "POST /task/flip",
                      "status": "GET /task/status",
                      "abort": "POST /task/abort"}}


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> None:
    global _args
    import uvicorn

    parser = argparse.ArgumentParser(description="拨闸任务调度服务（17001，常驻）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=17001)
    parser.add_argument("--reach-base", default="http://127.0.0.1:8001")
    parser.add_argument("--camera-serial", default="CP0BB53000FS")
    parser.add_argument("--network-interface", default="enp86s0")
    parser.add_argument("--console", default="http://127.0.0.1:7002",
                        help="人工确认台地址（不可达时自动不带兜底）")
    parser.add_argument("--no-console", action="store_true")
    parser.add_argument("--yolo", default="http://127.0.0.1:7004",
                        help="YOLO 推理服务地址")
    parser.add_argument("--no-yolo", action="store_true")
    _args = parser.parse_args()
    _args.reach_base = _args.reach_base.rstrip("/")

    print(f"[dispatch] 调度服务已启动（常驻属正常）: http://{_lan_ip()}:{_args.port}/")
    print(f"[dispatch] 外部触发: POST /task/flip → 轮询 GET /task/status")
    print(f"[dispatch] reach_server 按需拉起: {sys.executable} reach_server.py "
          f"--camera-serial {_args.camera_serial} "
          f"--network-interface {_args.network_interface}")
    uvicorn.run(app, host=_args.host, port=_args.port, log_level="warning")


if __name__ == "__main__":
    main()
