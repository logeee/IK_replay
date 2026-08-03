"""笛卡尔直线插补与「平移在先、进出在后」主段规划（撞障时 RRT-Connect 兜底）。"""

from __future__ import annotations

import multiprocessing as mp
import time

import numpy as np
from fastapi.responses import JSONResponse

from .state import router, state


# --------------- 笛卡尔直线插补（沿面横移用） ---------------


def _cartesian_line(start_named: dict, p_from: np.ndarray, p_to: np.ndarray,
                    step: float) -> tuple[list[dict], dict, float]:
    """TCP 从 p_from 直线走到 p_to：切小步逐步 IK（前一步做种子）。

    返回 (路点列表[不含起点], 终点关节, 最大 IK 误差 mm)；IK 不收敛抛 ValueError。
    """
    from core.types import IKRequest, Pose

    model = state.robot_model
    tcp_offset = Pose(xyz=list(state.p_tool))
    total = float(np.linalg.norm(p_to - p_from))
    if total < 1e-6:
        return [], start_named, 0.0
    n_steps = max(2, int(np.ceil(total / max(step, 1e-4))))
    out: list[dict] = []
    q_prev = start_named
    max_err = 0.0
    for i in range(1, n_steps + 1):
        target = p_from + (p_to - p_from) * (i / n_steps)
        res = state.ik_solver.solve(IKRequest(
            chain_id=state.chain_id,
            current_joints=q_prev,
            target_pose=Pose(xyz=target.tolist()),
            tcp_offset=tcp_offset,
            base_link=model.base_link(state.chain_id),
            end_link=model.end_link(state.chain_id),
            joint_names=state.joint_names,
            seed=q_prev,
            solver_options={"solve_orientation": False, "tolerance_mm": 3.0},
        ))
        if not res.success:
            raise ValueError(f"第 {i}/{n_steps} 步 IK 未收敛（{res.error_mm:.1f} mm），"
                             "直线可能超出可达范围")
        max_err = max(max_err, float(res.error_mm))
        q_prev = res.named_target_joints
        out.append({"named_joints": q_prev, "tcp_pose": res.tcp_pose.to_dict()})
    return out, q_prev, max_err


def _attach_collision(waypoints: list[dict], check: bool) -> dict | None:
    """按需给路点标注逐帧碰撞并返回摘要。"""
    if not check or state.collision_checker is None:
        return None
    from core.types import Pose
    tcp_offset = Pose(xyz=list(state.p_tool))
    checks = state.collision_checker.check_trajectory(
        waypoints, state.chain_id, tcp_offset)
    collision = state.collision_checker.summarize_checks(checks)
    for wp, chk in zip(waypoints, checks):
        wp["collision"] = {"status": chk["status"],
                           "status_label": chk["status_label"],
                           "min_distance_mm": chk["min_distance_mm"]}
    return collision


@router.post("/plan_cartesian")
def reach_plan_cartesian(body: dict):
    """指尖沿直线平移的轨迹：把总位移切成小步，逐步 IK（前一步做种子），
    TCP 全程钉在直线上——不会像关节空间插值那样中途下沉再抬起。

    Body: {"start_joints": named, "direction_root": [x,y,z], "distance_m": float,
           "step_m": 0.01}
    """
    if state.ik_solver is None:
        return JSONResponse({"ok": False, "error": "IK 求解器未注入"}, status_code=409)
    from core.types import Pose

    try:
        start_named = {str(k): float(v) for k, v in dict(body["start_joints"]).items()}
        direction = np.asarray(body["direction_root"], dtype=float).reshape(3)
        distance = float(body["distance_m"])
        step = abs(float(body.get("step_m", 0.01)))
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"参数非法: {exc}"}, status_code=400)
    norm = float(np.linalg.norm(direction))
    if norm < 1e-9 or abs(distance) < 1e-6:
        return JSONResponse({"ok": False, "error": "方向/距离为零"}, status_code=400)
    direction /= norm

    tcp_offset = Pose(xyz=list(state.p_tool))
    p0 = np.asarray(state.robot_model.tcp_pose(start_named, state.chain_id, tcp_offset).xyz)
    waypoints = [{"index": 0, "named_joints": start_named,
                  "tcp_pose": {"xyz": p0.tolist(), "rpy": [0.0, 0.0, 0.0]}}]
    try:
        seg, _, max_err = _cartesian_line(start_named, p0, p0 + direction * distance, step)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    waypoints.extend(seg)
    for i, wp in enumerate(waypoints):
        wp["index"] = i

    collision = _attach_collision(waypoints, bool(body.get("check_collision", True)))
    return {"ok": True, "waypoints": waypoints, "collision": collision,
            "max_ik_error_mm": max_err, "steps": len(waypoints) - 1}


