"""Reach adapter：点击相机画面 → 3D 目标 → IK 预演 → 确认后真机执行。

按 README 的约定以"可选 adapter"形式挂在离线 API 外围：
不改动核心求解/规划代码，reach_server.py 启动时注入相机、
手眼标定结果和（可选的）H2 手臂控制器。不启用时主应用行为不变。

坐标链：
  像素(u,v) --深度反投影--> P_camera --T_cam2base--> P_torso
  --T_root_torso(全零关节)--> P_root（IK/查看器使用的 URDF 根坐标系）

由于 IK 链的 base 是 torso_link、查看器/求解器都在"腰部关节为 0"的
模型上工作，上述换算与真机腰部实际姿态无关（解出的只是手臂关节角）。

模块划分（原单文件 reach.py 按职责拆分，对外接口不变）：
  state       ReachState / configure / 关节与躯干读取
  service     /status /motors
  perception  /stream /pick /scan_obstacles 及平面拟合
  locomotion  /perpendicular /turn /align_yaw /hold_record（LocoClient 转身）
  planning    /plan_cartesian /plan_axis_last（逐步 IK + RRT 兜底）
  recordings  /waypoints /sequences /sidesteps（录制与回放）
  execution   /arm /disarm /execute /stop /diagnostics（真机执行）
"""

from __future__ import annotations

from .state import ReachState, configure, router, state

# 导入各模块以注册路由（execution 先于依赖它的模块被隐式加载，顺序无碍：
# 各路由路径互不冲突）
from . import (execution, hand, locomotion, perception, planning, tcp,  # noqa: E402,F401
               pointcloud_source, recordings, service)
from .planning import _axis_last_worker  # noqa: F401  tools/test_axis_last_rrt.py 在用

__all__ = ["ReachState", "configure", "router", "state"]
