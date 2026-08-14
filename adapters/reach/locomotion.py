"""本体转身与对中：/perpendicular 观测、/turn 点动/按住、/align_yaw 闭环伺服。

腰部电机不受 rt/arm_sdk 混合通道控制，yaw 调整走高层 LocoClient.SetVelocity
让本体运控用腿原地转（详见 /turn 的 docstring）。
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime
from typing import Any

import numpy as np
from fastapi.responses import JSONResponse

from .execution import _tcp_position
from .perception import _fit_view_plane
from .state import _read_joints, _read_torso, router, state


# --------------- 垂直度观测 + 腰 yaw 点动（perp.html 调试页） ---------------


@router.get("/perpendicular")
def reach_perpendicular(dmin: float = 0.3, dmax: float = 1.0):
    """用 [dmin, dmax] 深度范围内的点拟合柜面平面，给出垂直度指标。

    深度不在该范围内的点视为异常（地面、远处背景、手臂等），不参与拟合。
    yaw_err_deg > 0 表示法线偏向画面右侧（柜面左边更远）；两个角都为 0
    即相机光轴与柜面严格垂直。
    """
    if not state.enabled:
        return JSONResponse({"ok": False, "error": "reach 未启用"}, status_code=409)
    _hold_check_stale()   # 顺带收尾心跳断掉的按住会话（见操作记录一节）
    out = _fit_view_plane(dmin, dmax)
    out["torso"] = _read_torso()
    out["turn_available"] = state.loco_available and state.handeye_ready
    out["align"] = {"running": state.align_running, "message": state.align_message}
    out["hold_record"] = {"active": _hold_group is not None,
                          "group": _hold_group["name"] if _hold_group else None}
    return out


TURN_RATE_DEG_S = 6.0      # 原地转身角速度
TURN_MAX_DEG = 10.0        # 单次点动上限
# 按住键盘连续转身：每次心跳发一个这么长的速度脉冲，前端 ~0.3s 心跳一次，
# 脉冲之间相互覆盖 → 连续转；心跳断了（松键/断网/页面崩）固件转完残余
# 脉冲即自动停 —— 等价于摇杆的"松手即停"死人开关。
TURN_HOLD_PULSE_S = 0.8
TURN_HOLD_RATE_DEG_S = 12.0        # 按住模式默认转速（前端可传 rate_deg_s 覆盖）
TURN_HOLD_RATE_RANGE = (2.0, 30.0)  # 前端可调范围；点动/对中仍用上面验证过的 6°/s

# --------------- 按住转身的操作记录（为自动纠偏学习采数据） ---------------
# 每次"按住→松开"落一条样本到 logs/reach/hold_<日期>.jsonl：
# 按住前/松开稳定后的柜面偏航角、方向、速度、时长、距离、腰角。
# 攒够人工纠偏样本后，用它学"人打杆的习惯"（多大偏差按多久、何时松手），
# 再写成自动纠偏。
HOLD_LOG_SETTLE_S = 1.0   # 松开后等运控/相机稳定再测 after（同 ALIGN_SETTLE_S 道理）
HOLD_STALE_S = 1.5        # 心跳断了这么久还没收到 stop → 按"超时"收尾该会话

_hold_lock = threading.Lock()
_hold_session: dict | None = None
_hold_group: dict | None = None    # 前端"开始记录"创建的分组 {"name": str}，写进每条样本


def _hold_measure(dmin: float, dmax: float) -> dict:
    """记录用的轻量测量：柜面偏航角 + 距离 + 腰关节。失败字段置 None，不抛。"""
    out: dict[str, Any] = {"yaw_err_deg": None, "distance_m": None}
    try:
        fit = _fit_view_plane(dmin, dmax)
        if fit.get("ok"):
            out["yaw_err_deg"] = round(float(fit["yaw_err_deg"]), 3)
            out["distance_m"] = round(float(fit["distance_m"]), 3)
    except Exception:
        pass
    try:
        torso = _read_torso()
        if torso and torso.get("waist_rad"):
            out["waist_deg"] = [round(math.degrees(v), 3) for v in torso["waist_rad"]]
    except Exception:
        pass
    return out


def _hold_log(entry: dict) -> None:
    """按住转身操作日志：logs/reach/hold_<日期>.jsonl。"""
    try:
        state.log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(timespec="milliseconds"),
                 "session": state.session_id,
                 # 抬手状态两份都记：ui = 前端人工勾选，auto = 按 TCP 前伸自动判
                 "hand_up_ui": bool(state.hand_raised_ui),
                 "arm_raised_auto": _arm_raised(),
                 **entry}
        path = state.log_dir / f"hold_{datetime.now():%Y%m%d}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _hold_note_beat(direction: float, rate: float, dmin: float, dmax: float) -> None:
    """心跳到达：开新会话（先测 before）或延续当前会话。方向反打视为新会话。"""
    global _hold_session
    now = time.monotonic()
    with _hold_lock:
        sess = _hold_session
        if sess is not None:
            stale = now - sess["last_beat"] > HOLD_STALE_S
            if stale or sess["dir"] != direction:
                _hold_close_locked("timeout" if stale else "reversed")
                sess = None
        if sess is None:
            _hold_session = {
                "dir": direction, "rate": rate, "dmin": dmin, "dmax": dmax,
                "t0": now, "last_beat": now, "beats": 1,
                "group": _hold_group["name"] if _hold_group else None,
                "before": _hold_measure(dmin, dmax),
            }
        else:
            sess["last_beat"] = now
            sess["beats"] += 1
            sess["rate"] = rate    # 中途调速：记最后生效的值


def _hold_close(reason: str) -> None:
    with _hold_lock:
        _hold_close_locked(reason)


def _hold_close_locked(reason: str) -> None:
    """收尾当前会话（须持有 _hold_lock）：后台延时测 after 并落盘。"""
    global _hold_session
    sess = _hold_session
    _hold_session = None
    if sess is None:
        return
    now = time.monotonic()
    if reason == "stop":
        duration = now - sess["t0"]              # 松键即停：按 stop 到达时刻算
    else:
        # 心跳断掉/反打：机器人把最后一个脉冲走完才停
        duration = sess["last_beat"] - sess["t0"] + TURN_HOLD_PULSE_S
    threading.Thread(target=_hold_write_entry,
                     args=(sess, max(0.0, duration), reason), daemon=True).start()


def _hold_write_entry(sess: dict, duration: float, reason: str) -> None:
    time.sleep(HOLD_LOG_SETTLE_S)
    after = _hold_measure(sess["dmin"], sess["dmax"])
    before = sess["before"]
    delta = None
    if before.get("yaw_err_deg") is not None and after.get("yaw_err_deg") is not None:
        delta = round(after["yaw_err_deg"] - before["yaw_err_deg"], 3)
    _hold_log({
        "event": "hold",
        "group": sess.get("group"),                    # 开始/结束记录之间的分组名
        "dir": int(sess["dir"]),                       # 1=左转 -1=右转
        "rate_deg_s": sess["rate"],
        "duration_s": round(duration, 3),
        "beats": sess["beats"],
        "end": reason,                                 # stop / timeout / reversed
        "commanded_deg": round(sess["rate"] * duration * sess["dir"], 2),
        "yaw_before_deg": before.get("yaw_err_deg"),
        "yaw_after_deg": after.get("yaw_err_deg"),
        "delta_yaw_deg": delta,
        # 相机到柜面的垂直距离，按前/后各记一份：不同距离下的调节习惯
        # 可能不同，学习时按距离分档要用
        "distance_before_m": before.get("distance_m"),
        "distance_after_m": after.get("distance_m"),
        "waist_before_deg": before.get("waist_deg"),
        "waist_after_deg": after.get("waist_deg"),
        "settle_s": HOLD_LOG_SETTLE_S,
    })


def _hold_check_stale() -> None:
    """由页面轮询顺带调用：心跳断掉又没等到 stop 的会话按超时收尾。"""
    with _hold_lock:
        sess = _hold_session
        if sess is not None and time.monotonic() - sess["last_beat"] > HOLD_STALE_S:
            _hold_close_locked("timeout")


@router.post("/hold_record")
def reach_hold_record(body: dict):
    """记录分组开关：{"start": true, "label"?: str} / {"stop": true}。

    开始后的每条 hold 样本都带 group 字段（默认组名 = 当前时刻），
    并在日志里写 record_start / record_stop 标记行，方便按组切数据。
    """
    global _hold_group
    with _hold_lock:
        if body.get("stop"):
            if _hold_group is not None:
                _hold_log({"event": "record_stop", "group": _hold_group["name"]})
                _hold_group = None
            return {"ok": True, "active": False}
        if body.get("start"):
            name = str(body.get("label") or datetime.now().strftime("%H%M%S")).strip()
            if _hold_group is not None:
                _hold_log({"event": "record_stop", "group": _hold_group["name"]})
            _hold_group = {"name": name}
            _hold_log({"event": "record_start", "group": name})
            return {"ok": True, "active": True, "group": name}
    return JSONResponse({"ok": False, "error": "需要 start 或 stop"}, status_code=400)


def _get_loco_client():
    """高层 loco RPC 客户端（懒创建）。DDS 在服务启动时已由只读订阅初始化。"""
    if state.loco_client is None:
        from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient

        c = LocoClient()
        if hasattr(c, "SetTimeout"):
            c.SetTimeout(3.0)
        c.Init()
        state.loco_client = c
    return state.loco_client


@router.post("/turn")
def reach_turn(body: dict):
    """原地转身点动（真机！全身动作）。Body: {"delta_deg": ±2} 或 {"stop": true}。

    H2 的 rt/arm_sdk 混合通道只覆盖双臂（15~28），腰电机指令会被固件忽略
    （已真机验证）；直接发 rt/lowcmd 又必须释放本体运控、机器人会失去平衡。
    所以对准柜面的 yaw 调整走高层 SetVelocity：让本体运控自己用腿原地转，
    平衡由它负责，与 arm_sdk 手臂控制可以共存（官方 VR 遥操即此组合）。
    注意：转身会带动整条手臂，请先把手收回再调。
    """
    if "hand_raised" in body:      # 前端人工标注，跟着每条日志落盘
        state.hand_raised_ui = bool(body["hand_raised"])
    if not state.loco_available:
        return JSONResponse({"ok": False, "error": "无 DDS 连接（--no-robot 模式）"},
                            status_code=409)
    if state.align_running and not body.get("stop"):
        return JSONResponse({"ok": False, "error": "一键对中进行中，先停止它"},
                            status_code=409)
    try:
        loco = _get_loco_client()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"loco 客户端初始化失败: {exc}"},
                            status_code=502)

    if body.get("stop"):
        _hold_close("stop")   # 有按住会话就收尾记录（无会话时是空操作）
        try:
            loco.StopMove()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"停止失败: {exc}"}, status_code=502)
        return {"ok": True, "stopped": True}

    # 按住模式：{"hold_dir": 1|-1}，正=左转。每次调用发一个短速度脉冲，
    # 由前端心跳维持连续性（见 TURN_HOLD_PULSE_S 注释）。
    hold_dir = body.get("hold_dir")
    if hold_dir is not None:
        try:
            direction = 1.0 if float(hold_dir) > 0 else -1.0
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "hold_dir 需为 ±1"},
                                status_code=400)
        try:
            rate = float(body.get("rate_deg_s") or TURN_HOLD_RATE_DEG_S)
        except (TypeError, ValueError):
            rate = TURN_HOLD_RATE_DEG_S
        rate = float(np.clip(rate, *TURN_HOLD_RATE_RANGE))
        # 操作记录：首拍会先测一次柜面偏航角（机器人此刻还没动），
        # dmin/dmax 用前端当前的深度范围，保证 before/after 同口径。
        _hold_note_beat(direction, rate,
                        float(body.get("dmin") or 0.4),
                        float(body.get("dmax") or 1.0))
        omega = math.radians(rate) * direction
        try:
            code = loco.SetVelocity(0.0, 0.0, omega, TURN_HOLD_PULSE_S)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"SetVelocity 失败: {exc}"},
                                status_code=502)
        if code not in (0, None, RPC_TIMEOUT_CODE):
            return JSONResponse({"ok": False, "error": f"SetVelocity 返回码 {code}"},
                                status_code=502)
        return {"ok": True, "hold_dir": int(direction),
                "omega_deg_s": math.degrees(omega), "pulse_s": TURN_HOLD_PULSE_S,
                **({"warning": "RPC 应答超时，指令可能已执行"}
                   if code == RPC_TIMEOUT_CODE else {})}

    try:
        delta = float(body["delta_deg"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "需要 delta_deg（度，正=左转）"},
                            status_code=400)
    delta = float(np.clip(delta, -TURN_MAX_DEG, TURN_MAX_DEG))
    if abs(delta) < 0.05:
        return {"ok": True, "delta_deg": 0.0, "duration_s": 0.0}
    omega = math.radians(TURN_RATE_DEG_S) * (1.0 if delta > 0 else -1.0)
    duration = abs(math.radians(delta)) / abs(omega)
    try:
        code = loco.SetVelocity(0.0, 0.0, omega, duration)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"SetVelocity 失败: {exc}"},
                            status_code=502)
    if code == RPC_TIMEOUT_CODE:
        # 应答超时 ≠ 没执行：运控忙时常见，指令多半已生效
        return {"ok": True, "delta_deg": delta, "omega_deg_s": math.degrees(omega),
                "duration_s": duration, "warning": "RPC 应答超时，指令可能已执行"}
    if code not in (0, None):
        return JSONResponse({"ok": False, "error": f"SetVelocity 返回码 {code}"},
                            status_code=502)
    return {"ok": True, "delta_deg": delta,
            "omega_deg_s": math.degrees(omega), "duration_s": duration}


# --------------- 一键对中（yaw 闭环伺服） ---------------

ALIGN_TOL_STRICT_DEG = 0.35  # 手臂收回时的收敛阈值
ALIGN_TOL_FALLBACK_DEG = 0.4  # 步数用尽时的兜底：残差在此内按"基本对中"收尾
ALIGN_TOL_RAISED_DEG = 2.8   # 手臂前伸时：运控持续配平、读数呼吸式波动，追不到 0.8
ARM_RAISED_TCP_X = 0.25      # TCP 前伸超过这个距离（米，根系）视为"手抬起来了"
ALIGN_MAX_STEPS = 15
# 脉冲幅度只用真机验证过能可靠执行的两档（人手点按收敛就是这么干的）。
# 连续伺服两次翻车的教训：转动中相机测量滞后必穿靶；腰编码器做代理，
# 停车后运控又会在腰/腿之间重分配旋转，读数对不上相机。所以：
# 测量只在静止时做，动作只用定长脉冲，简单且和人手一样快。
PULSE_BIG_DEG = 2.0        # |偏差| ≥ 1.5° 用大脉冲
PULSE_SMALL_DEG = 0.5      # 其余用小脉冲
PULSE_BIG_BELOW = 1.5
RPC_TIMEOUT_CODE = 3104    # unitree rpc：应答超时（指令多半已执行，不算失败）


def _arm_raised() -> bool:
    """手臂是否前伸（TCP 在根系向前超过阈值）。读不到关节时按未抬处理。"""
    try:
        tcp = _tcp_position([float(v) for v in _read_joints()])
        return tcp is not None and tcp[0] > ARM_RAISED_TCP_X
    except Exception:
        return False
ALIGN_SETTLE_S = 1.0       # 每步转完后等运控稳定再测


def _align_log(entry: dict) -> None:
    """对中过程逐步落盘：logs/reach/align_<日期>.jsonl，事后分析用。"""
    try:
        state.log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(timespec="milliseconds"),
                 "session": state.session_id,
                 "hand_up_ui": bool(state.hand_raised_ui),
                 "arm_raised_auto": _arm_raised(),
                 **entry}
        torso = _read_torso()
        if torso and torso.get("waist_rad"):
            entry["waist_deg"] = [round(math.degrees(v), 3)
                                  for v in torso["waist_rad"]]
        path = state.log_dir / f"align_{datetime.now():%Y%m%d}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _align_loop(tol: float, dmin: float, dmax: float,
                target: float = 0.0) -> None:
    """人手同款打法：静止测偏差 → 定长脉冲（0.5° 或 2°）→ 等稳 → 再测。

    修正方向：真机实测 yaw_err > 0（法线偏画面右）时左转（正角）是对的。
    保留自适应反号兜底：某步之后偏差反而变大就翻方向。
    target ≠ 0 时对到指定角度（全流程要求柜面指数停在 -3~-6° 带内）。
    每步的测量与动作都写入 logs/reach/align_<日期>.jsonl。
    """
    loco = _get_loco_client()
    sign = 1.0
    prev_err: float | None = None
    _align_log({"event": "start", "tol_deg": tol, "target_deg": target,
                "dmin": dmin, "dmax": dmax})
    try:
        for step in range(1, ALIGN_MAX_STEPS + 1):
            if state.align_cancel.is_set():
                state.align_message = "已中止"
                _align_log({"event": "cancelled", "step": step})
                return
            fitres = _fit_view_plane(dmin, dmax)
            if not fitres.get("ok"):
                state.align_message = f"对中失败：{fitres.get('error')}"
                _align_log({"event": "fit_fail", "step": step,
                            "error": fitres.get("error")})
                return
            yaw = float(fitres["yaw_err_deg"])
            err = yaw - target      # 相对目标角的偏差，方向约定与对 0 相同
            _align_log({"event": "measure", "step": step,
                        "yaw_err_deg": round(yaw, 3),
                        "pitch_err_deg": round(float(fitres["pitch_err_deg"]), 3),
                        "points": fitres.get("points_used")})
            if abs(err) <= tol:
                state.align_message = (f"对中完成：yaw {yaw:+.2f}°"
                                       f"（目标 {target:+.1f}°，{step - 1} 步）")
                _align_log({"event": "done", "step": step, "yaw_err_deg": round(yaw, 3)})
                return
            if prev_err is not None and abs(err) > abs(prev_err) + 0.3:
                sign = -sign     # 上一步把偏差转大了 → 方向反了
                _align_log({"event": "sign_flip", "step": step})
            prev_err = err

            size = PULSE_BIG_DEG if abs(err) >= PULSE_BIG_BELOW else PULSE_SMALL_DEG
            delta = size * (1.0 if sign * err > 0 else -1.0)
            state.align_message = f"第 {step} 步：偏差 {err:+.2f}° → 脉冲 {delta:+.1f}°"
            omega = math.radians(TURN_RATE_DEG_S) * (1.0 if delta > 0 else -1.0)
            duration = abs(math.radians(delta)) / abs(omega)
            code = loco.SetVelocity(0.0, 0.0, omega, duration)
            _align_log({"event": "pulse", "step": step, "delta_deg": delta,
                        "duration_s": round(duration, 3), "rpc_code": code})
            if code == RPC_TIMEOUT_CODE:
                # 应答超时 ≠ 没执行：运控忙（如手臂前伸配平）时常见，
                # 指令多半已生效，照常等稳再测，让闭环自己判断
                state.align_message += "（RPC 应答超时，按已执行继续）"
            elif code not in (0, None):
                state.align_message = f"对中失败：SetVelocity 返回码 {code}"
                return
            if state.align_cancel.wait(duration + ALIGN_SETTLE_S):
                state.align_message = "已中止"
                _align_log({"event": "cancelled", "step": step})
                return
        if prev_err is not None and abs(prev_err) <= max(ALIGN_TOL_FALLBACK_DEG, tol):
            state.align_message = (f"基本对中：偏差 {prev_err:+.2f}°"
                                   f"（未达 {tol}°，但已在兜底 "
                                   f"{ALIGN_TOL_FALLBACK_DEG}° 内）")
            _align_log({"event": "done_fallback", "err_deg": round(prev_err, 3)})
        else:
            state.align_message = (f"未收敛：{ALIGN_MAX_STEPS} 步后偏差仍 "
                                   f"{prev_err:+.2f}°（阈值 {tol}°）")
            _align_log({"event": "give_up",
                        "err_deg": None if prev_err is None else round(prev_err, 3)})
    except Exception as exc:
        state.align_message = f"对中异常：{exc}"
        _align_log({"event": "exception", "error": str(exc)})
    finally:
        try:
            loco.StopMove()
        except Exception:
            pass
        state.align_running = False


# --------------- 新对中（打杆式，参数学自 hold_*.jsonl 的手动纠偏数据） ---------------
# 2026-07-28 采集 60 次按键、0.41m/0.54m 两个距离，拟合结果几乎一致：
#   实际角 ≈ 有效速率 × (按住时长 − 死区)；指令 6°/s 时有效速率 3.5~4.1°/s，
#   死区 0.21~0.26s；<0.4s 的超短按响应完全随机（0.05°~1.3°）。
# 策略照抄人手的两段式，但每杆时长用模型直接解出来，不靠试探：
#   时长 = 死区 + |偏差|×GAIN / 有效速率 → 按完等稳再测 → 不够再补一杆。
HOLD_ALIGN_CMD_DEG_S = 6.0    # 指令速度：数据在这个档采的，模型只对这个档成立
HOLD_ALIGN_EFF_DEG_S = 3.8    # 有效速率（两距离拟合 4.1/3.5，取中偏保守）
HOLD_ALIGN_DEAD_S = 0.24      # 死区：短于此基本不动
HOLD_ALIGN_GAIN = 0.9         # 每杆只打目标的 9 折：宁欠勿过，过冲反打代价更大
HOLD_ALIGN_MIN_HOLD_S = 0.35  # 超短杆不打——死区附近响应是随机数，打了白打
HOLD_ALIGN_MAX_HOLD_S = 6.0   # 单杆上限（约一杆 22°，再大分两杆）
HOLD_ALIGN_MAX_STEPS = 12     # 混合模式：大偏差 2~4 杆 + 小脉冲收尾若干步
# 12:02 真机 6 轮的教训（align_20260728 第 4 轮）：小残差区间短杆响应随机
# （0.45s 预计 0.8° 实际走 1.4°），冲过头→反号兜底被随机性误触发→震荡放弃。
# 收尾策略 = 降速拉长："高速+超短时"落在死区/随机区（旧版 0.5° 脉冲折算
# 0.083s，远短于 0.24s 死区，所以只抽搐不动），改用低速+正常时长——同样
# 的角度时长翻倍且稳稳越过随机区，时间抖动折算的角度误差也减半。
# 低速档的有效速率先按 6°/s 档等比折算，待真机日志校准。
HOLD_ALIGN_FINE_DEG = 1.2         # |偏差| 小于此进入慢杆收尾
HOLD_ALIGN_FINE_CMD_DEG_S = 3.0   # 收尾指令速度（半速）
HOLD_ALIGN_FINE_EFF_DEG_S = HOLD_ALIGN_EFF_DEG_S * HOLD_ALIGN_FINE_CMD_DEG_S / HOLD_ALIGN_CMD_DEG_S
HOLD_ALIGN_FLIP_MIN_DEG = 1.5     # 反号兜底只在大杆后允许触发

# 提速逃生档：14:39 那轮 6°/s 的杆下发 4.7° 实测只动 0.22°、下发 3.1° 只动
# 0.01°——手臂前伸后重心前移，运控对这个档的转身指令基本不跟随。与其直接
# 判死，不如把指令速度提上去再试一杆（人手动纠偏时也是这么干的）。
# 有效速率按 6°/s 档等比外推，未经真机标定，所以：① 只在确认"下发了大杆
# 却没动"之后才启用；② 单杆预计角卡死在 BOOST_MAX_DEG 内，宁可多打几杆；
# ③ 每杆之后照常重新测量，实际转了多少由测量说了算。
# 真机跑过之后可以从 align_*.jsonl 里 event=hold & boost=true 的记录反算
# 真实有效速率，再回来校准 BOOST_EFF。
HOLD_ALIGN_BOOST_CMD_DEG_S = 20.0
HOLD_ALIGN_BOOST_EFF_DEG_S = (HOLD_ALIGN_EFF_DEG_S * HOLD_ALIGN_BOOST_CMD_DEG_S
                              / HOLD_ALIGN_CMD_DEG_S)   # ≈12.7°/s（待标定）
HOLD_ALIGN_BOOST_MAX_DEG = 5.0    # 提速档单杆预计角上限（≈0.6s）

# --------------------------------- 安全闸 ---------------------------------
# 2026-07-30 17:19 事故（align_20260730 第 12 轮）：起手式抬手后躯干自平衡
# 前倾 5°（腰俯仰 -0.44°→+4.6°），头部相机跟着低头，0.4~1.0 m 深度带里掺进
# 地面/前伸的手臂。整幅 SVD 拟合被污染（点数 27k→13.5k），量出假的 +42°；
# 闭环拿它当真，连发 6 杆 6s（每杆≈22°）的整体转身——而基座当时压根没响应
# （六杆下来实测 yaw 只变了 0.14°，伴随 3104 应答超时）。手臂正伸在柜面前
# 46 cm，这种空转一旦真执行就是拿手臂扫柜子。三道闸各自独立拦这次事故：
ALIGN_ERR_CAP_ARMUP_DEG = 24.0   # 抬手后偏差上限。原来是 12，但 07-31 15:34
                                 # 那次量到的 +17.8° 是真的（相机测的平面 yaw
                                 # 变化 +31.55° 与 IMU yaw 变化 -31.66° 完全
                                 # 吻合），被误判成"测量异常"白白放弃。测量
                                 # 可不可信交给点数闸去判，这里只管"再大就不
                                 # 是抬手状态下能安全纠回来的了"
ALIGN_ERR_CAP_DEG = 25.0         # 放手时的上限：超过说明相机根本没对着柜面
ALIGN_POINTS_MIN_RATIO = 0.7     # 拟合点数掉到首帧的七成以下 → 测量不可信
ALIGN_STALL_MIN_EXPECT_DEG = 3.0 # 只用"大杆"判无响应（短杆响应本就随机）
ALIGN_STALL_RATIO = 0.15         # 实测变化不到预计的这个比例算"没动"。15:00 那轮
                                 # 正常收敛时实测/预计最低到过 0.24，留 1.6 倍余量
ALIGN_STALL_MAX = 2              # 提速后仍连续这么多大杆没动 → 判运控未响应
                                 # （第一次没动不判死，先切提速档再给两次机会）
ALIGN_DEADBAND_DEG = 1.5         # 只在抬手时放宽到这个死区：抬手后基座常常
                                 # 完全不响应小杆（15:05 那轮为 1.2° 的残差白磨
                                 # 12 杆）。放手时必须尊重调用方给的阈值——流程
                                 # 的验收带就靠它留出余量（见 18:08 的 ALIGN_FAILED：
                                 # 服务器停在带边缘 1.46°，流程独立复测量出 1.57°）
ALIGN_ARMUP_MAX_HOLD_S = 1.5     # 抬手时单杆上限（≈5°），杜绝 22° 的整体转身

# 抬手后的扰动是单向的：手臂前伸，身体自己往 + 方向转（07-31 十几次记录无一例外，
# 幅度 +3.5~+9.9°，而且我们每下发一杆它还会跟着晃 ~1.5°/s 好几秒）。于是：
#   ① 只许往 - 方向纠（对抗扰动）。需要往 + 转时一律不动——那是在和扰动同向
#      叠加，15:34/15:35 两次把机器人甩到 +30°，起手都是那一杆"往 + 转 2.6°"；
#      yaw 低于目标时干脆等着，自然回转本来就会把它带上来。
#   ② 既然只往一个方向纠，打过头也不怕（自然回转会填回来），所以可以放心
#      在久纠不进时加大力度。
ALIGN_ARMUP_ONE_WAY = True
ALIGN_ARMUP_WAIT_S = 1.5         # yaw 低于目标时每次等多久再复测
ALIGN_ARMUP_WAIT_MAX = 3         # 等这么多次还没被自然回转带进带里就收工
ALIGN_ARMUP_ESCALATE_STEP = 3    # 打到第几杆还没进带 → 切提速档加大力度
ALIGN_ARMUP_BUDGET_DEG = 30.0    # 抬手时累计转身预算（要够纠 24° 上限内的漂移），
                                 # 超了停手报错，杜绝上午那种连转 130° 的空转。
                                 # 按"实测转过的角度"扣，不按预计角——14:39 那轮
                                 # 基座实际只转了不到 2°，预算却被没兑现的预计角
                                 # 扣光判超支，纠偏机会白白浪费


def _align_abort(msg: str, log: dict) -> None:
    """安全闸拦下：先停走，再把原因写进状态和落盘日志。"""
    try:
        if state.loco_client is not None:
            state.loco_client.StopMove()
    except Exception:
        pass
    state.align_message = f"对中中止：{msg}"
    _align_log(log)


def _align_loop_hold(tol: float, dmin: float, dmax: float,
                     target: float = 0.0) -> None:
    """新对中：静止测偏差 → 一杆按模型算好时长 → 等稳 → 再测。

    与旧版（定长 0.5°/2° 脉冲逐步磨）的区别：时长连续可变、带死区补偿，
    正常情况 2~3 杆收敛。方向约定与旧版相同（yaw_err>0 → 左转），同样保留
    "偏差变大就反号"的兜底。target ≠ 0 时对到指定角度而非 0。
    下发了大杆基座却没动时，先把指令速度从 6°/s 提到 20°/s 再试（单杆预计角
    仍卡在 5° 内），提速后还不动才判运控未响应。
    抬手状态下改成单向纠偏：只往 - 方向打，yaw 低于目标时只等不纠（见
    ALIGN_ARMUP_ONE_WAY），久纠不进则直接切提速档。
    逐步日志写 align_<日期>.jsonl，mode=hold。
    """
    loco = _get_loco_client()
    sign = 1.0
    prev_err: float | None = None
    prev_expect = 0.0     # 上一杆的预计角，用于限制反号兜底只在大杆后触发
    armup = _arm_raised()
    err_cap = ALIGN_ERR_CAP_ARMUP_DEG if armup else ALIGN_ERR_CAP_DEG
    tol_eff = max(tol, ALIGN_DEADBAND_DEG) if armup else tol
    first_points: int | None = None   # 首帧点数，后续帧掉太多说明拟合面变了
    prev_yaw: float | None = None
    stall = 0             # 连续"下发大杆但没动"的次数
    boost = False         # 运控不跟随 / 久纠不进 → 切提速档（切了就不切回来）
    turned_deg = 0.0      # 累计转身量，按实测逐步累加
    one_way = armup and ALIGN_ARMUP_ONE_WAY   # 抬手时只许往 - 方向纠
    waited = 0            # 单向模式下"低于目标只能干等"的次数
    _align_log({"event": "start", "mode": "hold", "tol_deg": tol,
                "tol_eff_deg": tol_eff, "target_deg": target, "armup": armup,
                "err_cap_deg": err_cap, "dmin": dmin, "dmax": dmax})
    try:
        for step in range(1, HOLD_ALIGN_MAX_STEPS + 1):
            if state.align_cancel.is_set():
                state.align_message = "已中止"
                _align_log({"event": "cancelled", "mode": "hold", "step": step})
                return
            fitres = _fit_view_plane(dmin, dmax)
            if not fitres.get("ok"):
                state.align_message = f"新对中失败：{fitres.get('error')}"
                _align_log({"event": "fit_fail", "mode": "hold", "step": step,
                            "error": fitres.get("error")})
                return
            yaw = float(fitres["yaw_err_deg"])
            err = yaw - target
            points = int(fitres.get("points_used") or 0)
            if first_points is None:
                first_points = points
            _align_log({"event": "measure", "mode": "hold", "step": step,
                        "yaw_err_deg": round(yaw, 3), "points": points})

            # 闸① 点数腰斩 = 拟合已经不是柜面（掺进地面/前伸的手臂）
            if first_points > 0 and points < first_points * ALIGN_POINTS_MIN_RATIO:
                _align_abort(f"拟合点数 {points} 掉到首帧 {first_points} 的 "
                             f"{points / first_points:.0%}（低于 "
                             f"{ALIGN_POINTS_MIN_RATIO:.0%}），深度带里多半掺进了"
                             f"地面或前伸的手臂，测得的 yaw {yaw:+.2f}° 不可信",
                             {"event": "sanity_points", "mode": "hold",
                              "step": step, "points": points,
                              "first_points": first_points,
                              "yaw_err_deg": round(yaw, 3)})
                return
            if abs(err) <= tol_eff:
                state.align_message = (f"新对中完成：yaw {yaw:+.2f}°"
                                       f"（目标 {target:+.1f}°±{tol_eff:.1f}°，"
                                       f"{step - 1} 步）")
                _align_log({"event": "done", "mode": "hold", "step": step,
                            "yaw_err_deg": round(yaw, 3)})
                return
            # 闸② 偏差超可信上限：抬手后不可能真的偏这么多，转身风险却极高
            if abs(err) > err_cap:
                _align_abort(f"偏差 {err:+.2f}° 超出可信上限 ±{err_cap:.0f}°"
                             f"（{'抬手中' if armup else '未抬手'}），"
                             f"判为测量异常，未下发转身",
                             {"event": "sanity_cap", "mode": "hold",
                              "step": step, "err_deg": round(err, 3),
                              "cap_deg": err_cap, "armup": armup})
                return
            # 闸③ 下发了大杆却没动 = 运控没在执行速度指令，闭环已失去反馈。
            # 但"没动"先不判死：先把指令速度提上去再试，提速后还是不动才判。
            if prev_yaw is not None:
                moved = abs(yaw - prev_yaw)
                if prev_expect != 0.0:
                    # 预算按实测扣：没兑现的杆不该占额度；而单向模式下"只等不纠"
                    # 那几轮的自然回转不是我们转的，也不该占
                    turned_deg += moved
                if abs(prev_expect) >= ALIGN_STALL_MIN_EXPECT_DEG:
                    if moved < ALIGN_STALL_RATIO * abs(prev_expect):
                        stall += 1
                        _align_log({"event": "stall", "mode": "hold",
                                    "step": step, "moved_deg": round(moved, 3),
                                    "expect_deg": round(prev_expect, 2),
                                    "count": stall, "boost": boost})
                        if not boost:
                            boost = True
                            stall = 0   # 换档重新计数，给提速档完整的机会
                            _align_log({"event": "boost_on", "mode": "hold",
                                        "step": step,
                                        "cmd_deg_s": HOLD_ALIGN_BOOST_CMD_DEG_S})
                        elif stall >= ALIGN_STALL_MAX:
                            _align_abort(
                                f"提速到 {HOLD_ALIGN_BOOST_CMD_DEG_S:.0f}°/s 后仍连续 "
                                f"{stall} 杆下发 {abs(prev_expect):.1f}° 却只动了 "
                                f"{moved:.2f}°，运控未在执行转身指令"
                                f"（检查运控状态/是否被其他程序接管）",
                                {"event": "sanity_stall", "mode": "hold",
                                 "step": step, "count": stall, "boost": True})
                            return
                    else:
                        stall = 0
            prev_yaw = yaw

            # 单向闸：yaw 低于目标时不许往 + 转，等自然回转把它带上来
            if one_way and err < 0:
                waited += 1
                _align_log({"event": "one_way_wait", "mode": "hold", "step": step,
                            "yaw_err_deg": round(yaw, 3), "err_deg": round(err, 3),
                            "count": waited})
                if waited >= ALIGN_ARMUP_WAIT_MAX:
                    state.align_message = (
                        f"yaw {yaw:+.2f}° 低于目标 {target:+.1f}° 且等不来自然回转"
                        f"（等了 {waited} 次）。抬手状态下不做正向纠偏——那与身体"
                        f"自己的 + 向回转同向，会越纠越远")
                    _align_log({"event": "one_way_giveup", "mode": "hold",
                                "step": step, "yaw_err_deg": round(yaw, 3)})
                    return
                state.align_message = (
                    f"第 {step} 步：yaw {yaw:+.2f}° 低于目标 {target:+.1f}°，"
                    f"抬手时不反向纠偏，等自然回转（第 {waited}/"
                    f"{ALIGN_ARMUP_WAIT_MAX} 次）")
                prev_expect = 0.0      # 没下发，不参与无响应/反号判定
                if state.align_cancel.wait(ALIGN_ARMUP_WAIT_S):
                    state.align_message = "已中止"
                    _align_log({"event": "cancelled", "mode": "hold", "step": step})
                    return
                continue

            # 久纠不进 → 加大力度（单向模式下过冲有自然回转兜底，可以放心加）
            if one_way and not boost and step >= ALIGN_ARMUP_ESCALATE_STEP:
                boost = True
                _align_log({"event": "boost_on", "mode": "hold", "step": step,
                            "reason": "escalate",
                            "cmd_deg_s": HOLD_ALIGN_BOOST_CMD_DEG_S})

            # 反号兜底只信"大杆"的结果：短杆/小脉冲的响应本身随机，
            # 偏差涨一点不代表方向错（第 4 轮真机就是被这个误触发震荡的）。
            # 单向模式下方向是钉死的，这条兜底本身就没有意义——而且 07-31
            # 15:34/15:35 两次正是被它误触发：偏差"变大"其实是冲过头（误差
            # 从负变正），方向压根没错，一反号就朝错误方向又打一杆。
            if (not one_way and prev_err is not None
                    and abs(err) > abs(prev_err) + 0.3
                    and abs(prev_expect) >= HOLD_ALIGN_FLIP_MIN_DEG):
                sign = -sign
                _align_log({"event": "sign_flip", "mode": "hold", "step": step})
            prev_err = err
            direction = 1.0 if sign * err > 0 else -1.0

            if boost and abs(err) >= HOLD_ALIGN_FINE_DEG:
                # 6°/s 那档基座不跟随，提速再试。时长按提速后的有效速率重算，
                # 单杆预计角照样卡死，靠多打几杆收敛而不是靠一杆打到位
                cmd_deg_s, eff_deg_s, kind = (HOLD_ALIGN_BOOST_CMD_DEG_S,
                                              HOLD_ALIGN_BOOST_EFF_DEG_S,
                                              f"提速{HOLD_ALIGN_BOOST_CMD_DEG_S:.0f}°/s 按")
                lo = HOLD_ALIGN_MIN_HOLD_S
                hi = HOLD_ALIGN_DEAD_S + HOLD_ALIGN_BOOST_MAX_DEG / eff_deg_s
            elif abs(err) < HOLD_ALIGN_FINE_DEG:
                # 小残差：降速慢杆收尾。同样的角度时长翻倍，稳稳越过死区
                # 和短杆随机区，分辨率比高速档细一倍
                cmd_deg_s, eff_deg_s, kind = (HOLD_ALIGN_FINE_CMD_DEG_S,
                                              HOLD_ALIGN_FINE_EFF_DEG_S, "慢杆")
                lo, hi = HOLD_ALIGN_MIN_HOLD_S + 0.05, 1.2
            else:
                cmd_deg_s, eff_deg_s, kind = (HOLD_ALIGN_CMD_DEG_S,
                                              HOLD_ALIGN_EFF_DEG_S, "按")
                lo, hi = HOLD_ALIGN_MIN_HOLD_S, HOLD_ALIGN_MAX_HOLD_S
            if armup:
                hi = min(hi, ALIGN_ARMUP_MAX_HOLD_S)   # 手臂前伸，禁止大角度转身
            hold_s = HOLD_ALIGN_DEAD_S + abs(err) * HOLD_ALIGN_GAIN / eff_deg_s
            hold_s = float(np.clip(hold_s, lo, hi))
            omega = math.radians(cmd_deg_s) * direction
            prev_expect = (hold_s - HOLD_ALIGN_DEAD_S) * eff_deg_s * direction
            # 预算看的是"已经实测转过的 + 这一杆预计要转的"，实测部分在上面
            # 逐步累加。这样没兑现的杆不占额度，真转起来了照样按 15° 拦住
            if armup and turned_deg + abs(prev_expect) > ALIGN_ARMUP_BUDGET_DEG:
                _align_abort(f"抬手状态实测已转 {turned_deg:.1f}°，再打这杆就超预算 "
                             f"{ALIGN_ARMUP_BUDGET_DEG:.0f}°，停手（手臂前伸时"
                             f"大幅转身有撞柜风险）",
                             {"event": "sanity_budget", "mode": "hold",
                              "step": step, "turned_deg": round(turned_deg, 2),
                              "expect_deg": round(prev_expect, 2)})
                return
            state.align_message = (f"第 {step} 步：偏差 {err:+.2f}° → "
                                   f"{kind} {hold_s:.2f}s（预计 {prev_expect:+.1f}°）")
            code = loco.SetVelocity(0.0, 0.0, omega, hold_s)
            _align_log({"event": "hold", "mode": "hold", "step": step,
                        "hold_s": round(hold_s, 3), "boost": boost,
                        "cmd_deg_s": cmd_deg_s, "turned_deg": round(turned_deg, 2),
                        "expect_deg": round(prev_expect, 2), "rpc_code": code})
            if code == RPC_TIMEOUT_CODE:
                state.align_message += "（RPC 应答超时，按已执行继续）"
            elif code not in (0, None):
                state.align_message = f"新对中失败：SetVelocity 返回码 {code}"
                return
            if state.align_cancel.wait(hold_s):
                state.align_message = "已中止"
                _align_log({"event": "cancelled", "mode": "hold", "step": step})
                return
            # 不指望 SetVelocity 的时长自己终止：07-31 15:35 那杆下发 0.635s
            # （预计 5°）实际转了 ~14°，基座明显没按时长停。时间一到就显式刹住
            try:
                loco.StopMove()
            except Exception:
                pass
            if state.align_cancel.wait(ALIGN_SETTLE_S):
                state.align_message = "已中止"
                _align_log({"event": "cancelled", "mode": "hold", "step": step})
                return
        if prev_err is not None and abs(prev_err) <= max(ALIGN_TOL_FALLBACK_DEG, tol):
            state.align_message = (f"基本对中：偏差 {prev_err:+.2f}°"
                                   f"（未达 {tol}°，但已在兜底内）")
            _align_log({"event": "done_fallback", "mode": "hold",
                        "err_deg": round(prev_err, 3)})
        else:
            state.align_message = (f"未收敛：{HOLD_ALIGN_MAX_STEPS} 杆后偏差仍 "
                                   f"{prev_err:+.2f}°（阈值 {tol}°）")
            _align_log({"event": "give_up", "mode": "hold",
                        "err_deg": None if prev_err is None else round(prev_err, 3)})
    except Exception as exc:
        state.align_message = f"新对中异常：{exc}"
        _align_log({"event": "exception", "mode": "hold", "error": str(exc)})
    finally:
        try:
            loco.StopMove()
        except Exception:
            pass
        state.align_running = False


@router.post("/align_yaw")
def reach_align_yaw(body: dict):
    """一键对中（真机！）。Body: {"start": true, "mode"?: "hold", "tol_deg"?,
    "target_deg"?, "dmin"?, "dmax"?} 或 {"stop": true}。mode="hold" 用新对中
    （打杆式），否则用旧版定长脉冲。闭环转身直到 yaw 进入 target±tol
    （target 缺省 0，即传统的垂直对中）。"""
    if "hand_raised" in body:
        state.hand_raised_ui = bool(body["hand_raised"])
    if body.get("stop"):
        state.align_cancel.set()
        try:
            if state.loco_client is not None:
                state.loco_client.StopMove()
        except Exception:
            pass
        return {"ok": True, "stopped": True}

    if not state.loco_available:
        return JSONResponse({"ok": False, "error": "无 DDS 连接（--no-robot 模式）"},
                            status_code=409)
    if state.align_running:
        return JSONResponse({"ok": False, "error": "对中已在进行中"}, status_code=409)
    if state.exec_running:
        return JSONResponse({"ok": False, "error": "手臂轨迹执行中，禁止转身"},
                            status_code=409)
    try:
        _get_loco_client()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"loco 客户端初始化失败: {exc}"},
                            status_code=502)

    if "tol_deg" in body:
        tol = float(body["tol_deg"])
        tol_note = f"指定阈值 {tol}°"
    elif _arm_raised():
        tol = ALIGN_TOL_RAISED_DEG
        tol_note = f"手臂前伸，阈值放宽到 {tol}°"
    else:
        tol = ALIGN_TOL_STRICT_DEG
        tol_note = f"手臂收回，严格阈值 {tol}°"
    dmin = float(body.get("dmin", 0.3))
    dmax = float(body.get("dmax", 1.0))
    target = float(body.get("target_deg", 0.0))
    use_hold = body.get("mode") == "hold"
    loop = _align_loop_hold if use_hold else _align_loop
    state.align_cancel = threading.Event()
    state.align_running = True
    if abs(target) > 0.01:
        tol_note += f"，目标 {target:+.1f}°"
    state.align_message = f"{'新' if use_hold else ''}对中开始（{tol_note}）…"
    state.align_thread = threading.Thread(
        target=loop, args=(tol, dmin, dmax, target), name="reach-align", daemon=True)
    state.align_thread.start()
    return {"ok": True, "started": True, "tol_deg": tol, "target_deg": target,
            "mode": "hold" if use_hold else "pulse"}