@router.post("/plan_axis_last")
def reach_plan_axis_last(body: dict):
    """「平移在先、进出在后」的取点主段规划（左侧规划）。

    把当前指尖到目标的位移按根系 ±x（机器人前后）分解成两段直线：
      · 往里伸（目标更靠前）：先在当前深度做竖直+水平平移对齐，最后一段
        纯 +x 往里伸——绝不提前探进去；
      · 往外拔（目标更靠后）：先纯 -x 拔出到目标深度，再做平移——
        平移永远发生在离面板较远的那个深度。
    两段都是笛卡尔直线（逐步 IK，TCP 钉在线上）。

    Body: {"start_joints": named, "target_root": [x,y,z],
           "step_m": 0.01, "check_collision": bool=true, "lift_m": 0.02}

    lift_m（中段抬高，仅往里伸时生效）：真实执行时实测位置拖在指令下方
    1~3cm（运动中的跟踪下垂），目标点又常贴着障碍下表面——平移段故意
    多抬 lift_m，进给段从【上前方】斜切进目标，真实路径全程高于目标高度，
    不会从下面钻进去刮底。终点精度不受影响（终点靠稳态收敛）。给 0 关闭。
    """
    if state.ik_solver is None:
        return JSONResponse({"ok": False, "error": "IK 求解器未注入"}, status_code=409)
    from core.types import Pose

    try:
        start_named = {str(k): float(v) for k, v in dict(body["start_joints"]).items()}
        p_target = np.asarray(body["target_root"], dtype=float).reshape(3)
        step = abs(float(body.get("step_m", 0.01)))
        lift = max(0.0, float(body.get("lift_m", 0.02)))
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"参数非法: {exc}"}, status_code=400)

    tcp_offset = Pose(xyz=list(state.p_tool))
    p0 = np.asarray(state.robot_model.tcp_pose(start_named, state.chain_id, tcp_offset).xyz)
    dx = float(p_target[0] - p0[0])
    if dx >= 0.0:
        # 往里伸：平移段（保持当前 x，高度多抬 lift），进给段从上前方斜切入目标
        p_mid = np.array([p0[0], p_target[1], p_target[2] + lift])
    else:
        # 往外拔：先退到目标深度（纯 -x），再平移
        p_mid = np.array([p_target[0], p0[1], p0[2]])

    # 逐步 IK 是纯 Python 计算，在服务进程里会和 50Hz 控制环/相机线程抢
    # GIL（同序列 RRT 的老问题：离线亚秒、在线好几秒）。fork 子进程独享
    # GIL，恢复离线速度；state/求解器由 fork 语义直接继承，零序列化成本。
    ctx = mp.get_context("fork")
    rx, tx = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_axis_last_worker, daemon=True,
                       args=(tx, start_named, p0.tolist(), p_mid.tolist(),
                             p_target.tolist(), dx, step,
                             bool(body.get("check_collision", True))))
    proc.start()
    tx.close()
    if rx.poll(30.0):
        plan_status, payload = rx.recv()
    else:
        plan_status, payload = "err", "规划子进程 30s 无响应"
    proc.join(timeout=1.0)
    if proc.is_alive():
        proc.kill()
    if plan_status != "ok":
        return JSONResponse({"ok": False, "error": str(payload)}, status_code=422)
    payload["mid_root"] = p_mid.tolist()
    return payload


