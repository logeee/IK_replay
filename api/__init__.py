"""拨动开关全流程的独立封装 API。

不启动/依赖任何前端调试页面，直接按既定流程执行：

  1️⃣ 一键开始
  2️⃣ YOLO 检测远方/就地、是否需要拨动          （待实现，留空）
  3️⃣ 腰部调节把平面指数收进 ±0.5°              （新腰部调节待实现，暂用一键对中占位）
  4️⃣ 测距离
  5️⃣ 按距离选起手式                            （待实现，留空）
      → 腰部调节收进 ±2° 并保持
      → YOLO 识别点位                          （待实现，留空）
      → IK 执行拨动
      → YOLO 复核是否拨动成功                   （待实现，留空）
      → 成功继续，失败回到 5️⃣
  收尾：插值快速回落                             （待实现，留空）

用法（reach_server 需已在运行）：

    from api import SwitchFlow
    result = SwitchFlow().run()

或命令行： python -m api --base http://127.0.0.1:8001
"""

from .client import ReachClient
from .flow import ErrorCode, FlowError, FlowResult, SwitchFlow

__all__ = ["ReachClient", "SwitchFlow", "FlowError", "FlowResult", "ErrorCode"]
