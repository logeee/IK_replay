#!/usr/bin/env python3
"""右侧规划（/api/trajectory/plan）"撞了才 RRT"兜底的离线回归。

与 tools/test_axis_last_rrt.py 同款场景：直线路径正中放一团环境体素，
期望端点自动转 RRT（planner=quintic+rrt）且新路径无碰撞、终点不变；
无障碍时行为与原来一致（planner=quintic）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module                     # noqa: E402
from core.types import IKRequest, Pose       # noqa: E402

ROBOT = "h2"
CHAIN = "right_arm"
P_TOOL = [0.27, 0.0, 0.0]


def run_case(with_obstacle: bool):
    model = app_module.robots[ROBOT]
    checker = app_module.collision_checkers[ROBOT]
    checker.enabled = True
    checker.set_environment([], radius=0.03)
    checker.set_environment_exclusions([])
    solver = app_module.solvers[ROBOT]["numerical"]
    names = model.joint_names(CHAIN)
    tcp = Pose(xyz=P_TOOL)

    start_named = dict(zip(names, [0.2, -0.25, 0.0, 0.9, 0.0, -0.1, 0.0]))
    p0 = np.asarray(model.tcp_pose(start_named, CHAIN, tcp).xyz)
    p_target = p0 + np.array([0.05, -0.25, 0.10])

    ik = solver.solve(IKRequest(
        chain_id=CHAIN, current_joints=start_named,
        target_pose=Pose(xyz=p_target.tolist()), tcp_offset=tcp,
        base_link=model.base_link(CHAIN), end_link=model.end_link(CHAIN),
        joint_names=names, seed=start_named,
        solver_options={"solve_orientation": False, "tolerance_mm": 3.0}))
    assert ik.success, f"测试目标 IK 未收敛: {ik.error_mm:.1f}mm"

    if with_obstacle:
        blob_center = p0 + (p_target - p0) * 0.5
        offs = np.array([[0.0, dy, dz] for dy in (-0.02, 0.0, 0.02)
                         for dz in (-0.02, 0.0, 0.02)])
        checker.set_environment(blob_center + offs, radius=0.03)
        checker.set_environment_exclusions([(p_target, 0.12)])
        chk0 = checker.check_state(start_named, CHAIN, tcp)
        assert chk0["status"] != "collision", \
            f"测试场景不合法：起点就撞了 {chk0.get('pair')}"

    payload = app_module.TrajectoryPayload(
        robot=ROBOT, chain_id=CHAIN,
        current_joints=start_named, target_joints=ik.named_target_joints,
        tcp_offset=app_module.PosePayload(xyz=P_TOOL),
        duration=4.0, steps=60, planner_type="quintic", check_collision=True)
    return app_module.plan_trajectory(payload)


def main() -> int:
    free = run_case(with_obstacle=False)
    print(f"无障碍: planner={free['planner']} 碰撞={free['collision']['status']} "
          f"路点 {free['waypoint_count']}")
    assert free["planner"] == "quintic"
    assert free["collision"]["status"] != "collision"

    blocked = run_case(with_obstacle=True)
    col = blocked["collision"]
    print(f"有障碍: planner={blocked['planner']} 碰撞={col['status']} "
          f"路点 {blocked['waypoint_count']}"
          + (f" rrt_error={col.get('rrt_error')}" if col.get("rrt_error") else ""))
    assert blocked["planner"] == "quintic+rrt", "应该转入 RRT 兜底"
    assert col["status"] != "collision", "RRT 路径不应再撞"

    last = blocked["waypoints"][-1]["tcp_pose"]["xyz"]
    free_last = free["waypoints"][-1]["tcp_pose"]["xyz"]
    gap = float(np.linalg.norm(np.asarray(last) - np.asarray(free_last)))
    print(f"终点一致性: 与无障碍规划终点差 {gap * 1000:.1f}mm")
    assert gap < 0.005, "绕障后终点漂了"
    print("\n全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
