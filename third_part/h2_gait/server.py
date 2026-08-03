"""H2 小碎步独立控制服务（前后端独立，不启动任何相机/YOLO 服务）。

只做三件事：
1. DDS 连本体：LocoClient 发速度（官方 SetVelocity 7105 通道）；
2. 订阅 rt/odommodestate 里程计（只读），给点动模式做"实测挪够就停"的闭环；
3. 起一个 FastAPI 小服务托管控制页面（web/index.html）。

启动：python3 third_part/h2_gait/server.py --iface enp86s0 --port 9210

安全设计：
- 按住模式是死人开关：前端 ~0.3s 心跳一次，每次心跳发 0.8s 短脉冲互相覆盖；
  心跳断了（松键/断网/页面崩），看门狗 0.9s 内显式 StopMove 刹车。
- 每个运动请求先校验 FSM 在行走状态（601/701/703...），不在就拒发。
- duration 不可信（真机教训），一切停止都走显式 StopMove。
- 速度/位移全部限幅；服务退出时刹车。
"""
from __future__ import annotations

import argparse
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

try:
    from .shuffle import RPC_TIMEOUT_CODE, WALKING_FSM_IDS
except ImportError:                       # 直接 python3 server.py 运行时
    from shuffle import RPC_TIMEOUT_CODE, WALKING_FSM_IDS

# ---------------- 限幅与节奏 ----------------
# 真机（2026-07-31）：平移 0.12 m/s 在迈步阈值之下——策略只倾斜重心不挪脚；
# 同事平台前进用 0.2 m/s 能走。所以上限放宽、点动加自动升速。
MAX_VX = 0.35          # m/s 前后
MAX_VY = 0.35          # m/s 左右
MAX_OMEGA = 0.60       # rad/s（≈34°/s，同事平台用 0.5）
HOLD_PULSE_S = 0.8     # 按住模式单个脉冲时长（前端 0.3s 心跳覆盖）
HOLD_STALE_S = 0.9     # 心跳断流判定 → 看门狗刹车
# 点动速度阶梯：从低档起，里程计发现没挪动就升档（横移阈值更高，起点也高）
STEP_LADDER_X = (0.12, 0.18, 0.25)
STEP_LADDER_Y = (0.15, 0.22, 0.28)
STEP_STALL_S = 1.0     # 每这么久检查一次是否在挪
STEP_STALL_M = 0.01    # 检查窗口内进展 <1cm 视为没迈步 → 升档
STEP_MIN_CM, STEP_MAX_CM = 0.5, 15.0
STEP_RESEND_S = 0.25   # 点动期间速度指令重发周期
STEP_POLL_S = 0.02     # 里程计进度检查周期
STEP_SETTLE_S = 0.6    # 刹车后等身体稳住再量最终位移
ODOM_FRESH_S = 0.5     # 里程计数据新鲜度
FSM_CACHE_S = 2.0      # FSM 校验结果缓存，避免每个心跳都打 RPC

WEB_DIR = Path(__file__).resolve().parent / "web"
LOG_DIR = Path(__file__).resolve().parent / "logs"


