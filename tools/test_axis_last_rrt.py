#!/usr/bin/env python3
"""左侧规划"撞了才 RRT"兜底的离线回归。

场景：在 TCP 直线路径正中放一团环境体素，直线必撞；期望 worker 自动转
RRT-Connect，返回 planner=axis_last+rrt 且新路径无碰撞。再跑一个无障碍
场景确认不撞时行为与原来完全一致（planner=axis_last）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters import reach                                # noqa: E402
from core.collision import ConfigurableCollisionChecker   # noqa: E402
from core.robot_config import load_robot_config           # noqa: E402
from core.robot_model import RobotModel                   # noqa: E402
from ik.numerical_solver import NumericalIKSolver         # noqa: E402

CHAIN = "right_arm"


class FakeConn:
    def __init__(self):
        self.payload = None

    def send(self, obj):
        self.payload = obj

    def close(self):
        pass


def run_case(with_obstacle: bool):
    model = RobotModel(load_robot_config(ROOT / "config" / "robots" / "h2.yaml"))
    checker = ConfigurableCollisionChecker(model)
    checker.enabled = True
    st = reach.state
    st.robot_model = model
    st.collision_checker = checker
    st.ik_solver = NumericalIKSolver(model)
    st.chain_id = CHAIN
    st.joint_names = model.joint_names(CHAIN)
    st.p_tool = [0.27, 0.0, 0.0]

    start_named = dict(zip(st.joint_names, [0.2, -0.25, 0.0, 0.9, 0.0, -0.1, 0.0]))
    from core.types import Pose
    tcp = Pose(xyz=st.p_tool)
    p0 = np.asarray(model.tcp_pose(start_named, CHAIN, tcp).xyz)
    # 位移放大到 ~28cm：碰撞体不小（TCP 球 6cm、体素球 3cm），路径太短
    # 的话障碍放哪都会蹭到端点，测不出"中段撞、端点自由"的目标场景
    p_target = p0 + np.array([0.05, -0.25, 0.10])
    p_mid = np.array([p0[0], p_target[1], p_target[2] + 0.02])

    if with_obstacle:
        # 平移段正中放一小团体素：挡住直线，起点/终点须离它足够远
        blob_center = p0 + (p_mid - p0) * 0.5
        offs = np.array([[0.0, dy, dz] for dy in (-0.02, 0.0, 0.02)
                         for dz in (-0.02, 0.0, 0.02)])
        checker.set_environment(blob_center + offs, radius=0.03)
        checker.set_environment_exclusions([(p_target, 0.12)])
        # 场景合法性自检：起点必须无碰撞（终点由 worker 内部复核）
        chk0 = checker.check_state(start_named, CHAIN, tcp)
        assert chk0["status"] != "collision", \
            f"测试场景不合法：起点就撞了 {chk0.get('pair')}"

    conn = FakeConn()
    reach._axis_last_worker(conn, start_named, p0.tolist(), p_mid.tolist(),
                            p_target.tolist(), 1.0, 0.01, True)
    status, payload = conn.payload
    assert status == "ok", f"worker 失败: {payload}"
    return payload


def main() -> int:
    free = run_case(with_obstacle=False)
    print(f"无障碍: planner={free['planner']} 碰撞={free['collision']['status']} "
          f"路点 {len(free['waypoints'])}")
    assert free["planner"] == "axis_last"
    assert free["collision"]["status"] != "collision"

    blocked = run_case(with_obstacle=True)
    col = blocked["collision"]
    print(f"有障碍: planner={blocked['planner']} 碰撞={col['status']} "
          f"路点 {len(blocked['waypoints'])}"
          + (f" rrt_error={col.get('rrt_error')}" if col.get("rrt_error") else ""))
    assert blocked["planner"] == "axis_last+rrt", "应该转入 RRT 兜底"
    assert col["status"] != "collision", "RRT 路径不应再撞"
    # 终点必须没变：绕障只改路径形状，不改目的地
    last = blocked["waypoints"][-1]["tcp_pose"]["xyz"]
    free_last = free["waypoints"][-1]["tcp_pose"]["xyz"]
    gap = float(np.linalg.norm(np.asarray(last) - np.asarray(free_last)))
    print(f"终点一致性: 与无障碍规划终点差 {gap * 1000:.1f}mm")
    assert gap < 0.005, "绕障后终点漂了"
    print("\n全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
