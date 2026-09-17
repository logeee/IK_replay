"""Recorded cabinet-relative TCP orientation at the 7005 target pose only.

Planning is offline. Only /execute can start a controller, using a server-issued
plan id tied to the pick, tool, world anchor and exact joint path.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import uuid
from copy import deepcopy
from pathlib import Path

import numpy as np
from fastapi.responses import JSONResponse
from scipy.spatial.transform import Rotation

from core.types import IKRequest, Pose, TrajectoryRequest
from core.utils import matrix_to_rpy
from .state import router, state

REFERENCES = Path(__file__).resolve().parents[2] / "data/orientation_references"
TOLERANCE_DEG = 2.0


def rotation(value, label="朝向"):
    r = np.asarray(value, dtype=float)
    if (r.shape != (3, 3) or not np.isfinite(r).all()
            or not np.allclose(r.T @ r, np.eye(3), atol=1e-6)
            or not np.isclose(np.linalg.det(r), 1., atol=1e-6)):
        raise ValueError(f"{label}不是有效旋转矩阵")
    return r


def angle_deg(actual, target):
    return float(np.degrees(Rotation.from_matrix(rotation(target).T @ rotation(actual)).magnitude()))


def load_reference(reference_id):
    if not isinstance(reference_id, str) or not reference_id or Path(reference_id).name != reference_id:
        raise ValueError("朝向记录名称非法")
    path = REFERENCES / reference_id / "reference.json"
    if not path.is_file() or not path.resolve().is_relative_to(REFERENCES.resolve()):
        raise ValueError("朝向记录不存在")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("robot") != state.robot_id or value.get("arm") != state.chain_id:
        raise ValueError("朝向记录与当前机器人或手臂不一致")
    if value.get("wrist_link") != state.robot_model.end_link(state.chain_id):
        raise ValueError("朝向记录的末端连杆不匹配")
    if value.get("hand_id") != (state.active_combo or {}).get("hand_id"):
        raise ValueError("朝向记录的手型号不匹配")
    if state.T_wrist2hand is None or not np.allclose(
        np.asarray(value.get("T_wrist_from_hand")), state.T_wrist2hand, atol=1e-6,
    ):
        raise ValueError("手部安装标定已变化，请重新记录朝向")
    rotation(value["tcp"]["R_cabinet_from_frame"], "记录的 TCP 朝向")
    return value


def load_orientation_source(reference_id, reference_kind="recorded_orientation"):
    """Return a cabinet-relative rotation; never reuse the recorded base rotation."""
    if reference_kind == "recorded_orientation":
        return load_reference(reference_id)
    if reference_kind != "cabinet_waypoint_orientation":
        raise ValueError("未知朝向来源")
    from .cabinet_waypoints import archive, _saved_geometry
    waypoint, _, directory, digest = archive(reference_id)
    panel_tcp, _, _, _ = _saved_geometry(str(directory), digest)
    return {"name": waypoint["name"], "record_digest": digest,
            "tcp": {"R_cabinet_from_frame": rotation(panel_tcp[:3, :3]).tolist()}}


@router.get("/orientation/references")
def references():
    entries = []
    for path in sorted(REFERENCES.glob("*/reference.json")):
        try:
            value = load_reference(path.parent.name)
            entries.append({"id": path.parent.name, "name": value["name"],
                            "recorded_at": value.get("recorded_at"),
                            "rpy_deg": value["tcp"]["rpy_deg"]})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {"ok": True, "references": entries, "tolerance_deg": TOLERANCE_DEG}


def cabinet_rotation():
    plane = state.plane or {}
    if (plane.get("horizontal_axis_source") != "wall_coordinate_x"
            or plane.get("wall_up_root") is None):
        raise ValueError("当前目标没有完整柜面坐标系，请在 7005 重新找点并确认目标")
    x = np.asarray(plane["right_root"], dtype=float)
    z = np.asarray(plane["wall_up_root"], dtype=float)
    return rotation(np.column_stack((x, np.cross(z, x), z)), "柜面坐标系")


def path_key(q_list):
    return hashlib.sha256(np.asarray(q_list, dtype='<f8').tobytes()).hexdigest()


def reference_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check_joint_path(q_list):
    """Validate joint data/limits; intermediate TCP orientation is unrestricted."""
    q = np.asarray(q_list, dtype=float)
    names = state.joint_names
    if q.ndim != 2 or q.shape[1] != len(names) or len(q) < 1 or not np.isfinite(q).all():
        raise ValueError("轨迹关节数据无效")
    lo, hi = state.robot_model.joint_limits(state.chain_id, names)
    if np.any(q < lo - 1e-6) or np.any(q > hi + 1e-6):
        raise ValueError("轨迹超出关节限位")
    return q


def check_goal_pose(actual, position, orientation, position_tolerance_mm=2., tolerance=TOLERANCE_DEG):
    """Called only for the planned endpoint or measured arrival, never en route."""
    actual = np.asarray(actual, dtype=float)
    if actual.shape != (4, 4) or not np.isfinite(actual).all():
        raise ValueError("终点位姿数据无效")
    error_mm = float(np.linalg.norm(actual[:3, 3] - np.asarray(position)) * 1000.)
    error_deg = angle_deg(actual[:3, :3], orientation)
    if error_mm > position_tolerance_mm or error_deg > tolerance:
        raise ValueError(f"终点位姿未达到：位置误差 {error_mm:.2f} mm（允许 ≤ {position_tolerance_mm:g} mm），"
                         f"朝向误差 {error_deg:.2f}°（允许 ≤ {tolerance:g}°）")
    return {"position_error_mm": error_mm, "orientation_error_deg": error_deg}


def parse_arrival_tolerance(value):
    if not isinstance(value, dict) or set(value) != {"position_mm", "orientation_deg"}:
        raise ValueError("到位容差需同时填写位置容差和朝向容差")
    result = {}
    for key, high, label in (("position_mm", 100., "位置容差（mm）"),
                             ("orientation_deg", 45., "朝向容差（°）")):
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not np.isfinite(raw) or not .1 <= raw <= high:
            raise ValueError(f"{label}必须在 0.1～{high:g} 之间")
        result[key] = float(raw)
    return result


def check_arrival_pose(actual, position, orientation, constraint):
    limits = constraint.get("arrival_tolerance") or {"position_mm": 10., "orientation_deg": TOLERANCE_DEG}
    return check_goal_pose(actual, position, orientation,
                           position_tolerance_mm=limits["position_mm"], tolerance=limits["orientation_deg"])


def joint_pose(q):
    return state.robot_model.tcp_matrix(q, state.chain_id, Pose(xyz=list(state.p_tool)))


def plan_path(start, target, r_target, r_cabinet, kind, lift, check_collision, via_joints=(), arrival_tolerance=None):
    from .planning import _attach_collision, _axis_last_rrt_fallback
    from planners.quintic import QuinticTrajectoryPlanner
    from planners.rrt import densify
    model, chain, names = state.robot_model, state.chain_id, state.joint_names
    offset = Pose(xyz=list(state.p_tool))
    q_start = check_joint_path([[float(start[n]) for n in names]])[0]
    via = [check_joint_path([[float(w[n]) for n in names]])[0] for w in via_joints]
    if kind != "direct" and via:
        raise ValueError("左侧规划不使用经由关节路点，请选择右侧规划")
    seed = via[-1] if via else q_start
    # Direct A/B arrival limits apply to IK acceptance and endpoint validation.
    # The optimizer still minimizes error to the requested pose; these limits
    # only determine acceptance after optimization. Other modes keep defaults.
    limits = arrival_tolerance or {"position_mm": 2., "orientation_deg": TOLERANCE_DEG}
    position_limit = limits["position_mm"]
    angle_limit = limits["orientation_deg"]
    ik_angle_limit = angle_limit if arrival_tolerance is not None else .35
    # Only this endpoint IK has an orientation target.
    goal = state.ik_solver.solve(IKRequest(
        chain_id=chain, current_joints=seed, seed=seed,
        target_pose=Pose(xyz=target.tolist(), rpy=matrix_to_rpy(r_target)), tcp_offset=offset,
        base_link=model.base_link(chain), end_link=model.end_link(chain), joint_names=names,
        solver_options={"solve_orientation": True, "rotation_weight": .5,
                        "regularization_weight": .001, "tolerance_mm": position_limit,
                        "rotation_tolerance_deg": ik_angle_limit, "max_iterations": 400},
    ))
    if not goal.success:
        raise ValueError(f"目标位姿 IK 求解未通过：位置误差 {goal.error_mm:.2f} mm（允许 ≤ {position_limit:g} mm），"
                         f"朝向误差 {np.degrees(goal.error_rotation):.2f}°（允许 ≤ {ik_angle_limit:g}°）；"
                         "请调整目标位置或目标朝向后重新规划")
    q_goal = np.asarray(goal.target_joints)
    goal_error = check_goal_pose(joint_pose(q_goal), target, r_target, position_limit, angle_limit)
    if np.max(np.abs(q_goal-q_start)) < 1e-8 and not via:
        raise ValueError("当前已在目标位姿，无需规划")

    if kind == "direct":
        # Same joint-space planner as the original right-side planning button.
        knots = [q_start, *via, q_goal]
        path = []
        segments = []
        for a, b in zip(knots[:-1], knots[1:]):
            count = max(20, int(np.ceil(1.875*np.max(np.abs(b-a))/.025))+1)
            segment = QuinticTrajectoryPlanner(model).plan(TrajectoryRequest(
                chain, a, b, offset, 1., count))
            segments.append([np.asarray(w.joints) for w in segment])
            path.extend(np.asarray(w.joints) for w in (segment if not path else segment[1:]))
    else:
        # Preserve original left-side XYZ order (root X insertion/retraction).
        # Interior IK solves position only; a smoothly changing seed leads into
        # the endpoint solution without pinning the TCP orientation to anything.
        p0 = joint_pose(q_start)[:3, 3]
        mid = (np.array([p0[0], target[1], target[2]+lift]) if target[0] >= p0[0]
               else np.array([target[0], p0[1], p0[2]]))
        points = [p0, mid, target]
        lengths = np.array([np.linalg.norm(b-a) for a,b in zip(points[:-1],points[1:])])
        if float(lengths.sum()) > 2.:
            raise ValueError("目标距离过远")
        counts = np.maximum(1, np.ceil(lengths/.01).astype(int))
        samples = max(20, int(np.ceil(np.max(np.abs(q_goal-q_start))/.025)))
        weights = lengths / lengths.sum() if lengths.sum() > 1e-8 else np.array([.5,.5])
        counts = np.maximum(counts, np.ceil(weights*samples).astype(int))
        path = [q_start]
        done, total = 0, int(counts.sum())
        for a, b, count in zip(points[:-1], points[1:], counts):
            for i in range(1, int(count)+1):
                done += 1
                if done == total:
                    path.append(q_goal)
                    continue
                u = done / total
                seed = q_start + (q_goal-q_start)*(10*u**3-15*u**4+6*u**5)
                result = state.ik_solver.solve(IKRequest(
                    chain_id=chain, current_joints=path[-1], seed=seed,
                    target_pose=Pose(xyz=(a+(b-a)*i/count).tolist()), tcp_offset=offset,
                    base_link=model.base_link(chain), end_link=model.end_link(chain), joint_names=names,
                    solver_options={"solve_orientation": False, "tolerance_mm": 2.,
                                    "regularization_weight": .001, "max_iterations": 240},
                ))
                if not result.success:
                    raise ValueError(f"中间位置 IK 不可达：位置误差 {result.error_mm:.2f} mm")
                path.append(np.asarray(result.target_joints))
        path = densify(path, .025)

    check_joint_path(path)
    def describe(values):
        return [{"index": i, "named_joints": model.named_chain_joints(q, chain),
                 "tcp_pose": model.tcp_pose(q, chain, offset).to_dict()}
                for i,q in enumerate(values)]

    waypoints = describe(path)
    collision = _attach_collision(waypoints, check_collision)
    planner = "goal_pose/" + kind
    if collision and collision.get("status") == "collision":
        if via:
            # Preserve requested via points when rerouting: solve each leg.
            rerouted = []
            for segment in segments:
                part = describe(segment)
                checked = _attach_collision(part, True)
                if checked and checked.get("status") == "collision":
                    part, checked, _ = _axis_last_rrt_fallback(part[0]["named_joints"], part, checked)
                    if checked and checked.get("status") == "collision":
                        raise ValueError("经由路点轨迹存在碰撞：" + str(checked.get("rrt_error") or "请调整路点"))
                rerouted.extend(part if not rerouted else part[1:])
            waypoints = rerouted
            collision = _attach_collision(waypoints, True)
            fallback = "via+rrt"
        else:
            waypoints, collision, fallback = _axis_last_rrt_fallback(start, waypoints, collision)
        if collision and collision.get("status") == "collision":
            raise ValueError("轨迹存在碰撞：" + str(collision.get("rrt_error") or "请调整目标"))
        if fallback.endswith("+rrt"):
            planner += "+rrt"
        # The shared RRT helper reports dummy RPY; show the actual FK orientation.
        for i, waypoint in enumerate(waypoints):
            waypoint["index"] = i
            waypoint["tcp_pose"] = model.tcp_pose(waypoint["named_joints"], chain, offset).to_dict()
    check_joint_path([[w["named_joints"][n] for n in names] for w in waypoints])
    check_goal_pose(joint_pose(waypoints[-1]["named_joints"]), target, r_target, position_limit, angle_limit)
    return {"ok": True, "waypoints": waypoints, "collision": collision,
            "goal_position_error_mm": goal_error["position_error_mm"],
            "goal_orientation_error_deg": goal_error["orientation_error_deg"],
            "target_pose": Pose(xyz=target.tolist(), rpy=matrix_to_rpy(r_target)).to_dict(),
            "planner": planner, "steps": len(waypoints)-1}


def _worker(connection, args):
    try:
        connection.send((True, plan_path(*args)))
    except Exception as exc:
        connection.send((False, str(exc)))
    finally:
        connection.close()


def pick_stamp(require_world=False):
    if (state.pick_context or {}).get("selection_mode") != "frozen_rgbd_pointcloud":
        raise ValueError("请在 7005 取点并确认目标")
    rt = state.pink_runtime
    from .execution import _pink_scope_error
    if rt is not None and rt.world_frame.anchored and not _pink_scope_error("主轨迹"):
        return (state.pick_revision, rt.world_frame.anchor_count,
                np.asarray(rt.pick_world_T_root).copy())
    if require_world:
        raise ValueError("PINK 执行需要先锚定世界系，再在 7005 重新取点并规划")
    return state.pick_revision, None, None


@router.post("/plan_orientation")
def plan_orientation(body: dict):
    try:
        reference_id = body.get("reference_id")
        reference_kind = body.get("reference_kind", "recorded_orientation")
        reference = load_orientation_source(reference_id, reference_kind)
        control_revision = state.cabinet_plan_revision
        revision, anchor, world_root = pick_stamp()
        if body.get("pick_revision") != revision:
            raise ValueError("选点已变化，请刷新目标后重新规划")
        target = np.asarray(body["target_root"], dtype=float)
        if target.shape != (3,) or not np.isfinite(target).all() or not np.allclose(target, state.pick_target_root, atol=1e-6, rtol=0):
            raise ValueError("规划目标与最近一次 7005 取点不一致")
        kind = body.get("kind", "axis_last")
        if kind not in ("direct", "axis_last"):
            raise ValueError("未知规划方式")
        lift = float(body.get("lift_m", .02))
        if not np.isfinite(lift) or not 0 <= lift <= .2:
            raise ValueError("中段抬高必须在 0–20 cm 之间")
        r_cabinet = cabinet_rotation()
        r_target = r_cabinet @ rotation(reference["tcp"]["R_cabinet_from_frame"])
        p_tool = list(state.p_tool)
        via_joints = body.get("via_joints") or []
        if not isinstance(via_joints, list) or len(via_joints) > 20:
            raise ValueError("经由路点最多 20 个")
        args = (body["start_joints"], target, r_target, r_cabinet, kind, lift,
                bool(body.get("check_collision", False)), via_joints)
        ctx = mp.get_context("fork")
        rx, tx = ctx.Pipe(duplex=False)
        process = ctx.Process(target=_worker, args=(tx, args), daemon=True)
        process.start()
        tx.close()
        try:
            if not rx.poll(30.):
                raise ValueError("朝向规划超时，请缩短移动距离")
            ok, result = rx.recv()
        finally:
            rx.close()
            process.join(timeout=.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.)
        if not ok:
            raise ValueError(result)
        new_revision, new_anchor, _ = pick_stamp()
        if (revision, anchor) != (new_revision, new_anchor):
            raise ValueError("规划期间取点或世界系锚定已变化，请重新规划")
        if control_revision != state.cabinet_plan_revision:
            raise ValueError("规划期间已停止或释放手臂，请重新规划")
        if list(state.p_tool) != p_tool or reference_key(load_orientation_source(reference_id, reference_kind)) != reference_key(reference):
            raise ValueError("规划期间 TCP 或朝向记录已变化，请重新规划")
        q_list = [[w["named_joints"][n] for n in state.joint_names] for w in result["waypoints"]]
        plan = {"id": uuid.uuid4().hex, "reference_id": reference_id, "name": reference["name"],
                "reference_kind": reference_kind, "control_revision": control_revision,
                "scope": "goal", "target_root": target.tolist(),
                "pick_revision": revision, "anchor_count": anchor, "path_key": path_key(q_list),
                "p_tool": p_tool, "reference_key": reference_key(reference), "R_root_tcp": r_target.tolist(),
                "R_world_tcp": None if world_root is None else (world_root[:3, :3] @ r_target).tolist(),
                "world_T_root_ref": None if world_root is None else world_root.tolist(),
                "tolerance_deg": TOLERANCE_DEG}
        state.orientation_plan = plan
        return {**result, "orientation": {k: plan[k] for k in ("id", "reference_id", "reference_kind", "name", "scope", "tolerance_deg")}}
    except (OSError, ValueError, TypeError, KeyError, EOFError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 422)


def execution_constraint(plan_id, q_list, backend):
    plan = state.orientation_plan
    if not plan_id:
        if plan and path_key(q_list) == plan["path_key"]:
            raise ValueError("该轨迹带有朝向约束，请携带规划编号执行，或退出模式后重新规划")
        return None
    if not plan or plan.get("scope") != "goal" or plan_id != plan["id"] or path_key(q_list) != plan["path_key"]:
        raise ValueError("朝向规划已失效或轨迹被修改，请重新规划")
    if backend not in ("legacy", "legacy_timed", "pink"):
        raise ValueError("未知执行后端")
    if plan.get("reference_kind") == "cabinet_waypoint":
        from .cabinet_waypoints import validate_plan
        validate_plan(plan, backend)
        check_joint_path(q_list)
        limits = plan.get("arrival_tolerance") or {"position_mm": 2., "orientation_deg": TOLERANCE_DEG}
        check_goal_pose(joint_pose(q_list[-1]), plan["target_root"], plan["R_root_tcp"],
                        limits["position_mm"], limits["orientation_deg"])
        return deepcopy(plan)
    revision, anchor, world_root = pick_stamp(require_world=backend == "pink")
    if (revision != plan["pick_revision"]
            or not np.allclose(state.p_tool, plan["p_tool"], atol=1e-10)):
        raise ValueError("取点或 TCP 已变化，请重新规划朝向轨迹")
    if backend == "pink" and (plan["world_T_root_ref"] is None or anchor != plan["anchor_count"]
            or not np.allclose(world_root, plan["world_T_root_ref"], atol=1e-10)):
        raise ValueError("PINK 世界系已变化，请在锚定后重新取点并规划")
    if plan.get("control_revision", state.cabinet_plan_revision) != state.cabinet_plan_revision:
        raise ValueError("规划后已停止或释放手臂，请重新规划")
    if reference_key(load_orientation_source(plan["reference_id"], plan.get("reference_kind", "recorded_orientation"))) != plan["reference_key"]:
        raise ValueError("朝向记录已变化，请重新规划")
    check_joint_path(q_list)
    check_goal_pose(joint_pose(q_list[-1]), plan["target_root"], plan["R_root_tcp"])
    return deepcopy(plan)