def _yaw_from_quat(q):  # unitree 顺序 [w, x, y, z]
    w, x, y, z = q
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class Odom:
    """rt/odommodestate 只读订阅（世界系位置 + 姿态，H2 实测 500Hz）。

    注：同事平台的桥订阅的低频版 rt/lf/odommodestate 在 H2 上没有数据，
    真机嗅探（2026-07-31）只有 rt/odommodestate 在发。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pos = (0.0, 0.0, 0.0)
        self._yaw = 0.0
        self._t = 0.0

    def start(self):
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_

        sub = ChannelSubscriber("rt/odommodestate", SportModeState_)
        sub.Init(self._handler, 10)
        self._sub = sub

    def _handler(self, msg):
        with self._lock:
            self._pos = (msg.position[0], msg.position[1], msg.position[2])
            self._yaw = _yaw_from_quat(msg.imu_state.quaternion)
            self._t = time.monotonic()

    def read(self):
        """返回 (x, y, yaw, fresh)。"""
        with self._lock:
            fresh = (time.monotonic() - self._t) < ODOM_FRESH_S if self._t else False
            return self._pos[0], self._pos[1], self._yaw, fresh


class LocoControl:
    def __init__(self):
        self._client = None
        self._lock = threading.Lock()        # 同一时刻只允许一个运动会话
        self._cancel = threading.Event()     # 手动停止 → 打断点动循环
        self._fsm_cache: tuple[float, int | None] = (0.0, None)
        self.odom = Odom()
        self.hold_active = False
        self.hold_last_beat = 0.0
        self.step_busy = False
        self.last_result: dict = {}

    # ---------- 连接 ----------

    def connect(self, iface: str):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient

        ChannelFactoryInitialize(0, iface)
        c = LocoClient()
        c.SetTimeout(3.0)
        c.Init()
        self._client = c
        self.odom.start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---------- 状态 ----------

    def fsm_id(self, cached: bool = True) -> int | None:
        now = time.monotonic()
        if cached and now - self._fsm_cache[0] < FSM_CACHE_S:
            return self._fsm_cache[1]
        try:
            code, fsm = self._client.GetFsmId()
            fsm = fsm if code == 0 else None
        except Exception:
            fsm = None
        self._fsm_cache = (now, fsm)
        return fsm

    def _guard(self) -> str | None:
        fsm = self.fsm_id()
        if fsm is None or fsm not in WALKING_FSM_IDS:
            return (f"当前 FSM={fsm} 不是行走状态，"
                    f"请先用遥控器切到运动模式（如 703 PhaseWalk）")
        return None

    def status(self) -> dict:
        x, y, yaw, fresh = self.odom.read()
        return {"fsm_id": self.fsm_id(),
                "walking": self.fsm_id() in WALKING_FSM_IDS,
                "odom": {"x": round(x, 4), "y": round(y, 4),
                         "yaw_deg": round(math.degrees(yaw), 2),
                         "fresh": fresh},
                "hold_active": self.hold_active,
                "step_busy": self.step_busy,
                "last_result": self.last_result}

    # ---------- 按住模式（死人开关） ----------

    def hold_beat(self, vx: float, vy: float, omega: float) -> dict:
        err = self._guard()
        if err:
            return {"ok": False, "error": err}
        if self.step_busy:
            return {"ok": False, "error": "点动执行中，先等它完成"}
        vx = max(-MAX_VX, min(MAX_VX, vx))
        vy = max(-MAX_VY, min(MAX_VY, vy))
        omega = max(-MAX_OMEGA, min(MAX_OMEGA, omega))
        code = self._client.SetVelocity(vx, vy, omega, HOLD_PULSE_S)
        if code not in (0, None, RPC_TIMEOUT_CODE):
            return {"ok": False, "error": f"SetVelocity 返回码 {code}"}
        self.hold_active = True
        self.hold_last_beat = time.monotonic()
        return {"ok": True, "vx": vx, "vy": vy, "omega": omega,
                "pulse_s": HOLD_PULSE_S,
                **({"warning": "RPC 超时，指令多半已执行"}
                   if code == RPC_TIMEOUT_CODE else {})}

    def _watchdog(self):
        while True:
            time.sleep(0.15)
            if self.hold_active and \
                    time.monotonic() - self.hold_last_beat > HOLD_STALE_S:
                self.stop(reason="watchdog")

    # ---------- 停止 ----------

    def stop(self, reason: str = "manual") -> dict:
        self.hold_active = False
        if reason in ("manual", "shutdown"):
            self._cancel.set()               # 打断进行中的点动
        try:
            self._client.StopMove()
        except Exception as exc:
            return {"ok": False, "error": f"StopMove 失败: {exc}"}
        if reason != "manual":
            self._log({"event": "auto_stop", "reason": reason})
        return {"ok": True}

    # ---------- 点动模式（里程计闭环） ----------

    def step(self, dx_cm: float, dy_cm: float) -> dict:
        err = self._guard()
        if err:
            return {"ok": False, "error": err}
        if self.hold_active:
            return {"ok": False, "error": "按住模式进行中，松开再点动"}
        dist_cm = math.hypot(dx_cm, dy_cm)
        if dist_cm < STEP_MIN_CM:
            return {"ok": False, "error": f"位移需 ≥{STEP_MIN_CM}cm"}
        if dist_cm > STEP_MAX_CM:
            scale = STEP_MAX_CM / dist_cm
            dx_cm, dy_cm, dist_cm = dx_cm * scale, dy_cm * scale, STEP_MAX_CM
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "另一个动作正在执行"}
        self.step_busy = True
        self._cancel.clear()
        try:
            result = self._step_locked(dx_cm, dy_cm, dist_cm)
        finally:
            self.step_busy = False
            self._lock.release()
        self.last_result = result
        self._log({"event": "step", **result})
        return result

    def _step_locked(self, dx_cm: float, dy_cm: float, dist_cm: float) -> dict:
        target_m = dist_cm / 100.0
        ux, uy = dx_cm / dist_cm, dy_cm / dist_cm     # 机体系单位方向
        ladder = STEP_LADDER_Y if abs(dy_cm) > abs(dx_cm) else STEP_LADDER_X
        speed_idx = 0
        speed = ladder[speed_idx]
        vx, vy = ux * speed, uy * speed

        x0, y0, yaw0, fresh = self.odom.read()
        closed_loop = fresh
        c0, s0 = math.cos(yaw0), math.sin(yaw0)

        def progress():
            """机体系（步开始时刻）位移在指令方向上的投影 + 侧偏（m）。"""
            x, y, _, _ = self.odom.read()
            wx, wy = x - x0, y - y0
            bx = c0 * wx + s0 * wy      # 机体前向
            by = -s0 * wx + c0 * wy     # 机体左向
            along = bx * ux + by * uy
            cross = -bx * uy + by * ux
            return along, cross, bx, by

        expect_s = target_m / ladder[0]
        # 留够升档时间：低速试探 + 两次升档窗口
        deadline = time.monotonic() + max(6.0, expect_s * 3.0
                                          + STEP_STALL_S * len(ladder) + 2.0)
        t_start = time.monotonic()
        last_send = 0.0
        rpc_warn = False
        stall_t = t_start          # 上次"是否在挪"检查时刻
        stall_along = 0.0          # 上次检查时的进展

        cancelled = False
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                cancelled = True
                break
            now = time.monotonic()
            if now - last_send >= STEP_RESEND_S:
                code = self._client.SetVelocity(vx, vy, 0.0, HOLD_PULSE_S)
                if code == RPC_TIMEOUT_CODE:
                    rpc_warn = True
                elif code not in (0, None):
                    self.stop()
                    return {"ok": False, "error": f"SetVelocity 返回码 {code}",
                            "dx_cm": dx_cm, "dy_cm": dy_cm}
                last_send = now
            if closed_loop:
                along, _, _, _ = progress()
                if along >= target_m:
                    break
                # 没迈步（倾斜应付）→ 升一档速度再试
                if now - stall_t >= STEP_STALL_S:
                    if along - stall_along < STEP_STALL_M \
                            and speed_idx < len(ladder) - 1:
                        speed_idx += 1
                        speed = ladder[speed_idx]
                        vx, vy = ux * speed, uy * speed
                        last_send = 0.0        # 立刻按新速度重发
                        self._log({"event": "step_escalate",
                                   "speed_mps": speed,
                                   "along_cm": round(along * 100, 2)})
                    stall_t, stall_along = now, along
            elif now - t_start >= expect_s:   # 无里程计：退化为开环时长
                break
            time.sleep(STEP_POLL_S)
        else:
            self.stop()
            along, cross, bx, by = progress() if closed_loop else (None,) * 4
            return {"ok": False,
                    "error": f"超时未走够（已试到 {speed:.2f} m/s 仍没迈步，"
                             f"策略可能被卡住或该方向死区更高）",
                    "dx_cm": dx_cm, "dy_cm": dy_cm, "closed_loop": closed_loop,
                    "speed_final": speed,
                    "measured_along_cm": None if along is None else round(along * 100, 2)}

        if cancelled:
            self.stop()
            time.sleep(0.3)
            along, cross, _, _ = progress() if closed_loop else (None,) * 4
            return {"ok": False, "error": "已手动停止", "cancelled": True,
                    "dx_cm": dx_cm, "dy_cm": dy_cm, "speed_final": speed,
                    "measured_along_cm": None if along is None else round(along * 100, 2)}

        self.stop()
        time.sleep(STEP_SETTLE_S)
        out = {"ok": True, "dx_cm": round(dx_cm, 2), "dy_cm": round(dy_cm, 2),
               "closed_loop": closed_loop, "speed_final": speed,
               "escalations": speed_idx,
               "elapsed_s": round(time.monotonic() - t_start, 2)}
        if closed_loop:
            along, cross, bx, by = progress()
            out.update({"measured_along_cm": round(along * 100, 2),
                        "measured_cross_cm": round(cross * 100, 2),
                        "measured_bx_cm": round(bx * 100, 2),
                        "measured_by_cm": round(by * 100, 2)})
        if rpc_warn:
            out["warning"] = "期间有 RPC 超时，按已执行处理"
        return out

    # ---------- 日志 ----------

    def _log(self, rec: dict):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f"server_{datetime.now():%Y%m%d}.jsonl"
            rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), **rec}
            with path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


ctl = LocoControl()
app = FastAPI(title="H2 小碎步控制", docs_url=None, redoc_url=None)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/status")
def api_status():
    return ctl.status()


@app.post("/api/hold")
def api_hold(body: dict):
    out = ctl.hold_beat(float(body.get("vx") or 0.0),
                        float(body.get("vy") or 0.0),
                        float(body.get("omega") or 0.0))
    return out if out.get("ok") else JSONResponse(out, status_code=409)


@app.post("/api/stop")
def api_stop():
    return ctl.stop()


@app.post("/api/step")
def api_step(body: dict):
    out = ctl.step(float(body.get("dx_cm") or 0.0),
                   float(body.get("dy_cm") or 0.0))
    return out if out.get("ok") else JSONResponse(out, status_code=409)


@app.on_event("shutdown")
def on_shutdown():
    ctl.stop(reason="shutdown")


def main():
    parser = argparse.ArgumentParser(description="H2 小碎步独立控制服务")
    parser.add_argument("--iface", default="enp86s0", help="DDS 网卡名")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9210)
    args = parser.parse_args()

    ctl.connect(args.iface)
    print(f"[h2_gait] 已连 DDS（{args.iface}），当前 FSM={ctl.fsm_id(cached=False)}")
    print(f"[h2_gait] 控制页面: http://<本机IP>:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
