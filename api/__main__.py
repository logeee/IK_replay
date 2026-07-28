"""命令行入口：python -m api [--base http://127.0.0.1:8001]"""

from __future__ import annotations

import argparse
import sys

from .client import ReachClient
from .flow import SwitchFlow


def main() -> int:
    parser = argparse.ArgumentParser(description="拨动开关全流程（无前端）")
    parser.add_argument("--base", default="http://127.0.0.1:8001",
                        help="reach_server 地址")
    parser.add_argument("--coarse-tol", type=float, default=0.5,
                        help="3️⃣ 平面指数粗收敛阈值（°）")
    parser.add_argument("--fine-tol", type=float, default=2.0,
                        help="5️⃣ 保持阶段阈值（°）")
    parser.add_argument("--rounds", type=int, default=3,
                        help="拨动失败重试轮数")
    args = parser.parse_args()

    flow = SwitchFlow(client=ReachClient(args.base),
                      coarse_tol_deg=args.coarse_tol,
                      fine_tol_deg=args.fine_tol,
                      max_flip_rounds=args.rounds)
    result = flow.run()
    print(f"[flow] 结果: ok={result.ok} code={result.code.name} "
          f"message={result.message} detail={result.detail}")
    return 0 if result.ok else int(result.code)


if __name__ == "__main__":
    sys.exit(main())
