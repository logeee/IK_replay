"""17001 任务调度服务——外部系统触发拨闸流程的唯一入口。

部署形态（谁常驻、谁按需）：
    · 本服务 17001：常驻。外部系统（导航栈把机器人开到电柜前后）只需
      POST /task/flip，然后轮询 GET /task/status 拿结果。
    · yolo_server 7004 / console 7002：常驻（不占相机）。
    · reach_server 18001：平时关着。本服务收到任务时子进程拉起；它只读订阅
      外部 teleimager ZMQ，不会启动本机相机。任务结束（无论成败）后 SIGINT
      优雅关掉，reach_server 自己会释放手臂并断开 ZMQ/DDS。

若收到任务时 18001 已经在跑（比如你手动开着调试），直接复用，任务结束
后也不关它——谁启动的谁负责关。

启动（fastapi 环境）：
    /home/robot/miniconda3/envs/fastapi/bin/python -m api.dispatch

外部对接（面向作业平台的统一任务接口，平台不感知后端用 VLA 还是 IK）：
    POST /check/flip   → body {"language": "<固定指令，必填，同 /task/flip>"}
                          站位检查（同步阻塞，最长约 4 分钟，客户端超时建议
                          ≥300s）。导航把机器人开到柜前后、调 /task/flip 之前
                          先调它确认"站到位了"。四步：距离粗查 0.44~0.60m →
                          朝向收进 [-12,-8]°（不在带内会真机转身纠正）→ 5 电机
                          限位（左腿俯仰 ±6°、右腿俯仰 ±10°、腿偏航 ±30°、
                          腰偏航 -1~3.5°）+
                          距离终检 0.44~0.55m → YOLO：若识别到开关已在
                          language 的目标状态 → passed=true 且 need_flip=false
                          （无需拨动），否则要求拨前状态的框横向居中（中间 60%）。
                          相机：passed 且 need_flip → 保持开启留给紧接着的
                          /task/flip 复用；失败或无需拨动 → 立即关（外部手动
                          启动的 18001 除外，成败都不动）。
    POST /task/flip    → body {"language": "<固定指令，必填>",
                                "retries": 3}   # 可选，最大尝试轮数（VLA 后端忽略）
                          返回 {"ok": true, "task_id": "..."}；执行中再触发 → 409
    GET  /task/status  → 状态机 idle/starting/running/done + 流程日志尾部
                          + 最终结果（错误码见 api.flow.ErrorCode）
    POST /task/abort   → 急停正在执行的动作并强制结束任务（= /emergency/stop）
    POST /emergency/stop → 强制停止（任何状态下都可调，没任务在跑也能用）：
                          停转身 → 急停手臂轨迹 → 释放手臂（权重渐出，控制权
                          交还本体）→ 关掉自己拉起的 reach_server（放相机/DDS）。
                          用于"别的程序要接管、必须马上让我们松手"的场合。

language 逐字固定（大小写/空格容错，多余的不认）：
    "Change the switch from close to remote"   就地 → 远方（IK 已验证）
    "Change the switch from remote to close"   远方 → 就地（暂未支持，
        任务立即以 NOT_IMPLEMENTED 结束，不会启动任何硬件）
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
_check: dict[str, Any] | None = None  # 当前/最近一次站位检查（/check/flip）
# 站位检查通过后留下的 reach 子进程——交给紧接着的 /task/flip 认领，
# 它任务结束后负责关；下一次 /check/flip 也可认领（失败时就能关掉它）
_check_reach_proc: subprocess.Popen | None = None
# 收到 /emergency/stop 后置位：正在跑的检查/任务在最近的检查点退出。
# 新的 /check/flip、/task/flip 会先清掉它。
_estop = threading.Event()


# ------------------------------------------------------------ reach 生命周期


def _reach_alive(timeout_s: float = 2.0) -> bool:
    try:
        r = _http.get(f"{_args.reach_base}/api/reach/status", timeout=timeout_s)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _spawn_reach(task: dict) -> None:
    """子进程拉起 reach_server，输出落到日志文件。"""
    log_dir = ROOT / "logs" / "reach"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"dispatch_reach_{datetime.now():%Y%m%d_%H%M%S}.log"
    cmd = [
        sys.executable,
        str(ROOT / "reach_server.py"),
        "--port", str(_args.reach_port),
        "--camera-source", "zmq",
        "--camera-host", _args.camera_host,
        "--camera-request-port", str(_args.camera_request_port),
        "--camera-name", _args.camera_name,
        "--camera-rgbd-calib", _args.camera_rgbd_calib,
        "--network-interface", _args.network_interface,
        "--calib", _args.calib,
        "--tool-out-mm", str(_args.tool_out_mm),
    ]
    if _args.camera_port is not None:
        cmd.extend(["--camera-port", str(_args.camera_port)])
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
    """SIGINT 优雅关停（reach_server 自己释放手臂并断开数据源），拖住兜底 kill。"""
    proc: subprocess.Popen | None = task.get("reach_proc")
    if proc is None or proc.poll() is not None:
        return
    task["log"].append("关闭 reach_server，断开 ZMQ/DDS…")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15.0)
        task["log"].append("reach_server 已退出")
    except subprocess.TimeoutExpired:
        proc.kill()
        task["log"].append("⚠ reach_server 15s 未退出，已强杀")


@app.on_event("shutdown")
def _shutdown_cleanup() -> None:
    """本服务被 SIGTERM/SIGINT 收掉时，把自己拉起的 reach_server 一并关掉。

    reach 子进程是独立会话（start_new_session=True），按 dispatch 命令行
    匹配的 pkill 碰不到它——不在这里收，8001 会变孤儿继续占相机。
    覆盖两种在管的进程：站位检查通过后留给 /task/flip 的、任务正拿着的。
    （kill -9 不走这里，那种情况只能手动 pkill -f reach_server.py。）
    """
    global _check_reach_proc
    with _lock:
        leftover, _check_reach_proc = _check_reach_proc, None
        task = _task
    if leftover is not None and leftover.poll() is None:
        holder = {"log": [], "reach_proc": leftover}
        _stop_reach(holder)
        for line in holder["log"]:
            print(f"[dispatch] 退出清理: {line}")
    if (task is not None and not task.get("reach_external")
            and task.get("reach_proc") is not None):
        try:
            _stop_reach(task)
            print("[dispatch] 退出清理: 任务持有的 reach_server 已关闭")
        except Exception as exc:
            print(f"[dispatch] 退出清理失败: {exc}")


# ------------------------------------------------------------------ 任务执行


def _run_task(task: dict) -> None:
    global _check_reach_proc
    try:
        with _lock:
            leftover = _check_reach_proc   # 站位检查通过后留下的 reach
            _check_reach_proc = None
        if _reach_alive():
            if leftover is not None and leftover.poll() is None:
                task["reach_proc"] = leftover
                task["reach_external"] = False
                task["log"].append("复用站位检查留下的 reach_server（任务结束后关闭）")
            else:
                task["reach_external"] = True
                task["log"].append("reach_server 已在运行（外部启动），复用且任务后不关闭")
        else:
            if leftover is not None and leftover.poll() is None:
                # 进程还活着但 HTTP 已不响应：先收掉再重启，别让两个进程抢相机
                task["reach_proc"] = leftover
                task["log"].append("站位检查留下的 reach_server 已不响应，先关掉再重启")
                _stop_reach(task)
                task["reach_proc"] = None
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
                          console=console, yolo=yolo,
                          max_flip_rounds=int(task.get("retries") or 3))
        task["flow"] = flow
        if _estop.is_set():
            # 强制停止卡在"拉起 reach"和"建流程"之间时，别让流程真的跑起来
            flow.request_abort()
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


# ------------------------------------------------------------ 站位检查

CHECK_DMIN, CHECK_DMAX = 0.4, 1.0        # 平面拟合深度范围（同 flip 流程）
CHECK_DIST_COARSE = (0.44, 0.60)         # 第 1 步：距离粗查（m）
CHECK_DIST_FINAL = (0.44, 0.55)          # 第 3 步：距离终检（m，更严）
# 下限 0.44 = 最近的起手式门槛（「0.44避障起手式」），比它近就没有可用起手式
CHECK_YAW_TARGET_DEG = -7.0              # 第 2 步：抬手前粗对齐带 [-9, -5]°
CHECK_YAW_TOL_DEG = 2.0                  # 带比流程的 ±1.5 略宽：流程自己还会
                                         # 再粗对齐一次，这里没必要卡得更死
CHECK_ALIGN_TIMEOUT_S = 90.0
CHECK_MOTOR_LIMITS_DEG = {               # 第 3 步：允许区间 (下限, 上限)（°），键 = 电机序号
    0: ("左腿俯仰", -6.0, 6.0), 6: ("右腿俯仰", -10.0, 10.0),
    2: ("左腿偏航", -30.0, 30.0), 8: ("右腿偏航", -30.0, 30.0),
    14: ("腰偏航", -1.0, 3.5),   # 非对称：机器人惯常往左偏
}
CHECK_BOX_BAND = (0.20, 0.80)            # 第 4 步：框中心须在画宽的中间 60%
CHECK_SCENE_CLASSES = ("就地", "远方")
# language → (拨前状态, 目标状态)：第 4 步识别到目标状态 = 无需拨动
CHECK_KIND_STATES = {"close_to_remote": ("就地", "远方"),
                     "remote_to_close": ("远方", "就地")}


def _run_checks(check: dict, kind: str) -> dict:
    """四步站位检查。返回 {"passed", "need_flip", "failed_step", "message", "steps"}。

    steps 里每步都带实测值；失败即停，后面的步骤不做。
    第 2 步可能真机转身（复用 reach 的新对中闭环），其余步骤纯只读。
    第 4 步若识别到开关已在 language 的目标状态 → passed=True 且
    need_flip=False（不必再调 /task/flip，居中检查也不做了）。
    """
    log: list = check["log"]
    steps: list[dict] = []
    check["steps"] = steps
    client = ReachClient(_args.reach_base)

    def ok_step(message: str) -> None:
        steps[-1].update(passed=True, message=message)
        log.append(f"✔ 第{steps[-1]['step']}步 {steps[-1]['name']}：{message}")

    def fail(message: str) -> dict:
        step = steps[-1]
        step.update(passed=False, message=message)
        log.append(f"✘ 第{step['step']}步 {step['name']}：{message}")
        return {"passed": False, "need_flip": True, "failed_step": step["step"],
                "message": f"第{step['step']}步（{step['name']}）不满足：{message}",
                "steps": steps}

    def measure() -> dict:
        if _estop.is_set():
            raise RuntimeError("收到强制停止")
        fit = client.perpendicular(CHECK_DMIN, CHECK_DMAX)
        if not fit.get("ok"):
            raise RuntimeError(f"平面拟合失败: {fit.get('error')}")
        return fit

    # ---- 1️⃣ 距离粗查（纯测量）----
    lo, hi = CHECK_DIST_COARSE
    steps.append({"step": 1, "name": "距离粗查", "range_m": [lo, hi]})
    fit = measure()
    dist = float(fit["distance_m"])
    steps[-1]["distance_m"] = round(dist, 3)
    if not lo <= dist <= hi:
        return fail(f"距柜面 {dist:.3f} m，要求 {lo}~{hi} m——"
                    f"转身救不了距离，需要导航重新进位")
    ok_step(f"距柜面 {dist:.3f} m")

    # ---- 2️⃣ 朝向：不在带内就对中纠正（可能真机转身）----
    band_lo = CHECK_YAW_TARGET_DEG - CHECK_YAW_TOL_DEG
    band_hi = CHECK_YAW_TARGET_DEG + CHECK_YAW_TOL_DEG
    steps.append({"step": 2, "name": "朝向（平面指数）",
                  "range_deg": [band_lo, band_hi], "corrected": False})
    yaw = float(fit["yaw_err_deg"])
    if not band_lo <= yaw <= band_hi:
        log.append(f"yaw {yaw:+.2f}° 不在 [{band_lo}, {band_hi}]° 内，"
                   f"启动对中（真机原地转身）…")
        res = client.align_yaw_start(CHECK_DMIN, CHECK_DMAX,
                                     tol_deg=CHECK_YAW_TOL_DEG,
                                     target_deg=CHECK_YAW_TARGET_DEG,
                                     mode="hold")
        if not res.get("ok"):
            steps[-1]["yaw_deg"] = round(yaw, 2)
            return fail(f"对中启动失败: {res.get('error')}")
        deadline = time.monotonic() + CHECK_ALIGN_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if _estop.is_set():
                client.align_yaw_stop()
                raise RuntimeError("收到强制停止（对中中）")
            fit = client.perpendicular(CHECK_DMIN, CHECK_DMAX)
            align = fit.get("align") or {}
            if not align.get("running"):
                log.append(f"对中结束：{align.get('message') or ''}")
                break
        else:
            client.align_yaw_stop()
            steps[-1]["yaw_deg"] = round(yaw, 2)
            return fail(f"对中超时（>{CHECK_ALIGN_TIMEOUT_S:.0f}s 未收敛）")
        steps[-1]["corrected"] = True
        yaw = float(measure()["yaw_err_deg"])
    steps[-1]["yaw_deg"] = round(yaw, 2)
    if not band_lo <= yaw <= band_hi:
        return fail(f"对中后 yaw {yaw:+.2f}° 仍不在 [{band_lo}, {band_hi}]° 内，"
                    f"转不进带内")
    ok_step(f"yaw {yaw:+.2f}°"
            + ("（已转动纠正）" if steps[-1]["corrected"] else "（无需纠正）"))

    # ---- 3️⃣ 5 电机 + 距离终检（纯测量）----
    steps.append({"step": 3, "name": "电机与距离终检", "items": []})
    res = client.motors()
    if not res.get("ok"):
        return fail(f"读电机角度失败: {res.get('error')}")
    items: list[dict] = steps[-1]["items"]
    bad: list[str] = []
    for m in res["motors"]:
        name, m_lo, m_hi = CHECK_MOTOR_LIMITS_DEG[m["index"]]
        good = m_lo <= float(m["q_deg"]) <= m_hi
        items.append({"item": f"{name}#{m['index']}", "q_deg": m["q_deg"],
                      "range_deg": [m_lo, m_hi], "passed": good})
        if not good:
            bad.append(f"{name} {m['q_deg']:+.2f}°（要求 {m_lo}~{m_hi}°）")
    lo, hi = CHECK_DIST_FINAL
    dist = float(measure()["distance_m"])
    dist_good = lo <= dist <= hi
    items.append({"item": "距离", "distance_m": round(dist, 3),
                  "range_m": [lo, hi], "passed": dist_good})
    if not dist_good:
        bad.append(f"距离 {dist:.3f} m（要求 {lo}~{hi} m）")
    if bad:
        return fail("；".join(bad))
    ok_step(f"5 电机全部在限内，距离 {dist:.3f} m")

    # ---- 4️⃣ YOLO：已在目标状态则无需拨动；否则检查框横向居中 ----
    pre_cls, post_cls = CHECK_KIND_STATES[kind]
    lo, hi = CHECK_BOX_BAND
    steps.append({"step": 4, "name": "YOLO 状态与居中", "band_ratio": [lo, hi],
                  "expect_pre": pre_cls, "expect_post": post_cls})
    if _args.no_yolo:
        return fail("调度启动时带了 --no-yolo，无法做该项检查")
    scene = YoloClient(_args.yolo).scene()
    if not scene.get("ok"):
        return fail(f"YOLO 检测失败: {scene.get('error')}")
    cands = [b for b in scene.get("boxes", [])
             if b.get("name") in CHECK_SCENE_CLASSES]
    if not cands:
        return fail("画面里没识别到「就地/远方」开关框")
    best = max(cands, key=lambda b: b["conf"])
    if best["name"] == post_cls:
        steps[-1].update({"scene": best["name"], "conf": best["conf"]})
        ok_step(f"识别到「{post_cls}」——开关已在目标状态，无需拨动")
        return {"passed": True, "need_flip": False, "failed_step": None,
                "message": f"开关已在目标状态「{post_cls}」，无需调用 /task/flip"
                           f"（相机已释放）",
                "steps": steps}
    width = float(client.status()["camera"]["width"])
    x1, _, x2, _ = best["xyxy"]
    cx = (x1 + x2) / 2.0
    ratio = cx / width
    steps[-1].update({"scene": best["name"], "conf": best["conf"],
                      "cx_px": round(cx, 1), "frame_width_px": int(width),
                      "cx_ratio": round(ratio, 3)})
    if ratio < lo:
        off = lo - ratio
        return fail(f"「{best['name']}」框偏左：中心在画宽 {ratio * 100:.1f}% 处，"
                    f"超出左边界 {off * 100:.1f}%（约 {off * width:.0f}px）")
    if ratio > hi:
        off = ratio - hi
        return fail(f"「{best['name']}」框偏右：中心在画宽 {ratio * 100:.1f}% 处，"
                    f"超出右边界 {off * 100:.1f}%（约 {off * width:.0f}px）")
    ok_step(f"「{best['name']}」框中心在画宽 {ratio * 100:.1f}% 处（要求 20%~80%）")

    return {"passed": True, "need_flip": True, "failed_step": None,
            "message": "站位合格，可以调用 /task/flip（相机保持开启供其复用）",
            "steps": steps}


@app.post("/check/flip")
def check_flip(body: dict | None = None):
    """站位检查（同步阻塞）。与 /task/flip 互斥；相机生命周期见模块注释。

    Body: {"language": "<和 /task/flip 同款固定指令，必填>"}——
    第 4 步靠它判断"开关是否已在目标状态"。
    """
    global _check, _check_reach_proc
    language = str((body or {}).get("language") or "").strip()
    if not language:
        return JSONResponse(
            {"ok": False, "error": "缺少必填字段 language",
             "supported": ["Change the switch from close to remote",
                           "Change the switch from remote to close"]},
            status_code=422)
    kind = _parse_language(language)
    if kind is None:
        return JSONResponse(
            {"ok": False, "error": f"无法识别的指令: {language!r}",
             "supported": ["Change the switch from close to remote",
                           "Change the switch from remote to close"]},
            status_code=422)
    with _lock:
        if _task is not None and _task["state"] != "done":
            return JSONResponse(
                {"ok": False, "error": "拨闸任务执行中，不能同时做站位检查",
                 "task_id": _task["id"], "state": _task["state"]},
                status_code=409)
        if _check is not None and _check["state"] == "running":
            return JSONResponse(
                {"ok": False, "error": "已有站位检查在执行"}, status_code=409)
        _estop.clear()     # 新的检查开始，清掉上一次的强制停止标记
        _check = {"state": "running", "log": [], "steps": [],
                  "reach_proc": None, "reach_log": None,
                  "started_at": datetime.now().isoformat(timespec="seconds")}
        check = _check
        check["log"].append(f"指令: {language}（{kind}）")
        leftover = _check_reach_proc   # 上次检查通过留下的 reach，认领回来
        _check_reach_proc = None
    t0 = time.monotonic()
    camera_kept = False
    try:
        if leftover is not None and leftover.poll() is None:
            check["reach_proc"] = leftover
        if _reach_alive():
            if check["reach_proc"] is None:
                check["log"].append("reach_server 已在运行（外部启动），"
                                    "复用；本检查成败都不关它")
            else:
                check["log"].append("复用上次站位检查留下的 reach_server")
        else:
            if check["reach_proc"] is not None:
                check["log"].append("留下的 reach_server 已不响应，先关掉再重启")
                _stop_reach(check)
                check["reach_proc"] = None
            _spawn_reach(check)
            _wait_reach_ready(check)
        report = _run_checks(check, kind)
    except Exception as exc:
        report = {"passed": False, "need_flip": True, "failed_step": None,
                  "message": f"检查执行异常: {exc}",
                  "steps": check.get("steps", [])}
        check["log"].append(f"✘ {report['message']}")
    finally:
        # 相机只在「站位合格且接下来要拨」时留给 /task/flip 复用；
        # 失败或"已在目标状态无需拨"都立即释放
        keep = bool(report.get("passed")) and bool(report.get("need_flip"))
        proc = check.get("reach_proc")
        if proc is not None and proc.poll() is None:
            if keep:
                with _lock:
                    _check_reach_proc = proc   # 留给紧接着的 /task/flip 认领
                camera_kept = True
            else:
                _stop_reach(check)
        elif keep and check.get("reach_proc") is None and _reach_alive():
            camera_kept = True                 # 外部启动的 8001，本来就开着
        check["state"] = "done"

    report.update(ok=True, camera_kept=camera_kept,
                  duration_s=round(time.monotonic() - t0, 1),
                  log=check["log"])
    return report


# ---------------------------------------------------------------------- 接口


# 作业平台的指令是逐字固定的句子，等价于枚举值；归一化后精确匹配。
LANGUAGE_TASKS = {
    "change the switch from close to remote": "close_to_remote",
    "change the switch from remote to close": "remote_to_close",
}


def _parse_language(text: str) -> str | None:
    norm = " ".join(text.lower().replace(".", " ").split())
    return LANGUAGE_TASKS.get(norm)


@app.post("/task/flip")
def task_submit(body: dict | None = None):
    global _task
    language = str((body or {}).get("language") or "").strip()
    if not language:
        return JSONResponse(
            {"ok": False, "error": "缺少必填字段 language",
             "supported": ["Change the switch from close to remote",
                           "Change the switch from remote to close"]},
            status_code=422)
    kind = _parse_language(language)
    if kind is None:
        return JSONResponse(
            {"ok": False, "error": f"无法识别的指令: {language!r}",
             "supported": ["Change the switch from close to remote",
                           "Change the switch from remote to close"]},
            status_code=422)
    try:
        retries = int((body or {}).get("retries") or 3)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "retries 必须是整数"},
                            status_code=422)
    if not 1 <= retries <= 20:
        return JSONResponse({"ok": False, "error": "retries 取值范围 1~20"},
                            status_code=422)

    with _lock:
        if _task is not None and _task["state"] != "done":
            return JSONResponse(
                {"ok": False, "error": "已有任务在执行",
                 "task_id": _task["id"], "state": _task["state"]},
                status_code=409)
        if _check is not None and _check["state"] == "running":
            return JSONResponse(
                {"ok": False, "error": "站位检查（/check/flip）执行中，请等它返回再触发任务"},
                status_code=409)
        _estop.clear()     # 新任务开始，清掉上一次的强制停止标记
        now = datetime.now().isoformat(timespec="seconds")
        _task = {"id": uuid.uuid4().hex[:10], "state": "starting",
                 "language": language, "kind": kind, "retries": retries,
                 "started_at": now, "finished_at": None,
                 "result": None, "flow": None,
                 "log": [f"指令: {language}（{kind}，最多 {retries} 轮）"],
                 "reach_proc": None, "reach_external": False}
        if kind == "remote_to_close":
            # 明知做不了就快速失败，不启动任何硬件——平台仍按统一的
            # 轮询路径拿到结果，错误码 NOT_IMPLEMENTED
            _task["state"] = "done"
            _task["finished_at"] = now
            _task["result"] = {
                "ok": False, "code": 1, "code_name": "NOT_IMPLEMENTED",
                "message": "「远方 → 就地」暂未支持（镜像动作尚未真机验证）",
                "detail": {}}
            return {"ok": True, "task_id": _task["id"]}
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
            "language": t.get("language"), "retries": t.get("retries"),
            "started_at": t["started_at"], "finished_at": t["finished_at"],
            "result": t["result"], "log": log[-60:]}


def _reach_base() -> str:
    return _args.reach_base if _args is not None else "http://127.0.0.1:8001"


def _emergency_stop(reason: str) -> dict:
    """强制停止：停转身 → 急停轨迹 → 释放手臂 → 关掉自己拉起的 reach_server。

    任何状态下都能调（没任务在跑也能用，用于收拾上一次留下的接管状态）。
    每一步都尽力做完，前一步失败不影响后一步——目标只有一个：让我们这边
    彻底不再给机器人发指令，把控制权交还本体，好让别的程序安全接手。
    """
    _estop.set()
    base = _reach_base()
    actions: list[str] = []

    def post(path: str, body: dict | None = None, timeout: float = 5.0) -> dict:
        try:
            r = _http.post(f"{base}{path}", json=body or {}, timeout=timeout)
            try:
                return r.json() if r.content else {}
            except ValueError:
                return {"http": r.status_code}
        except requests.RequestException as exc:
            return {"error": str(exc)}

    with _lock:
        task, check = _task, _check
    flow: SwitchFlow | None = task.get("flow") if task else None
    if flow is not None:
        flow.request_abort()      # 流程在最近的检查点退出，不再下发新动作

    if _reach_alive(1.5):
        # 1) 先停基座：对中闭环可能正拿着速度指令在转身
        r = post("/api/reach/align_yaw", {"stop": True})
        actions.append("停止转身" + (f"（{r['error']}）" if r.get("error") else ""))
        # 2) 急停手臂轨迹（冻结在当前指令位，不下坠）
        r = post("/api/reach/stop")
        actions.append("急停手臂轨迹" + (f"（{r.get('error')}）"
                                        if r.get("error") else ""))
        # 3) 等执行线程真的退出，否则 disarm 会被"轨迹执行中"挡回 409
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                st = _http.get(f"{base}/api/reach/exec_status", timeout=1.0).json()
            except (requests.RequestException, ValueError):
                break
            if not st.get("running"):
                break
            time.sleep(0.2)
        # 4) 释放手臂：权重渐出，控制权交还本体控制器
        r = post("/api/reach/disarm", timeout=15.0)
        actions.append("释放手臂" if r.get("ok")
                       else f"释放手臂失败（{r.get('error') or r}）")
    else:
        actions.append("reach_server 未在运行，我们本来就没有控制权")

    # 5) 关掉自己拉起的 reach_server（外部启动的不动，只是已经放了手）
    global _check_reach_proc
    with _lock:
        leftover, _check_reach_proc = _check_reach_proc, None
    holders: list[dict] = [{"log": [], "reach_proc": leftover}]
    if task is not None and not task.get("reach_external"):
        holders.append(task)
    if check is not None:
        holders.append(check)
    for holder in holders:
        if holder.get("reach_proc") is None:
            continue
        try:
            _stop_reach(holder)
            actions.append("已关闭 reach_server（断开 ZMQ/DDS）")
        except Exception as exc:
            actions.append(f"关闭 reach_server 出错: {exc}")

    line = f"⚑ 强制停止（{reason}）：" + "；".join(actions)
    for holder in (task, check):
        if holder is not None and holder.get("state") != "done":
            holder["log"].append(line)
    print(f"[dispatch] {line}")
    return {"ok": True, "reason": reason, "actions": actions,
            "arm_released": any(a == "释放手臂" for a in actions),
            "task_state": task["state"] if task else "idle"}


@app.post("/emergency/stop")
def emergency_stop(body: dict | None = None):
    """强制停止（任何状态下都可调，无任务时也能用）：急停 + 释放手臂控制权。

    Body 可选 {"reason": "..."}。返回逐项动作清单。
    """
    reason = str((body or {}).get("reason") or "外部强制停止")
    return _emergency_stop(reason)


@app.post("/task/abort")
def task_abort():
    """中止当前任务。等价于 /emergency/stop，只是会额外提示没有任务在跑。"""
    with _lock:
        t = _task
    res = _emergency_stop("task/abort")
    if t is None or t["state"] == "done":
        res["message"] = "没有正在执行的任务；已按强制停止处理（急停+释放手臂）"
    else:
        res["message"] = "已急停并释放手臂，稍后轮询 /task/status"
    return res


@app.get("/")
def index():
    return {"service": "flip-dispatch",
            "usage": {"check": 'POST /check/flip  body={"language": "..."}'
                               '（站位检查，同步，客户端超时建议 ≥300s）',
                      "start": 'POST /task/flip  body={"language": "..."}',
                      "status": "GET /task/status",
                      "abort": "POST /task/abort",
                      "estop": "POST /emergency/stop（任何状态：急停+释放手臂）"},
            "languages": ["Change the switch from close to remote",
                          "Change the switch from remote to close"]}


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
    parser.add_argument("--reach-base", default="http://127.0.0.1:18001")
    parser.add_argument("--reach-port", type=int, default=18001,
                        help="按需拉起的 reach_server 监听端口，须与 --reach-base 一致")
    parser.add_argument("--camera-host", default="127.0.0.1",
                        help="外部 teleimager 主机")
    parser.add_argument("--camera-request-port", type=int, default=60000,
                        help="teleimager 配置请求端口")
    parser.add_argument("--camera-port", type=int, default=None,
                        help="RGB-D ZMQ 端口；默认从 teleimager 配置获取")
    parser.add_argument("--camera-name", default="head_rgbd_camera")
    parser.add_argument(
        "--camera-rgbd-calib",
        default=str(ROOT / "config" / "camera" / "orbbec_rgbd_calibration.json"),
        help="本地 SDK 一次性导出的 RGB-D 标定 JSON",
    )
    parser.add_argument(
        "--calib",
        default=("/home/robot/yx/project/calib/hand_eye_3D/handeye3d_data/"
                 "biaoding/handeye3d_result.json"),
        help="reach_server 使用的手眼标定结果",
    )
    parser.add_argument("--tool-out-mm", type=float, default=15.0,
                        help="TCP 沿腕系 +x 方向额外外移毫米数")
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
    print(f"[dispatch] 外部触发: POST /task/flip （body 带 language）→ 轮询 GET /task/status")
    print(f"[dispatch] reach_server 按需拉起: {sys.executable} reach_server.py "
          f"--port {_args.reach_port} --camera-source zmq "
          f"--camera-host {_args.camera_host} --network-interface {_args.network_interface} "
          f"--calib {_args.calib} --tool-out-mm {_args.tool_out_mm:g}")
    uvicorn.run(app, host=_args.host, port=_args.port, log_level="warning")


if __name__ == "__main__":
    main()
