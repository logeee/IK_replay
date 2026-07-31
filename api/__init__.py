"""拨动开关全流程的独立封装 API。

不依赖任何前端调试页面，按既定流程执行：

  1️⃣ 一键开始
  2️⃣ YOLO 场景判断（7004 服务）：「就地」= 要拨、「远方」= 无需拨直接结束
      （每处视觉判断连问 3 帧；配了 YOLO 仍没结论 → YOLO_FAILED 退出，
        手臂受控回落，不转人工——自动化不能卡在等人上）
  3️⃣ 腰部粗对齐：平面指数收进 -3° ~ -6°（抬手前，允许真机转身）
  4️⃣ 测距离
  5️⃣ 按距离自动选起手式（序列名带距离门槛，如「0.44避障起手式」需 ≥0.44m；
      距离太近 → POSE_UNAVAILABLE 错误码退出），并按距离补位：
      ≥0.5m 加摆「0.5以上」、0.46~0.5m 直接取点、0.44~0.46m 补到「0.44终点」
      → 6️⃣ 细对齐并保持 -3°±4°（抬手会把躯干带偏 4~8°，转身纠偏，服务端限幅；
        基座对 6°/s 不跟随时自动提速到 20°/s 再试，仍不动才判运控未响应）
      → YOLO 识别点位：「就地」框 + 固定相对偏移
        （目标点上抬补重力下垂：≥0.52m 首轮就垫 1cm，每重试一轮再 +1cm，
          总封顶 3cm）
      → IK 执行拨动（取点偏移0 → 左侧规划抬高2cm → 到位6s
        → 左移6cm+推力25N，拨完就地停住）
      → YOLO 立即复核：「远方」= 成功、「就地」= 复看一眼仍「就地」= 失败
      → 成功直接收尾；失败插值回「<距离>终点」路点重试
  收尾：快速插值到「起手点测试」路点 → 到位立即释放手臂

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
