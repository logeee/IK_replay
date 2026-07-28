"""拨动开关全流程的独立封装 API。

不依赖任何前端调试页面，按既定流程执行：

  1️⃣ 一键开始
  2️⃣ YOLO 场景判断（7004 服务）：「就地」= 要拨、「远方」= 无需拨直接结束
      （YOLO 不可达/没识别到 → 转 7002 确认台人工判断）
  3️⃣ 腰部粗对齐：平面指数收进 -3° ~ -6°
  4️⃣ 测距离
  5️⃣ 按距离自动选起手式（序列名带距离门槛，如「0.46起手式」需 ≥0.46m；
      距离太近 → POSE_UNAVAILABLE 错误码退出）
      → 6️⃣ 腰部细对齐收进 -3°±2° 并保持
      → YOLO 识别点位                          （未部署 → 确认台画面点选）
      → IK 执行拨动（取点偏移0 → 左侧规划抬高2cm → 确认 → 到位6s
        → 左移6cm+推力25N → 关节插值回「<距离>终点」路点）
      → YOLO 复核：「远方」= 成功、「就地」= 失败（没识别到 → 确认台人工）
      → 成功继续，失败回到 5️⃣
  收尾：关节插值到「起手点测试」路点 → 释放手臂

用法（reach_server 需已在运行）：

  终端 A：python -m api.console          # 7002 人工确认台（fastapi 环境）
  终端 B：/home/robot/miniconda3/envs/yolo/bin/python -m api.yolo_server \\
              --model skip_yolo_file/Xuanniu.pt   # 7004 YOLO 常驻推理
  浏览器：http://<机器人IP>:7002/         # 常驻相机画面 + 问题卡片
  终端 C：python -m api                  # 跑全流程（fastapi 环境）

或纯代码：
    from api import SwitchFlow, ConsoleClient
    result = SwitchFlow(console=ConsoleClient()).run()
"""

from .client import ReachClient
from .console_client import ConsoleAbort, ConsoleClient
from .flow import ErrorCode, FlowError, FlowResult, SwitchFlow
from .yolo_client import YoloClient

__all__ = ["ReachClient", "SwitchFlow", "FlowError", "FlowResult", "ErrorCode",
           "ConsoleClient", "ConsoleAbort", "YoloClient"]
