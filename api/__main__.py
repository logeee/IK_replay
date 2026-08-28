"""命令行入口：python -m api [--base http://127.0.0.1:8001]

未部署步骤默认走 7002 人工确认台（需先另开一个终端跑
python -m api.console）；--no-console 恢复"未实现即中止"。
"""

from __future__ import annotations

import argparse
import sys

from core.alignment_config import load_alignment_config

from .client import ReachClient
from .console_client import ConsoleClient
from .flow import SwitchFlow
from .yolo_client import YoloClient


def main() -> int:
    parser = argparse.ArgumentParser(description="拨动开关全流程")
    parser.add_argument("--base", default="http://127.0.0.1:8001",
                        help="reach_server 地址")
    parser.add_argument("--console", default="http://127.0.0.1:7002",
                        help="人工确认台地址（python -m api.console 启动）")
    parser.add_argument("--no-console", action="store_true",
                        help="不用确认台：未部署步骤直接按 NOT_IMPLEMENTED 中止")
    parser.add_argument("--yolo", default="http://127.0.0.1:7004",
                        help="YOLO 推理服务地址（python -m api.yolo_server 启动）")
    parser.add_argument("--no-yolo", action="store_true",
                        help="不用 YOLO：场景判断和复核全走确认台人工")
    parser.add_argument("--coarse-target", type=float,
                        help="覆盖 JSON 中的3️⃣粗对齐目标角（°）")
    parser.add_argument("--coarse-tol", type=float,
                        help="覆盖 JSON：粗对齐验收改为目标±此值，命令阈值取一半")
    parser.add_argument("--fine-target", type=float,
                        help="覆盖 JSON 中的6️⃣保持目标角（°）")
    parser.add_argument("--fine-tol", type=float,
                        help="覆盖 JSON：细对齐验收改为目标±此值，命令阈值取一半")
    parser.add_argument("--align-mode", default="hold", choices=["hold", "pulse"],
                        help="腰部对齐用新对中(hold)还是旧定长脉冲(pulse)")
    parser.add_argument("--approach-offset", type=float, default=0.0,
                        help="取点接近偏移（m，0=顶到表面，负=压入表面）")
    parser.add_argument("--duration", type=float, default=6.0,
                        help="IK 主段（到位）时长（s）")
    parser.add_argument("--sidestep-cm", type=float, default=10.0,
                        help="到位后沿柜面左移距离（cm，负=右移）")
    parser.add_argument("--push-n", type=float, default=10.0,
                        help="横移时的前馈推力（N）")
    parser.add_argument("--lift-cm", type=float, default=2.0,
                        help="规划中段抬高（cm）")
    parser.add_argument("--rounds", type=int, default=3,
                        help="拨动失败重试轮数")
    args = parser.parse_args()

    console = None if args.no_console else ConsoleClient(args.console)
    yolo = None if args.no_yolo else YoloClient(args.yolo)
    alignment = load_alignment_config()
    coarse = alignment["coarse"]
    fine = alignment["fine"]
    coarse_target = (coarse["target_deg"] if args.coarse_target is None
                     else args.coarse_target)
    fine_target = (fine["target_deg"] if args.fine_target is None
                   else args.fine_target)
    coarse_min = (coarse["accept_min_deg"] if args.coarse_tol is None
                  else coarse_target - args.coarse_tol)
    coarse_max = (coarse["accept_max_deg"] if args.coarse_tol is None
                  else coarse_target + args.coarse_tol)
    coarse_cmd_tol = (coarse["command_tolerance_deg"] if args.coarse_tol is None
                      else args.coarse_tol / 2)
    fine_min = (fine["accept_min_deg"] if args.fine_tol is None
                else fine_target - args.fine_tol)
    fine_max = (fine["accept_max_deg"] if args.fine_tol is None
                else fine_target + args.fine_tol)
    fine_cmd_tol = (fine["command_tolerance_deg"] if args.fine_tol is None
                    else args.fine_tol / 2)
    flow = SwitchFlow(client=ReachClient(args.base),
                      console=console,
                      yolo=yolo,
                      coarse_target_deg=coarse_target,
                      coarse_accept_min_deg=coarse_min,
                      coarse_accept_max_deg=coarse_max,
                      coarse_command_tol_deg=coarse_cmd_tol,
                      fine_target_deg=fine_target,
                      fine_accept_min_deg=fine_min,
                      fine_accept_max_deg=fine_max,
                      fine_command_tol_deg=fine_cmd_tol,
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