def _axis_last_worker(conn, start_named, p0_list, p_mid_list, p_target_list,
                      dx, step, check_collision):
    """fork 子进程里跑左侧规划的逐步 IK + 碰撞标注（独享 GIL）。"""
    try:
        p0 = np.asarray(p0_list, dtype=float)
        p_mid = np.asarray(p_mid_list, dtype=float)
        p_target = np.asarray(p_target_list, dtype=float)
        waypoints = [{"named_joints": start_named,
                      "tcp_pose": {"xyz": p0.tolist(), "rpy": [0.0, 0.0, 0.0]}}]
        max_err = 0.0
        q = start_named
        for label, a, b in (("平移" if dx >= 0 else "拔出", p0, p_mid),
                            ("进给" if dx >= 0 else "平移", p_mid, p_target)):
            try:
                seg, q, err = _cartesian_line(q, a, b, step)
            except ValueError as exc:
                conn.send(("err", f"{label}段: {exc}"))
                return
            max_err = max(max_err, err)
            waypoints.extend(seg)
        for i, wp in enumerate(waypoints):
            wp["index"] = i
        if len(waypoints) < 2:
            conn.send(("err", "位移为零，无需规划"))
            return
        collision = _attach_collision(waypoints, check_collision)
        planner = "axis_last"
        if (check_collision and collision
                and collision.get("status") == "collision"
                and state.collision_checker is not None):
            # 两段直线撞了 → RRT-Connect 关节空间绕障（同序列执行的
            # line-else-rrt 策略）。起终点上已存在的碰撞对自动豁免
            # （指尖贴面等），目标周围的环境豁免球在 pick 时已设好。
            # 注意：绕障路径不再保证"平移在先、进给在后"的两段形状。
            waypoints, collision, planner = _axis_last_rrt_fallback(
                start_named, waypoints, collision)
        conn.send(("ok", {"ok": True, "waypoints": waypoints, "collision": collision,
                          "max_ik_error_mm": max_err, "planner": planner,
                          "mode": "push_in" if dx >= 0 else "pull_out",
                          "steps": len(waypoints) - 1}))
    except Exception as exc:
        conn.send(("err", f"规划子进程异常: {exc}"))
    finally:
        conn.close()


def _axis_last_rrt_fallback(start_named: dict, waypoints: list[dict],
                            collision: dict) -> tuple[list[dict], dict, str]:
    """左侧规划直线撞障时的 RRT-Connect 绕障兜底（在 fork 子进程里跑）。

    成功：返回按 0.04rad 帧距重采样的新路径 + 重算的碰撞标注 + "axis_last+rrt"。
    失败：原路径原标注返回，collision 里带 rrt_error 说明绕不过去的原因，
    由人看着碰撞可视化决定走不走。
    """
    from core.types import Pose
    from planners.rrt import densify, rrt_connect_path

    names = state.robot_model.joint_names(state.chain_id)
    tcp_offset = Pose(xyz=list(state.p_tool))
    q_start = np.asarray([float(start_named[n]) for n in names], dtype=float)
    q_goal = np.asarray([float(waypoints[-1]["named_joints"][n]) for n in names],
                        dtype=float)
    # 终点是算出来的姿态（不是真机到过的），它撞障就是真警报：绕障无意义，
    # 直接报出撞的是哪一对，让人挪目标/重扫障
    goal_chk = state.collision_checker.check_state(
        [float(v) for v in q_goal], state.chain_id, tcp_offset)
    if goal_chk["status"] == "collision":
        pair = goal_chk.get("pair") or {}
        collision["rrt_error"] = (f"终点姿态本身撞障（{pair.get('a', '?')} ↔ "
                                  f"{pair.get('b', '?')}），无路可绕")
        print(f"[reach] 左侧规划撞障，RRT 不适用: {collision['rrt_error']}")
        return waypoints, collision, "axis_last"
    print("[reach] 左侧规划直线撞障 → 转 RRT-Connect 绕障…")
    t0 = time.perf_counter()
    try:
        path = rrt_connect_path(state.robot_model, state.collision_checker,
                                state.chain_id, q_start, q_goal, tcp_offset,
                                {"timeout_s": 15.0, "exempt_goal": False})
    except ValueError as exc:
        collision["rrt_error"] = str(exc)
        print(f"[reach] RRT 绕障失败（{time.perf_counter() - t0:.1f}s）: {exc}")
        return waypoints, collision, "axis_last"
    print(f"[reach] RRT 绕障成功（{time.perf_counter() - t0:.1f}s，"
          f"折线 {len(path)} 节点）")

    out: list[dict] = []
    for i, qv in enumerate(densify(path, 0.04)):
        joints = [float(v) for v in qv]
        tcp = state.robot_model.tcp_pose(joints, state.chain_id, tcp_offset)
        out.append({"index": i,
                    "named_joints": state.robot_model.named_chain_joints(
                        joints, state.chain_id),
                    "tcp_pose": {"xyz": [float(v) for v in tcp.xyz],
                                 "rpy": [0.0, 0.0, 0.0]}})
    return out, _attach_collision(out, True), "axis_last+rrt"
