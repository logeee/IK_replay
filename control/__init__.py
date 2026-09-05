"""闭环跟踪与故障监管层（world-frame PINK tracking）。

分层：``ik/``（解目标关节角）→ ``planners/``（出关节轨迹）→ ``control/``
（世界系 TCP 闭环跟踪 + 故障状态机 + 浮动基座估计）→ ``adapters/reach/``（执行）。

来源
----
本包大部分代码移植自同事 Dailin 的 *H2 arm-motion-middleware v1.0.3*
（源码 HEAD ``f276d0846408c9243fda735f61ba9e80c8913faf``，2026-09-04 真机
右臂 Approach→Hold→Return 验证 PASS）。移植时去掉了 cuRobo/GPU 与 Isaac Sim
依赖，规划由本项目 ``ik/``+``planners/`` 承担；执行由本项目 ``H2ArmController``
承担。逐文件对应关系：

===========================  ==========================================
本包文件                      原始文件
===========================  ==========================================
``interfaces.py``            ``control/interfaces.py``
``tool_config.py``           ``control/tool_config.py``
``pink_arm_controller.py``   ``control/pink_arm_controller.py``
``base_pose_predictor.py``   ``control/base_pose_predictor.py``
``approach_tracker.py``      ``control/approach_tracker.py``
``trajectory_reference.py``  ``planning/trajectory_reference.py``
``fault_supervisor.py``      ``arm_motion/fault_supervisor.py``
``floating_base.py``         ``hardware/h2/floating_base_state.py``（输入结构改写）
===========================  ==========================================

依赖约束
--------
* 只依赖 numpy / pinocchio / pin-pink / qpsolvers 与 ``core/``；
* 不得反向 import ``adapters/``、``api/``；
* 18000 的 ``motion_backend`` 为 ``legacy`` 时本包不会被 import，
  因此这里不做任何 eager import，保证 pinocchio 缺失也不影响原方案。
"""

from __future__ import annotations

__all__ = [
    "MIDDLEWARE_SOURCE_VERSION",
    "MIDDLEWARE_SOURCE_COMMIT",
]

MIDDLEWARE_SOURCE_VERSION = "arm-motion-middleware-v1.0.3"
MIDDLEWARE_SOURCE_COMMIT = "f276d0846408c9243fda735f61ba9e80c8913faf"
