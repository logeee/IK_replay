"""H2 腿部小碎步控制（高层 LocoClient 速度脉冲，只动腿，不碰腰/手臂）。

原理：机器人处于运动模式（walking FSM）时，向本体运控发
SetVelocity(vx, vy, 0, duration) 短脉冲，让步态策略自己迈小步平移；
平衡、落脚全由本体负责，我们只给"往哪边挪、挪多少"。

真机经验（来自本仓库对中代码与 reach_logs 的教训）：
- SetVelocity 的 duration 不可靠：曾下发 0.635s（预计 5°）实际转了 ~14°，
  所以每一杆之后必须显式 StopMove() 刹车，绝不指望固件按时长自停。
- RPC 返回码 3104 = 应答超时，指令多半已经执行，按已执行处理。
- 步态策略对小速度有死区：速度给太小可能原地踏步不位移。
  宁可"稍大速度 × 短时长"，不要"极小速度 × 长时长"。
- 位移是开环的（速度 × 时间 − 刹车滑行），单杆误差可达 ±50%。
  精确到位要靠外部反馈（视觉/里程）多杆逼近；本模块只负责"迈一小步"。
- 迈步会带动全身（包括前伸的手臂），走步前先把手收回。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

RPC_TIMEOUT_CODE = 3104          # 应答超时 ≠ 未执行

# 真机 GetAvailableFsmIds 实测（2026-07-31）里的行走类状态：
#   601=HybridWalk  701=WalkNew  703=PhaseWalk
# 500/501 是 SDK 惯例值，本机固件未列出但保留兼容。
WALKING_FSM_IDS = frozenset({500, 501, 601, 701, 703})


@dataclass
class ShuffleConfig:
    speed_mps: float = 0.08        # 小碎步平移速度（死区之上、又足够慢）
    max_step_cm: float = 10.0      # 单杆位移上限（安全限幅）
    min_pulse_s: float = 0.30      # 太短的脉冲固件基本不响应
    max_pulse_s: float = 2.50      # 单杆时长上限（防手滑输大数）
    settle_s: float = 0.80         # 刹车后等身体稳住再返回
    check_fsm: bool = True         # 走步前校验处于行走 FSM


class H2Shuffle:
    """小碎步控制器。用法：

        sh = H2Shuffle()
        sh.connect("enp86s0")
        sh.step(dy_cm=+5)     # 左移约 5cm（开环，误差见模块注释）
        sh.step(dx_cm=-3)     # 后退约 3cm
        sh.stop()
    """

    def __init__(self, config: ShuffleConfig | None = None,
                 log_dir: str | Path | None = None):
        self.cfg = config or ShuffleConfig()
        self._client = None
        if log_dir is None:
            log_dir = Path(__file__).resolve().parent / "logs"
        self.log_path = (Path(log_dir)
                         / f"shuffle_{datetime.now():%Y%m%d}.jsonl")

    # ---------------- 连接 / 状态 ----------------

    def connect(self, iface: str = "enp86s0", domain: int = 0,
                timeout_s: float = 5.0):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient

        ChannelFactoryInitialize(domain, iface)
        c = LocoClient()
        c.SetTimeout(timeout_s)
        c.Init()
        self._client = c
        return self

    def attach(self, loco_client):
        """复用外部已建好的 LocoClient（如 reach_server 里的那只）。"""
        self._client = loco_client
        return self

    @property
    def client(self):
        if self._client is None:
            raise RuntimeError("先 connect(iface) 或 attach(loco_client)")
        return self._client

    def fsm_id(self) -> int | None:
        code, fsm = self.client.GetFsmId()
        return fsm if code == 0 else None

    def in_walking_mode(self) -> bool:
        fsm = self.fsm_id()
        return fsm is not None and fsm in WALKING_FSM_IDS

    # ---------------- 小碎步 ----------------

    def step(self, dx_cm: float = 0.0, dy_cm: float = 0.0) -> dict:
        """迈一小步：dx 前(+)/后(−)，dy 左(+)/右(−)，单位 cm。

        开环脉冲：速度固定为 cfg.speed_mps，时长 = 距离/速度，
        发完等时长到，立刻显式刹车，再等 settle_s 稳定。
        返回 {"ok", "dx_cm", "dy_cm", "pulse_s", "rpc_code", ...}。
        """
        cfg = self.cfg
        dist_cm = math.hypot(dx_cm, dy_cm)
        if dist_cm < 0.5:
            return {"ok": True, "skipped": "位移 <0.5cm，不动"}
        if dist_cm > cfg.max_step_cm:
            scale = cfg.max_step_cm / dist_cm
            dx_cm, dy_cm, dist_cm = (dx_cm * scale, dy_cm * scale,
                                     cfg.max_step_cm)

        if cfg.check_fsm:
            fsm = self.fsm_id()
            if fsm is None or fsm not in WALKING_FSM_IDS:
                return {"ok": False, "fsm_id": fsm,
                        "error": f"当前 FSM={fsm} 不是行走状态"
                                 f"（需 {sorted(WALKING_FSM_IDS)} 之一），"
                                 f"请先用遥控器把机器人切到运动模式"}

        pulse_s = dist_cm / 100.0 / cfg.speed_mps
        pulse_s = min(max(pulse_s, cfg.min_pulse_s), cfg.max_pulse_s)
        # 时长被限幅后按实际时长反算速度，保证位移量不变
        vx = dx_cm / 100.0 / pulse_s
        vy = dy_cm / 100.0 / pulse_s

        code = self.client.SetVelocity(vx, vy, 0.0, pulse_s)
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"),
               "dx_cm": round(dx_cm, 2), "dy_cm": round(dy_cm, 2),
               "vx": round(vx, 4), "vy": round(vy, 4),
               "pulse_s": round(pulse_s, 3), "rpc_code": code}
        self._log(rec)
        if code not in (0, None, RPC_TIMEOUT_CODE):
            return {"ok": False, "error": f"SetVelocity 返回码 {code}", **rec}

        time.sleep(pulse_s)
        self.stop()                    # 教训：不指望 duration 自停
        time.sleep(cfg.settle_s)
        rec["ok"] = True
        if code == RPC_TIMEOUT_CODE:
            rec["warning"] = "RPC 应答超时，指令多半已执行"
        return rec

    def stop(self):
        try:
            self.client.StopMove()     # 即 SetVelocity(0,0,0)
        except Exception:
            pass

    # ---------------- 记录 ----------------

    def _log(self, rec: dict):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _cli():
    import argparse

    parser = argparse.ArgumentParser(
        description="H2 小碎步点动（真机！确保周围无障碍、手臂已收回）")
    parser.add_argument("iface", nargs="?", default="enp86s0",
                        help="DDS 网卡名（默认 enp86s0）")
    parser.add_argument("--speed", type=float, default=None,
                        help="平移速度 m/s（默认 0.08）")
    args = parser.parse_args()

    cfg = ShuffleConfig()
    if args.speed:
        cfg.speed_mps = args.speed
    sh = H2Shuffle(cfg).connect(args.iface)
    print(f"已连接，当前 FSM={sh.fsm_id()}  行走模式={sh.in_walking_mode()}")
    print("命令：f/b/l/r <cm>（前/后/左/右）、s（急停）、st（状态）、q（退出）")
    print("示例：l 5  → 左移约 5cm")
    while True:
        try:
            line = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            sh.stop()
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0]
        if cmd == "q":
            sh.stop()
            break
        if cmd == "s":
            sh.stop()
            print("已刹车")
            continue
        if cmd == "st":
            print(f"FSM={sh.fsm_id()}  行走模式={sh.in_walking_mode()}")
            continue
        if cmd in ("f", "b", "l", "r"):
            try:
                cm = float(parts[1]) if len(parts) > 1 else 3.0
            except ValueError:
                print("距离要是数字，如: l 5")
                continue
            dx, dy = {"f": (cm, 0.0), "b": (-cm, 0.0),
                      "l": (0.0, cm), "r": (0.0, -cm)}[cmd]
            print(sh.step(dx_cm=dx, dy_cm=dy))
            continue
        print("不认识的命令，可用 f/b/l/r <cm>、s、st、q")


if __name__ == "__main__":
    _cli()
