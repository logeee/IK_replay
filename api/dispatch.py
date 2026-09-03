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
                                "retries": 3,   # 可选，最大尝试轮数（VLA 后端忽略）
                                "manual": false,  # 可选，手动确认模式（见下）
                                "site": "lab",  # 可选，现场：lab=实验室柜（默认）
                                                # factory=工厂柜；只影响该柜
                                                # 已验证动作的筛选，不影响方向
                                "target_offset_wall_mm": {"x":0,"y":0,"z":0},
                                # 可选，目的点人工微调（墙面系，mm，单轴限 ±100）：
                                # x=沿墙向右 y=法向入墙 z=沿墙向上。叠加在 7005
                                # 点云算法算出的目的点上，不动粉点→目的点的模型偏移
                                # 也可改传 "target_offset_preset": "配置名"，
                                # 运行时按所选起手式距离应用静态值或关键帧曲线；
                                # target_offset_wall_mm 与它同时出现时，显式 XYZ 优先
                                "lift_mm": {"base":10,"step":10,"max":30},
                                # 可选，拨点上抬（抵消重力下垂，mm，各项 0~50）：
                                # 首轮抬 base，每重试一轮加 step，合计封顶 max。
                                # 不按距柜面远近区分
                                "push_force_n": 15}
                                # 可选，沿拨动方向的前馈推力（N，0~40；0=关闭）
                          site / target_offset_wall_mm / lift_mm / push_force_n
                          不带时自动套「外部调用
                          默认配置」（两个任务方向可分别选择命名偏移配置和推力；
                          GET/POST /config/defaults 读改存，配置由
                          POST /config/offset-presets[/delete] 管理）
                          返回 {"ok": true, "task_id": "..."}；执行中再触发 → 409

方向：language 唯一决定——「远方→就地」向左拨（右→左），「就地→远方」
    向右拨（左→右）；两边使用相同位移、下倾、重试和收尾逻辑，推力可
    按方向分别配置。YOLO（Xuanniu_D.pt）识别开关物理指向「远方就地左/右」，
    任何现场一致；site 只决定该柜验证过哪些动作（能力注册表 task.sites），
    不再参与方向判断。工厂柜双向已验证，实验室柜当前只验证过向左拨。
    GET  /task/status  → 状态机 idle/starting/running/done + 流程日志尾部
                          + 最终结果（错误码见 api.flow.ErrorCode）
                          + step_times 分步耗时 + prompt 当前等待确认的步骤
    POST /task/abort   → 急停正在执行的动作并强制结束任务（= /emergency/stop）

手动确认模式（网页 http://<机器人IP>:17001/ 上可视化操作）：
    /task/flip 带 "manual": true 后，流程在每个主要步骤（接管/场景判断/
    对中/起手式/细对齐/取点/拨动/复核/收尾）执行前挂起，网页给出"我即将
    做什么"的说明，操作员选择：
      · 执行该步（proceed）· 中止流程（abort，走受控回落+释放）
      · 其他动作：前往某已录位点 / 接管手臂 / 释放手臂——在流程线程上
        串行执行，完成后回到同一确认提示，可连续选择多个其他动作
    POST /task/decision → 提交上述决定；GET /manual/waypoints → 位点列表。
    POST /arm/stop      → 机械臂复位并释放：立即冻结当前轨迹并中断流程，
                          以50% Kp、0.15rad/s沿安全路点回「起手点测试」后释放。
    POST /emergency/stop → 强制停止（任何状态下都可调，没任务在跑也能用）：
                          停转身 → 急停手臂轨迹 → 释放手臂（权重渐出，控制权
                          交还本体）→ 关掉自己拉起的 reach_server（放相机/DDS）。
                          用于"别的程序要接管、必须马上让我们松手"的场合。

language 逐字固定（大小写/空格容错，多余的不认）：
    "Change the switch from close to remote"   就地 → 远方（向右拨，工厂柜验证）
    "Change the switch from remote to close"   远方 → 就地（向左拨，两柜验证）
