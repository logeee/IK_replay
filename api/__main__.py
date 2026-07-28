"""命令行入口：python -m api [--base http://127.0.0.1:8001]

未部署步骤默认走 7002 人工确认台（需先另开一个终端跑
python -m api.console）；--no-console 恢复"未实现即中止"。
"""

from __future__ import annotations

import argparse
import sys

from .client import ReachClient
from .console_client import ConsoleClient
from .flow import SwitchFlow


def main() -> int:
    parser = argparse.ArgumentParser(description="拨动开关全流程")
    parser.add_argument("--base", default="http://127.0.0.1:8001",
                        help="reach_server 地址")
    parser.add_argument("--console", default="http://127.0.0.1:7002",
                        help="人工确认台地址（python -m api.console 启动）")
    parser.add_argument("--no-console", action="store_true",
                        help="不用确认台：未部署步骤直接按 NOT_IMPLEMENTED 中止")
    parser.add_argument("--coarse-target", type=float, default=-4.5,
                        help="3️⃣ 粗对齐目标角（°），带 = 目标±coarse-tol")
    parser.add_argument("--coarse-tol", type=float, default=1.5,
                        help="3️⃣ 粗对齐带半宽（°），默认 -4.5±1.5 即 -6~-3")
    parser.add_argument("--fine-target", type=float, default=-3.0,
                        help="6️⃣ 保持目标角（°）")
    parser.add_argument("--fine-tol", type=float, default=2.0,
                        help="6️⃣ 保持带半宽（°），默认 -3±2")
    parser.add_argument("--align-mode", default="hold", choices=["hold", "pulse"],
                        help="腰部对齐用新对中(hold)还是旧定长脉冲(pulse)")
    parser.add_argument("--approach-offset", type=float, default=0.0,
                        help="取点接近偏移（m，0=顶到表面，负=压入表面）")
    parser.add_argument("--duration", type=float, default=6.0,
                        help="IK 主段（到位）时长（s）")
    parser.add_argument("--sidestep-cm", type=float, default=6.0,
                        help="到位后沿柜面左移距离（cm，负=右移）")
    parser.add_argument("--push-n", type=float, default=25.0,
                        help="横移时的前馈推力（N）")
    parser.add_argument("--lift-cm", type=float, default=2.0,
                        help="规划中段抬高（cm）")
    parser.add_argument("--rounds", type=int, default=3,
                        help="拨动失败重试轮数")
    args = parser.parse_args()

    console = None if args.no_console else ConsoleClient(args.console)
    flow = SwitchFlow(client=ReachClient(args.base),
                      console=console,
                      coarse_target_deg=args.coarse_target,
                      coarse_tol_deg=args.coarse_tol,
                      fine_target_deg=args.fine_target,
                      fine_tol_deg=args.fine_tol,
                      align_mode=args.align_mode,
                      approach_offset_m=args.approach_offset,
                      reach_duration_s=args.duration,
                      sidestep_cm=args.sidestep_cm,
                      push_force_n=args.push_n,
                      lift_m=max(0.0, args.lift_cm) / 100.0,
                      max_flip_rounds=args.rounds)
    result = flow.run()
    print(f"[flow] 结果: ok={result.ok} code={result.code.name} "
          f"message={result.message} detail={result.detail}")
    return 0 if result.ok else int(result.code)


if __name__ == "__main__":
    sys.exit(main())