"""

from __future__ import annotations

import argparse
import math
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
from fastapi.responses import FileResponse, JSONResponse

from copy import deepcopy

from core.alignment_config import load_alignment_config
from core.capability_client import (
    DEFAULT_CAPABILITY_URL,
    CapabilityUnavailable,
    describe_active,
    fetch_snapshot,
)
from core.capability_registry import (
    ARM_LABELS,
    IMPLEMENTED_METHODS,
    calibration_info,
    capability_for,
    claimed_sequence_names,
    find_hand,
)
from core.dispatch_defaults import (
    DEFAULT_DISPATCH_DEFAULTS,
    DEFAULT_LIFT_MM,
    DEFAULT_PUSH_FORCE_N,
    OFFSET_KEYFRAME_MAX_DISTANCE_M,
    OFFSET_KEYFRAME_MIN_DISTANCE_M,
    OFFSET_KEYFRAME_STEP_M,
    find_offset_preset,
    load_dispatch_defaults,
    save_dispatch_defaults,
    validate_lift_mm,
    validate_offset_keyframes,
    validate_offset_mm,
    validate_push_force_n,
)

from .client import ReachClient
from .console_client import ConsoleClient
from .flow import (
    FLIP_KIND_STATES,
    KIND_DIRECTIONS,
    KIND_LABELS,
    ErrorCode,
    FlowError,
    SwitchFlow,
    resolve_flip_intent,
)
from .switch_states import SCENE_CLASSES
from .pointcloud_client import PointcloudClient
from .yolo_client import YoloClient

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

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
_service_started_at = datetime.now().isoformat(timespec="seconds")
_service_started_monotonic = time.monotonic()
_task_stats = {
    "accepted": 0,
    "succeeded": 0,
    "failed": 0,
    "rejected_busy": 0,
}


def _service_stats_locked() -> dict[str, Any]:
    """Caller must hold _lock."""
    return {
        "started_at": _service_started_at,
        "uptime_s": round(max(0.0, time.monotonic() - _service_started_monotonic), 1),
        **_task_stats,
    }


def _count_finished_task_locked(task: dict) -> None:
    """Count one accepted task exactly once. Caller must hold _lock."""
    if task.get("stats_counted"):
        return
    result = task.get("result")
    if not isinstance(result, dict):
        return
    if result.get("ok"):
        _task_stats["succeeded"] += 1
    else:
        _task_stats["failed"] += 1
    task["stats_counted"] = True


# ---------------------------------------------------- 能力注册表（18000 配置）

# 四级能力注册表快照：main() 启动拜访 18000 经 HTTP 拉取后写入（拉不到
# 直接拒绝启动）；改 18000 配置后重启 17001 生效，不热切换。本服务不再
# 直接读 config/capability_registry.json——那是 18000 的存储。
# 缓存为 None（只会出现在测试注入或未经 main 的导入路径）时按旧的
# SITE_SUPPORTED_KINDS / --calib 行为走。
_capability_registry_cache: dict[str, Any] | None = None
_capability_registry_loaded = False


def _capability_registry() -> dict[str, Any] | None:
    return _capability_registry_cache


def _active_arm_context() -> dict[str, Any] | None:
    """激活组合（臂 + 手型号）对应的执行链与标定；注册表不可用时 None。"""
    registry = _capability_registry()
    if registry is None:
        return None
    active = registry.get("active")
    if not active:
        return None
    hand = find_hand(registry, active["hand_id"]) or {}
    calib = calibration_info(registry, active["arm"], active["hand_id"])
    return {
        "arm": active["arm"],
        "hand_id": active["hand_id"],
        "hand_name": hand.get("name") or active["hand_id"],
        "tool_out_mm": hand.get("tool_out_mm"),
        "calib_status": calib["status"],
        "calib_path": (str(ROOT / calib["path"])
                       if calib["status"] == "ready" else None),
    }


def _capability_for_kind(site: str, kind: str) -> dict[str, Any] | None:
    """激活组合下，某现场 + 任务方向对应的已启用能力条目。

    方向由 kind 唯一决定（与 site 无关）；site 只用于筛选该柜验证过
    的能力（task.sites）。
    """
    registry = _capability_registry()
    if registry is None:
        return None
    active = registry.get("active")
    if not active or site not in SITES or kind not in KIND_DIRECTIONS:
        return None
    return capability_for(registry, active["arm"], active["hand_id"],
                          KIND_DIRECTIONS[kind], site)


# ------------------------------------------------------------ reach 生命周期


def _reach_alive(timeout_s: float = 2.0) -> bool:
    try:
        r = _http.get(f"{_args.reach_base}/api/reach/status", timeout=timeout_s)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _spawn_reach(task: dict) -> None:
    """子进程拉起 reach_server，输出落到日志文件。

    执行链 / 手眼标定 / TCP 外移优先取能力注册表的激活组合（18000 配置）；
    注册表不可用或标定待补时回退命令行参数并在任务日志里说明。
    """
    # reach_server 启动时自己也要拜访 18000；这里先确认可达，让 18000 挂掉
    # 的错误直接落在任务日志里，而不是等 reach 起不来再翻它的日志。
    try:
        fetch_snapshot(_args.capability_url, attempts=1)
    except CapabilityUnavailable as exc:
        raise RuntimeError(f"18000 能力中心不可达，无法拉起 reach_server：{exc}")

    log_dir = ROOT / "logs" / "reach"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"dispatch_reach_{datetime.now():%Y%m%d_%H%M%S}.log"
    chain = "right_arm"
    calib = _args.calib
    tool_out_mm = _args.tool_out_mm
    ctx = _active_arm_context()
    if ctx is not None:
        chain = ctx["arm"]
        if ctx["tool_out_mm"] is not None:
            tool_out_mm = ctx["tool_out_mm"]
        if ctx["calib_path"]:
            calib = ctx["calib_path"]
            task["log"].append(
                f"激活组合 {ARM_LABELS.get(chain, chain)}+{ctx['hand_name']}："
                f"标定 {calib}")
        else:
            task["log"].append(
                f"⚠ 激活组合 {ARM_LABELS.get(chain, chain)}+{ctx['hand_name']} "
                f"的标定{'待补' if ctx['calib_status'] == 'pending' else '未登记'}"
                f"，回退命令行标定 {calib}")
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
        "--chain", chain,
        "--calib", calib,
        "--tool-out-mm", str(tool_out_mm),
        "--yolo-base", _args.yolo,
        "--capability-url", _args.capability_url,
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


# ------------------------------------------------------------ 手动模式闸门


class _ManualGate:
    """手动模式的步骤闸门：流程线程在每个主要步骤前阻塞，等网页操作员决定。

    决定通过 POST /task/decision 提交，三类：
      · proceed        —— 执行该步，流程继续
      · abort          —— 中止流程（走受控回落 + 释放）
      · goto_waypoint / arm / disarm —— "其他动作"：在流程线程上执行
        （保证机器人指令串行），完成后回到同一个确认提示，可连续选择
    """

    OTHER_ACTIONS = ("goto_waypoint", "arm", "disarm")

    def __init__(self, task: dict):
        self.task = task
        self._event = threading.Event()
        self._decision: dict | None = None

    # ---- HTTP 线程：提交决定 ----

    def submit(self, decision: dict) -> dict:
        action = str(decision.get("action") or "")
        if action not in ("proceed", "abort", *self.OTHER_ACTIONS):
            return {"ok": False, "error": f"不支持的动作: {action!r}"}
        with _lock:
            prompt = self.task.get("prompt")
            if not prompt:
                return {"ok": False,
                        "error": "当前没有等待确认的步骤（可能正在执行动作）"}
            wanted = decision.get("prompt_id")
            if wanted and wanted != prompt["id"]:
                return {"ok": False, "error": "确认请求已过期（步骤已变更）"}
            self._decision = dict(decision)
        self._event.set()
        return {"ok": True, "accepted": action}

    # ---- 流程线程：阻塞等待并处理 ----

    def __call__(self, step_id: str, message: str, detail: dict) -> None:
        flow: SwitchFlow = self.task["flow"]
        actions_done: list[dict] = []
        while True:
            prompt_id = uuid.uuid4().hex[:8]
            with _lock:
                self.task["prompt"] = {
                    "id": prompt_id,
                    "step_id": step_id,
                    "message": message,
                    "detail": detail,
                    "since": datetime.now().isoformat(timespec="seconds"),
                    "actions_done": list(actions_done),
                }
            self._event.clear()
            while not self._event.wait(timeout=0.2):
                if flow.abort.is_set() or flow.reset_and_release.is_set():
                    with _lock:
                        self.task["prompt"] = None
                    message = (
                        "等待确认时收到机械臂复位并释放请求"
                        if flow.reset_and_release.is_set()
                        else "等待确认时收到强制停止"
                    )
                    raise FlowError(ErrorCode.ABORTED, message)
            with _lock:
                decision, self._decision = self._decision or {}, None
                self.task["prompt"] = None   # 执行动作期间不接受新决定
            action = decision.get("action")
            if action == "proceed":
                return
            if action == "abort":
                raise FlowError(ErrorCode.ABORTED,
                                f"操作员在「{message}」前中止流程")
            outcome = self._run_other(flow, action, decision)
            outcome["at"] = datetime.now().strftime("%H:%M:%S")
            actions_done.append(outcome)
            # 回到循环：重新挂出同一步骤的确认提示（可连续选其他动作）

    def _run_other(self, flow: SwitchFlow, action: str, decision: dict) -> dict:
        """执行一个"其他动作"，返回 {"label", "ok", "error"?} 供提示面板展示。"""
        label = {"goto_waypoint": "前往位点", "arm": "接管手臂",
                 "disarm": "释放手臂"}.get(action, action)
        try:
            if action == "goto_waypoint":
                name = str(decision.get("waypoint") or "").strip()
                if not name:
                    return {"label": label, "ok": False, "error": "缺少位点名"}
                label = f"前往位点「{name}」"
                flow._interp_to_waypoint(name, "手动动作")
                return {"label": label, "ok": True}
            if action == "arm":
                res = ReachClient(_args.reach_base).arm()
                ok = bool(res.get("ok"))
                flow.log_lines.append(
                    f"[{datetime.now():%H:%M:%S}] [manual] 接管手臂 → "
                    f"{'成功' if ok else res.get('error')}")
                return {"label": "接管手臂", "ok": ok,
                        **({} if ok else {"error": str(res.get("error"))})}
            if action == "disarm":
                res = ReachClient(_args.reach_base).disarm()
                ok = bool(res.get("ok"))
                flow.log_lines.append(
                    f"[{datetime.now():%H:%M:%S}] [manual] 释放手臂 → "
                    f"{'成功' if ok else res.get('error')}")
                return {"label": "释放手臂", "ok": ok,
                        **({} if ok else {"error": str(res.get("error"))})}
            return {"label": label, "ok": False, "error": "不支持的动作"}
        except FlowError as exc:
            if flow.abort.is_set() or flow.reset_and_release.is_set():
                raise
            return {"label": label, "ok": False, "error": exc.message}
        except Exception as exc:
            return {"label": label, "ok": False, "error": str(exc)}


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
        pointcloud = None
        if not _args.no_pointcloud:
            pointcloud = PointcloudClient(_args.pointcloud)
            if not pointcloud.alive():
                task["log"].append(f"⚠ 7005 点云服务不可达（{_args.pointcloud}），"
                                   f"取点退回旧框偏移法")
                pointcloud = None
        alignment = load_alignment_config()
        coarse = alignment["coarse"]
        fine = alignment["fine"]
        task["log"].append(
            "腰部对齐配置："
            f"抬手前目标 {coarse['target_deg']:+.1f}°、验收 "
            f"{coarse['accept_min_deg']:+.1f}°~{coarse['accept_max_deg']:+.1f}°；"
            f"抬手后目标 {fine['target_deg']:+.1f}°、验收 "
            f"{fine['accept_min_deg']:+.1f}°~{fine['accept_max_deg']:+.1f}°"
        )

        # 能力注册表（18000 配置）：激活组合下该任务的实现方式参数与起手式正则
        capability = _capability_for_kind(task.get("site") or "lab",
                                          task.get("kind") or "")
        capability_kwargs: dict[str, Any] = {}
        if capability is not None:
            params = capability["method_params"]
            capability_kwargs = {
                "sidestep_cm": params["sidestep_cm"],
                "push_force_n": params["push_force_n"],
                "push_hold_s": params["push_hold_s"],
                "sidestep_down_deg": params["down_deg"],
                "pose_pattern": capability["assets"]["pose_pattern"] or None,
            }
            task["log"].append(
                f"能力配置「{capability['task']['name']}·"
                f"{capability['method']}」({capability['id']})："
                f"横移 {params['sidestep_cm']:g}cm、推力 "
                f"{params['push_force_n']:g}N、保持 {params['push_hold_s']:g}s、"
                f"下倾 {params['down_deg']:g}°")
        # 起手式认领（18000 配置，严格）：只允许本次解析出的能力条目认领
        # 过的动作参与选档——拨/扭各认各的，互不污染。没解析出条目时为
        # None（flow 按旧行为不过滤）。
        registry = _capability_registry()
        claimed_names: list[str] | None = None
        if capability is not None and registry is not None:
            claimed_names = claimed_sequence_names(registry,
                                                   capability["id"])
            task["log"].append(
                f"起手式认领：条目 {capability['id']} 共认领 "
                f"{len(claimed_names)} 个动作")
        flow = SwitchFlow(client=ReachClient(_args.reach_base),
                          console=console, yolo=yolo,
                          claimed_pose_names=claimed_names,
                          **capability_kwargs,
                          coarse_target_deg=coarse["target_deg"],
                          coarse_accept_min_deg=coarse["accept_min_deg"],
                          coarse_accept_max_deg=coarse["accept_max_deg"],
                          coarse_command_tol_deg=coarse["command_tolerance_deg"],
                          fine_target_deg=fine["target_deg"],
                          fine_accept_min_deg=fine["accept_min_deg"],
                          fine_accept_max_deg=fine["accept_max_deg"],
                          fine_command_tol_deg=fine["command_tolerance_deg"],
                          max_flip_rounds=int(task.get("retries") or 3),
                          site=task.get("site") or "lab",
                          flip_kind=task.get("kind"),
                          pointcloud=pointcloud,
                          push_force_n=float(
                              task.get("push_force_n", DEFAULT_PUSH_FORCE_N)
                          ),
                          lift_base_m=(task.get("lift_mm")
                                       or DEFAULT_LIFT_MM)["base"] / 1000.0,
                          lift_step_m=(task.get("lift_mm")
                                       or DEFAULT_LIFT_MM)["step"] / 1000.0,
                          lift_max_m=(task.get("lift_mm")
                                      or DEFAULT_LIFT_MM)["max"] / 1000.0,
                          target_offset_wall_m=tuple(
                              v / 1000.0 for v in
                              (task.get("target_offset_wall_mm")
                               or [0.0, 0.0, 0.0])),
                          target_offset_keyframes=[
                              {
                                  "distance_m": frame["distance_m"],
                                  "offset_wall_m": [
                                      frame["offset_mm"][axis] / 1000.0
                                      for axis in ("x", "y", "z")
                                  ],
                              }
                              for frame in (
                                  task.get("target_offset_keyframes") or []
                              )
                          ],
                          target_offset_preset_name=(
                              task.get("offset_preset_name") or ""
                          ),
                          first_round_offset_wall_m=tuple(
                              v / 1000.0 for v in
                              (task.get("first_round_offset_wall_mm")
                               or [0.0, 0.0, 0.0])))
        task["flow"] = flow
        if task.get("manual"):
            gate = _ManualGate(task)
            task["gate"] = gate
            flow.gate = gate
            task["log"].append("手动模式：每个主要步骤执行前都会在网页上等待确认")
        if _estop.is_set():
            # 强制停止卡在"拉起 reach"和"建流程"之间时，别让流程真的跑起来
            flow.request_abort()
        if task.get("reset_requested"):
            flow.request_reset_and_release()
            if task.get("reset_result") is not None:
                flow.finish_reset_and_release(task["reset_result"])
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
        with _lock:
            task["state"] = "done"
            task["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _count_finished_task_locked(task)


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
CHECK_SCENE_CLASSES = SCENE_CLASSES      # 开关物理指向类别（左/右）
# 人读文案用的任务语义标签（就地/远方）；YOLO 比对用 FLIP_KIND_STATES
CHECK_KIND_STATES = KIND_LABELS


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
    # YOLO 识别的是开关物理指向（左/右），任何现场一致，
    # 直接按任务的前/后指向类别比对即可。
    site = check.get("site") or "lab"
    intent = resolve_flip_intent(site, kind)
    pre_cls, post_cls = intent["flip_from"], intent["flip_to"]
    lo, hi = CHECK_BOX_BAND
    steps.append({"step": 4, "name": "YOLO 状态与居中", "band_ratio": [lo, hi],
                  "site": site,
                  "expect_pre": pre_cls, "expect_post": post_cls,
                  "direction": intent["direction"]})
    if _args.no_yolo:
        return fail("调度启动时带了 --no-yolo，无法做该项检查")
    scene = YoloClient(_args.yolo).scene()
    if not scene.get("ok"):
        return fail(f"YOLO 检测失败: {scene.get('error')}")
    cands = [b for b in scene.get("boxes", [])
             if b.get("name") in CHECK_SCENE_CLASSES]
    if not cands:
        return fail("画面里没识别到开关指向（左/右）的框")
    best = max(cands, key=lambda b: b["conf"])
    if best["name"] == post_cls:
        steps[-1].update({"scene": best["name"], "conf": best["conf"]})
        ok_step(f"识别到「{best['name']}」——开关已在目标状态「{post_cls}」，"
                f"无需拨动")
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
    site, _site_source = _resolve_site(body, _current_defaults())
    if site is None:
        return JSONResponse(
            {"ok": False, "error": "site 只能是 lab（实验室柜）或 factory（工厂柜）"},
            status_code=422)
    intent = resolve_flip_intent(site, kind)
    if not _kind_supported(site, kind):
        return JSONResponse(
            {"ok": False, "error": _unsupported_message(kind, site)},
            status_code=422,
        )
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
        _check = {"state": "running", "log": [], "steps": [], "site": site,
                  "reach_proc": None, "reach_log": None,
                  "started_at": datetime.now().isoformat(timespec="seconds")}
        check = _check
        check["log"].append(
            f"指令: {language}（{kind}，{intent['flip_from']}→{intent['flip_to']}，"
            f"{'向左拨' if intent['direction'] == 'rtl' else '向右拨'}，"
            f"现场 {SITE_LABELS[site]}）"
        )
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

# 现场（site）只决定该柜验证过哪些动作，不再影响方向：方向由 kind 唯一
# 决定（远方→就地=向左拨，就地→远方=向右拨），YOLO 按物理指向识别，
# 两柜一致。工厂柜双向已验证；实验室柜只验证过向左拨（rtl）。
SITES = ("lab", "factory")
SITE_LABELS = {"lab": "实验室柜", "factory": "工厂柜"}
# 旧的硬编码支持表：仅在能力注册表不可用（读失败/未设激活组合）时兜底。
# 正常路径由注册表的激活组合 + 已启用能力推导（种子内容与本表一致）。
SITE_SUPPORTED_KINDS = {
    "lab": frozenset({"remote_to_close"}),
    "factory": frozenset({"close_to_remote", "remote_to_close"}),
}


def _kind_supported(site: str, kind: str) -> bool:
    registry = _capability_registry()
    if registry is None or not registry.get("active"):
        return kind in SITE_SUPPORTED_KINDS.get(site, ())
    cap = _capability_for_kind(site, kind)
    return cap is not None and cap["method"] in IMPLEMENTED_METHODS


def _supported_kinds(site: str) -> list[str]:
    return [kind for kind in FLIP_KIND_STATES if _kind_supported(site, kind)]


def _parse_language(text: str) -> str | None:
    norm = " ".join(text.lower().replace(".", " ").split())
    return LANGUAGE_TASKS.get(norm)


def _current_defaults() -> dict:
    """读外部调用默认配置；文件损坏时按出厂默认走，别拦任务。"""
    try:
        return load_dispatch_defaults()
    except ValueError as exc:
        print(f"[dispatch] 默认配置读取失败，按出厂默认: {exc}")
        return deepcopy(DEFAULT_DISPATCH_DEFAULTS)


def _resolve_site(body: dict | None, defaults: dict) -> tuple[str | None, str]:
    """现场判定：请求显式给了 site 用请求的，否则用默认配置。"""
    raw = str((body or {}).get("site") or "").strip().lower()
    if not raw:
        return defaults["defaults"]["site"], "默认配置"
    return (raw if raw in SITES else None), "请求指定"


def _resolve_lift(body: dict | None, defaults: dict) -> tuple[dict, str]:
    """拨点上抬判定：请求显式给了 lift_mm 用请求的，否则用默认配置。

    {"base":首轮,"step":每轮递增,"max":封顶}（mm）。不再按距柜面远近区分。
    """
    raw = (body or {}).get("lift_mm")
    if raw is not None:
        return validate_lift_mm(raw), "请求指定"
    saved = defaults["defaults"].get("lift_mm") or DEFAULT_LIFT_MM
    return dict(saved), "默认配置"


def _resolve_push_force(
    body: dict | None,
    defaults: dict,
    kind: str,
) -> tuple[float, str]:
    """请求显式推力优先，否则按拨动方向读取 17001 默认值。"""
    if (body or {}).get("push_force_n") is not None:
        return (
            validate_push_force_n((body or {}).get("push_force_n")),
            "请求指定",
        )
    saved_by_kind = defaults["defaults"].get("push_force_n_by_kind") or {}
    saved = saved_by_kind.get(
        kind,
        defaults["defaults"].get("push_force_n"),
    )
    pre, post = CHECK_KIND_STATES[kind]
    return (
        validate_push_force_n(saved),
        f"「{pre}→{post}」默认配置",
    )


def _resolve_offset_spec(
    body: dict | None, defaults: dict, kind: str
) -> dict[str, Any]:
    """Resolve a static offset or a named distance-keyframe preset."""
    raw = (body or {}).get("target_offset_wall_mm")
    if raw is not None:
        return {
            "mode": "static",
            "offset_mm": _parse_target_offset(raw),
            "keyframes": [],
            "preset_name": "",
            "source": "请求指定",
        }

    requested_preset = str(
        (body or {}).get("target_offset_preset") or ""
    ).strip()
    if requested_preset:
        name = requested_preset
        source_prefix = "请求指定偏移配置"
    else:
        by_kind = defaults["defaults"].get("offset_preset_by_kind") or {}
        name = by_kind.get(kind) or ""
        source_prefix = "默认偏移配置"
    preset = find_offset_preset(defaults, name) if name else None
    if preset is None:
        if requested_preset:
            raise ValueError(f"没有偏移配置「{requested_preset}」")
        return {
            "mode": "static",
            "offset_mm": (0.0, 0.0, 0.0),
            "keyframes": [],
            "preset_name": "",
            "source": "无（默认配置未选偏移）",
        }
    mode = preset.get("mode") or (
        "keyframes" if preset.get("keyframes") is not None else "static"
    )
    pre, post = CHECK_KIND_STATES[kind]
    source = f"「{pre}→{post}」{source_prefix}「{name}」"
    if mode == "keyframes":
        return {
            "mode": "keyframes",
            "offset_mm": (0.0, 0.0, 0.0),
            "keyframes": deepcopy(preset.get("keyframes") or []),
            "preset_name": name,
            "source": source,
        }
    off = preset["offset_mm"]
    return {
        "mode": "static",
        "offset_mm": (off["x"], off["y"], off["z"]),
        "keyframes": [],
        "preset_name": name,
        "source": source,
    }


def _resolve_offset(
    body: dict | None, defaults: dict, kind: str
) -> tuple[tuple[float, float, float], str]:
    """Compatibility wrapper for callers that only need a static tuple."""
    spec = _resolve_offset_spec(body, defaults, kind)
    return spec["offset_mm"], spec["source"]


def _resolve_first_round_offset(
    body: dict | None, defaults: dict, kind: str
) -> tuple[tuple[float, float, float], str]:
    """首轮额外墙面系偏置：请求显式值优先，否则按任务方向读取默认。"""
    raw = (body or {}).get("first_round_offset_wall_mm")
    if raw is not None:
        return _parse_target_offset(raw), "请求指定"
    by_kind = (
        defaults["defaults"].get("first_round_offset_wall_mm_by_kind") or {}
    )
    saved = by_kind.get(kind) or {}
    pre, post = CHECK_KIND_STATES[kind]
    return (
        _parse_target_offset(saved),
        f"「{pre}→{post}」首轮默认偏置",
    )


# 目的点人工微调（墙面系）单轴上限：微调是给毫米级落点纠偏用的，
# 超过这个量说明算法/标定有问题，该修根源而不是硬掰
TARGET_OFFSET_LIMIT_MM = 100.0


def _parse_target_offset(value: Any) -> tuple[float, float, float]:
    """解析 {"x":右,"y":入墙,"z":上}（mm），缺省 0；越限抛 ValueError。"""
    if value is None:
        return (0.0, 0.0, 0.0)
    if not isinstance(value, dict):
        raise ValueError("target_offset_wall_mm 必须是 {x,y,z} 对象（单位 mm）")
    out = []
    for key in ("x", "y", "z"):
        try:
            v = float(value.get(key) or 0.0)
        except (TypeError, ValueError):
            raise ValueError(f"target_offset_wall_mm.{key} 必须是数字（mm）")
        if not math.isfinite(v) or abs(v) > TARGET_OFFSET_LIMIT_MM:
            raise ValueError(
                f"target_offset_wall_mm.{key} 超范围：单轴限 "
                f"±{TARGET_OFFSET_LIMIT_MM:g} mm（收到 {v}）")
        out.append(v)
    return tuple(out)


def _unsupported_message(kind: str, site: str) -> str:
    pre, post = CHECK_KIND_STATES[kind]
    supported = "、".join(
        f"「{CHECK_KIND_STATES[item][0]} → {CHECK_KIND_STATES[item][1]}」"
        for item in _supported_kinds(site)
    ) or "（无——激活组合下没有该柜已启用的能力）"
    return (f"「{pre} → {post}」在{SITE_LABELS[site]}上"
            f"尚未真机验证；该柜当前支持 {supported}")


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
    manual = bool((body or {}).get("manual"))
    defaults = _current_defaults()
    site, site_source = _resolve_site(body, defaults)
    if site is None:
        return JSONResponse(
            {"ok": False, "error": "site 只能是 lab（实验室柜）或 factory（工厂柜）"},
            status_code=422)
    intent = resolve_flip_intent(site, kind)
    try:
        offset_spec = _resolve_offset_spec(body, defaults, kind)
        offset_mm = offset_spec["offset_mm"]
        offset_source = offset_spec["source"]
        offset_keyframes = offset_spec["keyframes"]
        first_offset_mm, first_offset_source = _resolve_first_round_offset(
            body, defaults, kind
        )
        lift_mm, lift_source = _resolve_lift(body, defaults)
        push_force_n, push_force_source = _resolve_push_force(
            body,
            defaults,
            kind,
        )
        offsets_to_check = [
            (offset_mm, None)
        ] if not offset_keyframes else [
            (
                tuple(frame["offset_mm"][axis] for axis in ("x", "y", "z")),
                frame["distance_m"],
            )
            for frame in offset_keyframes
        ]
        for base_mm, distance in offsets_to_check:
            first_total_mm = tuple(
                base_mm[index] + first_offset_mm[index]
                for index in range(3)
            )
            for index, axis in enumerate(("右", "入墙", "上")):
                if abs(first_total_mm[index]) > TARGET_OFFSET_LIMIT_MM:
                    distance_note = (
                        f"（关键帧 {distance:.2f} m）" if distance is not None
                        else ""
                    )
                    raise ValueError(
                        f"首轮{axis}方向合计偏置超范围{distance_note}：单轴限 "
                        f"±{TARGET_OFFSET_LIMIT_MM:g} mm"
                        f"（基础 {base_mm[index]:g} + 首轮额外 "
                        f"{first_offset_mm[index]:g} = {first_total_mm[index]:g}）"
                    )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    with _lock:
        if _task is not None and _task["state"] != "done":
            _task_stats["rejected_busy"] += 1
            return JSONResponse(
                {"ok": False, "error": "已有任务在执行",
                 "task_id": _task["id"], "state": _task["state"]},
                status_code=409)
        if _check is not None and _check["state"] == "running":
            _task_stats["rejected_busy"] += 1
            return JSONResponse(
                {"ok": False, "error": "站位检查（/check/flip）执行中，请等它返回再触发任务"},
                status_code=409)
        _estop.clear()     # 新任务开始，清掉上一次的强制停止标记
        now = datetime.now().isoformat(timespec="seconds")
        offset_note = (f"，目的点微调 右{offset_mm[0]:+g}/上{offset_mm[2]:+g}"
                       f"/入墙{offset_mm[1]:+g} mm（{offset_source}）"
                       if any(offset_mm) else "")
        if offset_keyframes:
            offset_note = (
                f"，距离偏移关键帧「{offset_spec['preset_name']}」"
                f"（{len(offset_keyframes)} 帧，{offset_source}）"
            )
        first_offset_note = (
            f"，首轮额外偏置 右{first_offset_mm[0]:+g}/"
            f"上{first_offset_mm[2]:+g}/入墙{first_offset_mm[1]:+g} mm"
            f"（{first_offset_source}）"
            if any(first_offset_mm) else ""
        )
        lift_note = (f"，拨点上抬 首轮{lift_mm['base']:g}"
                     f"/每轮+{lift_mm['step']:g}"
                     f"/封顶{lift_mm['max']:g} mm（{lift_source}）")
        push_force_note = (
            f"，拨动推力 {push_force_n:g} N（{push_force_source}）"
        )
        _task = {"id": uuid.uuid4().hex[:10], "state": "starting",
                 "language": language, "kind": kind, "retries": retries,
                 "manual": manual, "site": site,
                 "direction": intent["direction"],
                 "flip_from": intent["flip_from"], "flip_to": intent["flip_to"],
                 "prompt": None, "gate": None,
                 "target_offset_wall_mm": list(offset_mm),
                 "target_offset_keyframes": deepcopy(offset_keyframes),
                 "offset_mode": offset_spec["mode"],
                 "offset_preset_name": offset_spec["preset_name"],
                 "offset_source": offset_source,
                 "first_round_offset_wall_mm": list(first_offset_mm),
                 "first_round_offset_source": first_offset_source,
                 "lift_mm": dict(lift_mm), "lift_source": lift_source,
                 "push_force_n": push_force_n,
                 "push_force_source": push_force_source,
                 "started_at": now, "finished_at": None,
                 "result": None, "flow": None,
                 "reset_requested": False, "reset_result": None,
                 "log": [f"指令: {language}（{kind}，"
                         f"{intent['flip_from']}→{intent['flip_to']}，"
                         f"{'向左拨' if intent['direction'] == 'rtl' else '向右拨'}，"
                         f"现场 {SITE_LABELS[site]}"
                         f"·{site_source}，最多 {retries} 轮"
                         f"{'，手动确认模式' if manual else ''}"
                         f"{offset_note}{first_offset_note}{lift_note}"
                         f"{push_force_note}）"],
                 "reach_proc": None, "reach_external": False,
                 "stats_counted": False}
        _task_stats["accepted"] += 1
        if not _kind_supported(site, kind):
            # 该柜没验证过这个方向，快速失败不启动硬件
            # ——平台仍按统一的轮询路径拿到结果，错误码 NOT_IMPLEMENTED
            _task["state"] = "done"
            _task["finished_at"] = now
            _task["result"] = {
                "ok": False, "code": 1, "code_name": "NOT_IMPLEMENTED",
                "message": _unsupported_message(kind, site),
                "detail": {}}
            _count_finished_task_locked(_task)
            return {"ok": True, "task_id": _task["id"]}
        threading.Thread(target=_run_task, args=(_task,), daemon=True).start()
        return {"ok": True, "task_id": _task["id"]}


@app.get("/task/status")
def task_status():
    with _lock:
        t = _task
        service = _service_stats_locked()
        check = None if _check is None else {
            "state": _check.get("state"),
            "started_at": _check.get("started_at"),
            "result": _check.get("result"),
        }
        prompt = None if t is None else t.get("prompt")
    common = {
        "ok": True,
        "service": service,
        "check": check,
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }
    if t is None:
        return {**common, "state": "idle", "task_id": None,
                "reach_alive": _reach_alive(0.5), "log": [], "result": None,
                "manual": False, "prompt": None, "step_times": []}
    flow: SwitchFlow | None = t.get("flow")
    log = list(t["log"]) + (list(flow.log_lines) if flow is not None else [])
    return {**common, "state": t["state"], "task_id": t["id"],
            "language": t.get("language"), "retries": t.get("retries"),
            "site": t.get("site") or "lab",
            "kind": t.get("kind"), "direction": t.get("direction"),
            "flip_from": t.get("flip_from"), "flip_to": t.get("flip_to"),
            "target_offset_wall_mm": t.get("target_offset_wall_mm")
                                     or [0.0, 0.0, 0.0],
            "target_offset_keyframes": deepcopy(
                t.get("target_offset_keyframes") or []
            ),
            "offset_mode": t.get("offset_mode") or "static",
            "offset_preset_name": t.get("offset_preset_name") or "",
            "effective_target_offset_wall_mm": (
                [
                    value * 1000.0
                    for value in flow.target_offset_wall_m
                ]
                if flow is not None else None
            ),
            "offset_interpolation": deepcopy(
                getattr(flow, "_target_offset_interpolation", None)
            ),
            "offset_source": t.get("offset_source") or "",
            "first_round_offset_wall_mm":
                t.get("first_round_offset_wall_mm") or [0.0, 0.0, 0.0],
            "first_round_offset_source":
                t.get("first_round_offset_source") or "",
            "lift_mm": t.get("lift_mm") or dict(DEFAULT_LIFT_MM),
            "lift_source": t.get("lift_source") or "",
            "push_force_n": t.get("push_force_n", DEFAULT_PUSH_FORCE_N),
            "push_force_source": t.get("push_force_source") or "",
            "started_at": t["started_at"], "finished_at": t["finished_at"],
            "reach_alive": _reach_alive(0.5),
            "manual": bool(t.get("manual")), "prompt": prompt,
            "step_times": flow.step_report() if flow is not None else [],
            "result": t["result"], "log": log[-120:]}


@app.post("/task/decision")
def task_decision(body: dict | None = None):
    """手动模式：对当前等待确认的步骤提交决定。

    Body: {"prompt_id": "...",          # 可选，防过期误提交
           "action": "proceed" | "abort" | "goto_waypoint" | "arm" | "disarm",
           "waypoint": "位点名"}         # 仅 goto_waypoint 需要
    """
    with _lock:
        t = _task
        gate: _ManualGate | None = None if t is None else t.get("gate")
    if t is None or t["state"] == "done":
        return JSONResponse({"ok": False, "error": "没有正在执行的任务"},
                            status_code=409)
    if gate is None:
        return JSONResponse({"ok": False, "error": "当前任务不是手动确认模式"},
                            status_code=409)
    result = gate.submit(body or {})
    if not result.get("ok"):
        return JSONResponse(result, status_code=409)
    return result


@app.get("/manual/waypoints")
def manual_waypoints():
    """给手动模式"前往位点"下拉框用的已录路点列表（透传 18001）。"""
    if not _reach_alive(1.5):
        return JSONResponse(
            {"ok": False, "error": "reach_server 未运行，无法读取位点列表"},
            status_code=503)
    try:
        res = ReachClient(_args.reach_base).waypoints()
        names = [str(w.get("name")) for w in (res.get("waypoints") or [])]
        return {"ok": True, "waypoints": names}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ---------------------- 外部调用默认配置（读 / 改 / 存） ----------------------
# 与网页手动单次测试互不干扰：手动栏总是显式带 site/偏移下发；
# 外部平台只带 language 时才套这里的默认值。持久化在
# config/dispatch_defaults.json。


@app.get("/config/defaults")
def config_defaults_get():
    """默认现场 + 默认偏移配置 + 全部命名偏移配置。"""
    cfg = _current_defaults()
    # 兼容浏览器里仍缓存着的单方向旧页面：旧页面只读取 offset_preset。
    # 对工厂柜它代表远→就，对实验室柜代表就→远；新版页面使用 by_kind。
    defaults = dict(cfg["defaults"])
    legacy_kind = resolve_flip_intent(defaults["site"])["kind"]
    defaults["offset_preset"] = (
        defaults.get("offset_preset_by_kind") or {}
    ).get(legacy_kind, "")
    defaults["push_force_n"] = (
        defaults.get("push_force_n_by_kind") or {}
    ).get(legacy_kind, DEFAULT_PUSH_FORCE_N)
    content = {
        "ok": True,
        **cfg,
        "defaults": defaults,
        "site_labels": SITE_LABELS,
        "offset_limit_mm": TARGET_OFFSET_LIMIT_MM,
        "offset_keyframe_distance": {
            "min": OFFSET_KEYFRAME_MIN_DISTANCE_M,
            "max": OFFSET_KEYFRAME_MAX_DISTANCE_M,
            "step": OFFSET_KEYFRAME_STEP_M,
        },
    }
    return JSONResponse(
        content,
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.post("/config/defaults")
def config_defaults_set(body: dict | None = None):
    """改默认值；两个任务方向可分别选择命名偏移配置。"""
    body = body or {}
    cfg = _current_defaults()
    if "site" in body:
        cfg["defaults"]["site"] = body.get("site")
    if "offset_preset_by_kind" in body:
        cfg["defaults"]["offset_preset_by_kind"] = (
            body.get("offset_preset_by_kind")
        )
    elif "offset_preset" in body:
        legacy = str(body.get("offset_preset") or "").strip()
        # 旧页面只有一个下拉框，只更新该现场原本对应的方向，避免覆盖新版
        # 页面已经为另一个方向独立保存的配置。
        by_kind = dict(cfg["defaults"].get("offset_preset_by_kind") or {})
        legacy_kind = resolve_flip_intent(cfg["defaults"]["site"])["kind"]
        by_kind[legacy_kind] = legacy
        cfg["defaults"]["offset_preset_by_kind"] = by_kind
    if "first_round_offset_wall_mm_by_kind" in body:
        cfg["defaults"]["first_round_offset_wall_mm_by_kind"] = (
            body.get("first_round_offset_wall_mm_by_kind")
        )
    if "lift_mm" in body:
        cfg["defaults"]["lift_mm"] = body.get("lift_mm")
    if "push_force_n_by_kind" in body:
        cfg["defaults"]["push_force_n_by_kind"] = (
            body.get("push_force_n_by_kind")
        )
    elif "push_force_n" in body:
        by_kind = dict(
            cfg["defaults"].get("push_force_n_by_kind") or {}
        )
        legacy_kind = resolve_flip_intent(cfg["defaults"]["site"])["kind"]
        by_kind[legacy_kind] = body.get("push_force_n")
        cfg["defaults"]["push_force_n_by_kind"] = by_kind
    try:
        saved = save_dispatch_defaults(cfg)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    return {"ok": True, **saved}


@app.post("/config/offset-presets")
def config_preset_upsert(body: dict | None = None):
    """Create or replace a static or distance-keyframe offset preset."""
    body = body or {}
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "配置名不能为空"},
                            status_code=422)
    mode = str(body.get("mode") or "static").strip().lower()
    try:
        if mode == "keyframes":
            preset = {
                "name": name,
                "mode": "keyframes",
                "keyframes": validate_offset_keyframes(body.get("keyframes")),
            }
        elif mode == "static":
            preset = {
                "name": name,
                "mode": "static",
                "offset_mm": validate_offset_mm(body.get("offset_mm")),
            }
        else:
            raise ValueError("mode 只能是 static 或 keyframes")
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    cfg = _current_defaults()
    cfg["offset_presets"] = (
        [p for p in cfg["offset_presets"] if p["name"] != name]
        + [preset])
    try:
        saved = save_dispatch_defaults(cfg)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    return {"ok": True, **saved}


@app.post("/config/offset-presets/delete")
def config_preset_delete(body: dict | None = None):
    """删除命名偏移配置；引用它的各方向默认值自动改回「无」。"""
    name = str((body or {}).get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "配置名不能为空"},
                            status_code=422)
    cfg = _current_defaults()
    remaining = [p for p in cfg["offset_presets"] if p["name"] != name]
    if len(remaining) == len(cfg["offset_presets"]):
        return JSONResponse({"ok": False, "error": f"没有配置「{name}」"},
                            status_code=404)
    cfg["offset_presets"] = remaining
    by_kind = cfg["defaults"].get("offset_preset_by_kind") or {}
    cfg["defaults"]["offset_preset_by_kind"] = {
        kind: "" if preset == name else preset
        for kind, preset in by_kind.items()
    }
    try:
        saved = save_dispatch_defaults(cfg)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    return {"ok": True, **saved}


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


ARM_RESET_WAYPOINT = "起手点测试"
ARM_RESET_SPEED_RAD_S = 0.15
ARM_RESET_STIFFNESS_SCALE = 0.5
ARM_RESET_SEGMENT_TIMEOUT_S = 60.0


def _wait_arm_idle(client: ReachClient, timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.exec_status()
        if not last.get("running"):
            return last
        time.sleep(0.1)
    client.stop()
    raise RuntimeError(f"等待机械臂停止超过 {timeout_s:g}s，已再次冻结")


def _reset_arm_via_waypoints(
    client: ReachClient, waypoint_names: list[str]
) -> list[str]:
    """以半刚度、低速逐段回位；全部到位后才释放。"""
    available = {
        str(item.get("name")): item
        for item in (client.waypoints().get("waypoints") or [])
    }
    actions: list[str] = []
    for name in waypoint_names:
        target = available.get(name)
        if target is None:
            raise RuntimeError(f"找不到安全复位路点「{name}」")
        joints = client.joints()
        if not joints.get("ok"):
            raise RuntimeError(f"读取当前关节失败: {joints.get('error')}")
        current = joints["named_joints"]
        end = target["named_joints"]
        travel = max(
            abs(float(end[key]) - float(current.get(key, 0.0)))
            for key in end
        )
        duration = max(2.0, travel / ARM_RESET_SPEED_RAD_S * 1.2)
        started = client.execute(
            waypoints=[current, end],
            duration=duration,
            max_speed_rad_s=ARM_RESET_SPEED_RAD_S,
            stiffness_scale=ARM_RESET_STIFFNESS_SCALE,
            label=f"arm_reset_{name}"[:32],
        )
        if not started.get("ok"):
            raise RuntimeError(
                f"回「{name}」被拒: {started.get('error') or started}"
            )
        ended = _wait_arm_idle(client, ARM_RESET_SEGMENT_TIMEOUT_S)
        message = str(ended.get("message") or "")
        if any(word in message for word in ("中止", "出错", "急停")):
            raise RuntimeError(f"回「{name}」未完成: {message}")
        actions.append(
            f"50%刚度低速到「{name}」"
            f"（Kp×{ARM_RESET_STIFFNESS_SCALE:g}，"
            f"≤{ARM_RESET_SPEED_RAD_S:g}rad/s）"
        )
    released = client.disarm()
    if not released.get("ok"):
        raise RuntimeError(f"释放手臂失败: {released.get('error') or released}")
    actions.append("释放手臂")
    return actions


@app.post("/arm/stop")
def arm_stop():
    """立即冻结当前动作、中断流程，再半刚度低速回安全点并释放。

    左-起手式沿用防碰柜路径：先回配套终点，再到「起手点测试」。
    任一回位段失败都保持接管，不直接释放。
    """
    if not _reach_alive(1.5):
        return JSONResponse(
            {"ok": False,
             "error": "reach_server 未在运行，手臂本来就不受我们控制"},
            status_code=409)
    with _lock:
        task = _task
        flow: SwitchFlow | None = (
            task.get("flow")
            if task is not None and task.get("state") != "done"
            else None
        )
        if task is not None and task.get("state") != "done":
            task["reset_requested"] = True
            task["prompt"] = None
    if flow is not None:
        flow.request_reset_and_release()

    result: dict[str, Any] = {
        "ok": False,
        "arm_released": False,
        "actions": [],
    }
    try:
        client = ReachClient(_reach_base())
        try:
            _http.post(
                f"{_reach_base()}/api/reach/align_yaw",
                json={"stop": True},
                timeout=3.0,
            )
            result["actions"].append("停止腰部对中")
        except requests.RequestException:
            result["actions"].append("停止腰部对中请求失败")

        stopped = client.stop()
        if not stopped.get("ok"):
            raise RuntimeError(
                f"冻结当前轨迹失败: {stopped.get('error') or stopped}"
            )
        result["actions"].append("立即冻结当前机械臂轨迹")
        _wait_arm_idle(client, 5.0)

        route = (
            flow.safe_reset_waypoints()
            if flow is not None
            else [ARM_RESET_WAYPOINT]
        )
        result["actions"].extend(_reset_arm_via_waypoints(client, route))
        result.update(
            ok=True,
            arm_released=True,
            message="当前流程已中断；机械臂已低刚度回到起手点测试并释放",
            route=route,
        )
    except Exception as exc:
        result.update(
            error=str(exc),
            message=(
                "当前流程已中断，但机械臂未能完成安全复位；"
                "为安全起见保持接管，请人工处置"
            ),
        )
    finally:
        with _lock:
            if task is not None and task.get("state") != "done":
                task["reset_result"] = dict(result)
                task["log"].append(
                    "🖐 机械臂复位并释放："
                    + (
                        "；".join(result["actions"])
                        if result.get("ok")
                        else f"失败，保持接管（{result.get('error')}）"
                    )
                )
        if flow is not None:
            flow.finish_reset_and_release(result)
    return result


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
    return FileResponse(
        WEB_DIR / "dispatch.html",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/info")
def service_info():
    ctx = _active_arm_context()
    capability = None
    if ctx is not None:
        capability = {
            "arm": ctx["arm"],
            "hand": ctx["hand_name"],
            "calib_status": ctx["calib_status"],
            "supported_kinds_by_site": {
                site: _supported_kinds(site) for site in SITES
            },
        }
    return {"service": "flip-dispatch",
            "usage": {"check": 'POST /check/flip  body={"language": "..."}'
                               '（站位检查，同步，客户端超时建议 ≥300s）',
                      "start": 'POST /task/flip  body={"language": "..."}',
                      "status": "GET /task/status",
                      "abort": "POST /task/abort",
                      "arm_stop": "POST /arm/stop（中断流程→半刚度安全回位→释放）",
                      "estop": "POST /emergency/stop（任何状态：急停+释放手臂）"},
            "languages": ["Change the switch from close to remote",
                          "Change the switch from remote to close"],
            "capability": capability}


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
    parser.add_argument("--pointcloud", default="http://127.0.0.1:7005",
                        help="7005 语义点云服务地址（取点算法）")
    parser.add_argument("--no-pointcloud", action="store_true",
                        help="不用点云算法取点，退回 YOLO 框偏移法")
    parser.add_argument("--capability-url", default=DEFAULT_CAPABILITY_URL,
                        help="18000 能力中心地址（启动拜访，必须可达）")
    _args = parser.parse_args()
    _args.reach_base = _args.reach_base.rstrip("/")

    # 启动拜访 18000：拉取能力注册表快照，拿不到就拒绝启动（启动脚本
    # prepare.sh 负责先把 18000 拉起来）。快照进程内一直用到退出，重启生效。
    global _capability_registry_cache, _capability_registry_loaded
    try:
        snapshot = fetch_snapshot(_args.capability_url)
    except CapabilityUnavailable as exc:
        print(f"[dispatch] 启动拜访 18000 失败：{exc}")
        raise SystemExit(1)
    _capability_registry_cache = snapshot["registry"]
    _capability_registry_loaded = True

    print(f"[dispatch] 调度服务已启动（常驻属正常）: http://{_lan_ip()}:{_args.port}/")
    print(f"[dispatch] 18000 {describe_active(snapshot)}"
          "；可接任务由 18000 注册表推导")
    print(f"[dispatch] 外部触发: POST /task/flip （body 带 language）→ 轮询 GET /task/status")
    print(f"[dispatch] reach_server 按需拉起: {sys.executable} reach_server.py "
          f"--port {_args.reach_port} --camera-source zmq "
          f"--camera-host {_args.camera_host} --network-interface {_args.network_interface} "
          f"--calib {_args.calib} --tool-out-mm {_args.tool_out_mm:g}")
    uvicorn.run(app, host=_args.host, port=_args.port, log_level="warning")


if __name__ == "__main__":
    main()
