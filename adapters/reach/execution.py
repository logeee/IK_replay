"""手臂接管/释放、轨迹执行（含推力段）、诊断与急停。"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

import numpy as np
from fastapi.responses import JSONResponse

from .state import (_read_joints, _read_torso, _torso_drift, _torso_rotation,
                    router, state)


COMMAND_SNAPSHOT_MAX_AGE_S = 0.25


def _json_safe_value(value: Any) -> Any:
    """Convert controller diagnostics into values FastAPI can encode as JSON."""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


def _validated_command_snapshot(snapshot: dict, joint_count: int,
                                *, now: float | None = None) -> tuple[np.ndarray, dict]:
    """Validate a controller snapshot and return its last published joint command."""
    if not isinstance(snapshot, dict):
        raise RuntimeError("控制器没有返回有效的已发送命令快照")
    q = np.asarray(snapshot.get("q_rad"), dtype=float).reshape(-1)
    if q.size != joint_count or not np.all(np.isfinite(q)):
        raise RuntimeError(f"已发送命令快照维度/数值异常（需要 {joint_count} 维）")
    try:
        sequence = int(snapshot["sequence"])
        sent_at = float(snapshot["sent_at_monotonic"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("已发送命令快照缺少序号或时间戳") from exc
    if sequence <= 0 or not np.isfinite(sent_at):
        raise RuntimeError("控制器尚未成功发布关节命令")
    age = max(0.0, (time.monotonic() if now is None else float(now)) - sent_at)
    if age > COMMAND_SNAPSHOT_MAX_AGE_S:
        raise RuntimeError(f"最后关节命令已过期（{age * 1000.0:.0f} ms）")

    tau = np.asarray(snapshot.get("tau_ff_nm", []), dtype=float).reshape(-1)
    if tau.size not in (0, joint_count) or (tau.size and not np.all(np.isfinite(tau))):
        raise RuntimeError("已发送命令快照中的前馈力矩异常")
    return q.copy(), {
        "sequence": sequence,
        "age_ms": round(age * 1000.0, 3),
        "tau_ff_nm": tau.tolist() if tau.size else None,
    }


def _build_control_waypoints(planned: list[np.ndarray],
                             command_start: np.ndarray) -> list[np.ndarray]:
    """Keep the measured-space plan, but anchor control at the last sent command."""
    if not planned:
        raise RuntimeError("控制轨迹为空")
    start = np.asarray(command_start, dtype=float).reshape(-1)
    if start.size != planned[0].size or not np.all(np.isfinite(start)):
        raise RuntimeError("控制轨迹起点维度/数值异常")
    control = [np.asarray(q, dtype=float).copy() for q in planned]
    control[0] = start.copy()
    return control


@router.post("/hand_move")
def reach_hand_move(body: dict | None = None):
    """卸力开关（摆中间位用）。Body: {"on": bool}

    on=true: kp=0 低阻尼，人可拖动手臂（会下坠，务必扶住！）
    on=false: 在人放置的位置重新抓取并刚性保持。
    """
    if state.controller is None:
        return JSONResponse({"ok": False, "error": "手臂未接管"}, status_code=409)
    if state.exec_running:
        return JSONResponse({"ok": False, "error": "轨迹执行中不能卸力"}, status_code=409)
    on = bool((body or {}).get("on"))
    if on:
        if not state.controller.enter_hand_move():
            return JSONResponse({"ok": False, "error": "点动模式中不能卸力，请先停止"},
                                status_code=409)
        return {"ok": True, "hand_move": True, "message": "已卸力，请扶住手臂后再拖动"}
    state.controller.stop()  # 退出卸力：从人放置的位置抓取保持
    return {"ok": True, "hand_move": False, "message": "已恢复刚性保持（当前位置）"}


# --------------- 真机关节 / 执行 ---------------


@router.get("/joints")
def reach_joints():
    """当前真机该链关节角（named dict），作为 IK 起点。"""
    try:
        q = [float(v) for v in _read_joints()]
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return {"ok": True, "named_joints": dict(zip(state.joint_names, q))}


# --------------- 接管 / 释放手臂（由前端操作） ---------------


@router.post("/arm")
def reach_arm():
    """接管手臂：创建控制器、发布 rt/arm_sdk、在当前姿态刚性保持。

    真机会被立即接管！前端须先经人确认，且确保没有其他控制程序。
    """
    if state.arm_factory is None:
        return JSONResponse(
            {"ok": False, "error": "本次启动不支持真机执行（mock 模式或 DDS 不可用）"},
            status_code=409)
    with state.arm_lock:
        if state.controller is not None:
            return {"ok": True, "armed": True, "message": "已处于接管状态"}
        try:
            controller = state.arm_factory()
            controller.start()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"接管失败: {exc}"}, status_code=502)
        state.controller = controller
    return {"ok": True, "armed": True, "message": "已接管，手臂在当前姿态刚性保持"}


@router.post("/disarm")
def reach_disarm():
    """释放手臂：权重渐出、交还本体控制器。调用前请扶住手臂。"""
    with state.arm_lock:
        if state.controller is None:
            return {"ok": True, "armed": False, "message": "本来就未接管"}
        if state.exec_running:
            return JSONResponse(
                {"ok": False, "error": "轨迹执行中不能释放，请先急停"}, status_code=409)
        controller, state.controller = state.controller, None
    controller.shutdown()
    return {"ok": True, "armed": False, "message": "已释放，控制权交还本体控制器"}


def _exec_status() -> dict:
    return {
        "running": state.exec_running,
        "progress": state.exec_progress,
        "message": state.exec_message,
        "torso_diag": state.torso_diag,
    }


def _execution_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(record.get(key))
        for key in (
            "id",
            "ts",
            "session",
            "segment",
            "result",
            "robot",
            "chain_id",
            "gravity_profile",
            "pick_context",
            "joint_names",
            "trajectory_start_rad",
            "target_rad",
            "reach_error_max_deg",
            "follow_error_max_deg",
            "tcp",
        )
    }


@router.get("/diagnostics")
def reach_diagnostics():
    """现场排查用：重力前馈在出多大力、躯干相对取点时刻漂了多少。"""
    ctl = state.controller
    arm: dict[str, Any] = {"armed": ctl is not None}
    if ctl is not None:
        st = ctl.status()
        arm.update({k: st.get(k) for k in
                    ("kp", "kd", "kp_wrist", "kd_wrist", "grav_alpha", "payload_kg",
                     "grav_in_float", "use_imu_gravity", "tau_grav_nm", "tau_push_nm",
                     "joint_names", "cmd_rad", "measured_rad", "desired_rad",
                     "dq_rad_s", "measured_dq_rad_s", "tau_est_nm",
                     "measured_tau_nm", "tau_ff_nm")})
        if st.get("cmd_rad") and st.get("measured_rad"):
            cmd = np.asarray(st["cmd_rad"], dtype=float)
            measured = np.asarray(st["measured_rad"], dtype=float)
            gap = cmd - measured
            # 跟随误差就是"下垂"的直接度量：重力前馈生效后应当从几度掉到零点几度
            arm["follow_error_deg"] = [round(math.degrees(v), 2) for v in gap]
            arm["follow_error_max_deg"] = round(math.degrees(float(np.max(np.abs(gap)))), 2)
            kp = float(st.get("kp") or 0.0)
            kp_wrist = float(st.get("kp_wrist") or kp)
            names = list(st.get("joint_names") or state.joint_names)
            kp_vec = np.asarray([
                kp_wrist if "wrist" in name else kp for name in names
            ], dtype=float)
            if kp_vec.size == gap.size:
                arm["estimated_pd_support_nm"] = (kp_vec * gap).tolist()
            arm["tcp_cmd_root_m"] = _tcp_position(cmd.tolist())
            arm["tcp_measured_root_m"] = _tcp_position(measured.tolist())
        try:
            arm["command_snapshot"] = _json_safe_value(ctl.command_snapshot())
        except Exception as exc:
            arm["command_snapshot_error"] = str(exc)
    now = _read_torso()
    return {
        "captured_at": datetime.now().isoformat(timespec="milliseconds"),
        "captured_monotonic": time.monotonic(),
        "gravity_profile": state.gravity_profile,
        "arm": arm,
        "torso_now": now,
        "torso_at_pick": state.pick_torso,
        "torso_drift": _torso_drift(state.pick_torso, now),
        "last_exec_drift": state.torso_diag,
    }


@router.get("/exec_status")
def reach_exec_status():
    return _exec_status()


@router.get("/executions")
def reach_executions(limit: int = 12, pointcloud_only: bool = False):
    safe_limit = max(1, min(int(limit), 30))
    with state.execution_history_lock:
        records = [
            _execution_summary(value)
            for value in reversed(state.execution_history)
        ]
    if pointcloud_only:
        records = [
            value
            for value in records
            if (value.get("pick_context") or {}).get("selection_mode")
            == "frozen_rgbd_pointcloud"
        ]
    return {"ok": True, "executions": records[:safe_limit]}


@router.get("/executions/{execution_id}")
def reach_execution(execution_id: str):
    with state.execution_history_lock:
        record = next(
            (
                deepcopy(value)
                for value in state.execution_history
                if value.get("id") == execution_id
            ),
            None,
        )
    if record is None:
        return JSONResponse(
            {"ok": False, "error": "执行记录不存在或已被较新记录替换"},
            status_code=404,
        )
    return {"ok": True, "execution": record}


def _position_jacobian(named: dict[str, float]) -> np.ndarray:
    """TCP 位置对该链关节的数值雅可比（3×n，根系）。"""
    from core.types import Pose

    tcp_offset = Pose(xyz=list(state.p_tool))
    model = state.robot_model
    p0 = np.asarray(model.tcp_pose(named, state.chain_id, tcp_offset).xyz)
    J = np.zeros((3, len(state.joint_names)))
    eps = 1e-4
    for k, name in enumerate(state.joint_names):
        q = dict(named)
        q[name] = q.get(name, 0.0) + eps
        J[:, k] = (np.asarray(model.tcp_pose(q, state.chain_id, tcp_offset).xyz) - p0) / eps
    return J


@router.post("/execute")
def reach_execute(body: dict):
    """执行已规划的关节轨迹（真机运动！前端须先经人确认）。

    Body: {"waypoints": [named_joints, ...], "duration": float,
           "max_speed_rad_s": float?, "label": str?,
           "push": {"direction_root": [x,y,z], "force_n": float}?}

    label（可选）：段名，只用于 logs/reach 里区分主轨迹/横移/收回。

    max_speed_rad_s（可选）：本次执行的关节限速档（默认 0.2，收回段等
    低精度动作可以给 0.4 提速），不会超过 --arm-max-speed 天花板。

    push（可选）：执行期间在 TCP 上沿指定方向叠加前馈力（τ=JᵀF）。
    纯位置控制的侧向刚度很低（~300 N/m），贴着旋钮也使不上力；
    有了前馈力矩，接触后能持续出力把旋钮拨过去。
    """
    if state.controller is None:
        return JSONResponse(
            {"ok": False, "error": "手臂未接管，请先在页面上点「接管手臂」"},
            status_code=409)
    waypoints = body.get("waypoints") or []
    duration = float(body.get("duration") or 4.0)
    speed = float(np.clip(float(body.get("max_speed_rad_s") or 0.2), 0.05, 0.5))
    label = str(body.get("label") or "reach")[:32]
    if len(waypoints) < 2:
        return JSONResponse({"ok": False, "error": "轨迹至少要有 2 个路点"}, status_code=400)
    try:
        q_list = [np.asarray([float(wp[name]) for name in state.joint_names], dtype=float)
                  for wp in waypoints]
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"路点关节缺失/非法: {exc}"}, status_code=400)

    push_tau = None
    push = body.get("push")
    if push:
        try:
            direction = np.asarray(push["direction_root"], dtype=float).reshape(3)
            direction /= max(float(np.linalg.norm(direction)), 1e-9)
            force = min(abs(float(push["force_n"])), 40.0)
        except (KeyError, TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": f"push 参数非法: {exc}"}, status_code=400)
        if force > 1e-3:
            J = _position_jacobian(dict(zip(state.joint_names, q_list[0])))
            push_tau = J.T @ (direction * force)

    with state.exec_lock:
        if state.exec_running:
            return JSONResponse({"ok": False, "error": "已有轨迹在执行中"}, status_code=409)

        # 起点必须贴近真机当前姿态，防止规划时用的起点已经过期
        measured = state.controller.read_measured()
        start_gap = float(np.max(np.abs(q_list[0] - measured)))
        if start_gap > 0.15:
            return JSONResponse(
                {"ok": False,
                 "error": f"轨迹起点与真机当前姿态差 {start_gap:.3f} rad（>0.15），"
                          "手臂可能已被移动，请重新取点规划"},
                status_code=409)

        ctl_status = state.controller.status()
        if ctl_status.get("float"):
            return JSONResponse(
                {"ok": False, "error": "手臂仍在卸力拖动模式，请先恢复刚性保持"},
                status_code=409)
        try:
            command_start, snapshot_meta = _validated_command_snapshot(
                state.controller.command_snapshot(), len(state.joint_names))
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"无法取得连续控制起点: {exc}"}, status_code=409)

        support_gap = command_start - measured
        kp = float(ctl_status.get("kp") or 0.0)
        kp_wrist = float(ctl_status.get("kp_wrist") or kp)
        kp_vec = np.asarray([
            kp_wrist if "wrist" in name else kp for name in state.joint_names
        ])
        command_handoff = {
            "planned_start_rad": q_list[0].tolist(),
            "measured_start_rad": measured.tolist(),
            "last_sent_start_rad": command_start.tolist(),
            "support_gap_rad": support_gap.tolist(),
            "support_gap_max_rad": float(np.max(np.abs(support_gap))),
            "estimated_pd_support_nm": (kp_vec * support_gap).tolist(),
            "snapshot_sequence": snapshot_meta["sequence"],
            "snapshot_age_ms": snapshot_meta["age_ms"],
            "last_sent_tau_ff_nm": snapshot_meta["tau_ff_nm"],
        }
        execution_context = {
            "pick_context": deepcopy(state.pick_context),
            "pick_target_root": deepcopy(state.pick_target_root),
            "pick_target_torso": deepcopy(state.pick_target_torso),
            "pick_pixel": deepcopy(state.pick_pixel),
            "pick_torso": deepcopy(state.pick_torso),
        }

        state.exec_cancel.clear()
        state.exec_running = True
        state.exec_progress = 0.0
        state.exec_message = "执行中"
        state.exec_thread = threading.Thread(
            target=_exec_loop,
            args=(q_list, duration),
            kwargs={
                "push_tau": push_tau,
                "speed": speed,
                "label": label,
                "command_start_q": command_start,
                "command_handoff": command_handoff,
                "execution_context": execution_context,
            },
            daemon=True)
        state.exec_thread.start()
    return {"ok": True, **_exec_status()}


def _tcp_position(q) -> list[float] | None:
    """一组关节角对应的指尖（TCP）位置，根系，米。"""
    try:
        from core.types import Pose

        named = dict(zip(state.joint_names, [float(v) for v in q]))
        pose = state.robot_model.tcp_pose(named, state.chain_id, Pose(xyz=list(state.p_tool)))
        return [float(v) for v in pose.xyz]
    except Exception:
        return None


def _log_exec(kind: str, result: str, q_target, *, sag=None,
              duration=None, speed=None, pushing: bool = False, push_tau=None,
              trace=None, command_handoff=None,
              execution_context: dict | None = None) -> None:
    """每段真机动作落一行 JSONL：logs/reach/reach_YYYYMMDD.jsonl。

    调参靠的是横向对比（改了 α / payload / kp 之后到底好了多少），
    而页面上的实时数字每次重新取点就被冲掉了，留不下证据。这里把一次
    执行的"参数—误差—躯干姿态"三件套整段存下来，事后能直接拉出来比。
    误差拆成三段，各自对应完全不同的病因：
      ik_mm      规划本身的残差（IK 没收敛到点上）
      track_mm   指令关节角 vs 实测关节角（下垂/摩擦，重力前馈治的就是它）
      total_mm   取点目标 vs 实际指尖（前两者叠加，加上躯干漂移和标定误差）
    """
    try:
        ctl = state.controller
        st = ctl.status() if ctl is not None else {}
        target = np.asarray(q_target, dtype=float)
        measured = np.asarray(st.get("measured_rad") or [], dtype=float)
        cmd = np.asarray(st.get("cmd_rad") or [], dtype=float)
        deg = np.degrees
        completed_at = datetime.now()
        context = execution_context or {
            "pick_context": state.pick_context,
            "pick_target_root": state.pick_target_root,
            "pick_target_torso": state.pick_target_torso,
            "pick_pixel": state.pick_pixel,
            "pick_torso": state.pick_torso,
        }
        pick_target_root = context.get("pick_target_root")
        pick_target_torso = context.get("pick_target_torso")
        pick_torso = context.get("pick_torso")

        rec: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "ts": completed_at.isoformat(timespec="milliseconds"),
            "completed_monotonic": time.monotonic(),
            "session": state.session_id,
            "segment": kind,
            "result": result,
            "robot": state.robot_id,
            "chain_id": state.chain_id,
            "gravity_profile": deepcopy(state.gravity_profile),
            "pick_context": deepcopy(context.get("pick_context")),
            "params": {
                "duration_s": duration,
                "max_speed_rad_s": speed,
                "pushing": pushing,
                "grav_alpha": st.get("grav_alpha"),
                "payload_kg": st.get("payload_kg"),
                "kp": st.get("kp"), "kd": st.get("kd"),
                "kp_wrist": st.get("kp_wrist"), "kd_wrist": st.get("kd_wrist"),
                "use_imu_gravity": st.get("use_imu_gravity"),
            },
            "joint_names": state.joint_names,
            "trajectory_start_rad": (
                deepcopy(command_handoff.get("planned_start_rad"))
                if isinstance(command_handoff, dict)
                else None
            ),
            "target_rad": target.tolist(),
            "cmd_rad": cmd.tolist(),
            "measured_rad": measured.tolist(),
            "tau_grav_nm": st.get("tau_grav_nm"),
            # 记的是本段申请的峰值推力，不是当前值：日志在撤力之后才写，
            # 那时 status 里的推力已经归零了，记下来全是 0 没有意义
            "tau_push_peak_nm": (None if push_tau is None
                                 else [round(float(v), 3) for v in np.asarray(push_tau)]),
            "settle_residual_rad": sag,
            "command_handoff": command_handoff,
        }
        if measured.size == target.size and measured.size:
            rec["reach_error_deg"] = [round(v, 3) for v in deg(target - measured)]
            rec["reach_error_max_deg"] = round(float(np.max(np.abs(deg(target - measured)))), 3)
        if cmd.size == measured.size and cmd.size:
            # 跟随误差 = 指令 - 实测，纯 PD 下这就是重力压出来的下垂量
            rec["follow_error_deg"] = [round(v, 3) for v in deg(cmd - measured)]
            rec["follow_error_max_deg"] = round(float(np.max(np.abs(deg(cmd - measured)))), 3)

        tcp_target = _tcp_position(target)
        tcp_actual = _tcp_position(measured) if measured.size == target.size else None
        rec["tcp"] = {
            "pick_target_root": pick_target_root,
            "planned_root": tcp_target,
            "actual_root": tcp_actual,
        }

        def mm(a, b):
            if a is None or b is None:
                return None
            return round(float(np.linalg.norm(np.asarray(a) - np.asarray(b))) * 1000.0, 1)

        torso_end = _read_torso()
        rec["tcp"]["ik_mm"] = mm(pick_target_root, tcp_target)
        rec["tcp"]["track_mm"] = mm(tcp_target, tcp_actual)
        rec["tcp"]["total_mm"] = mm(pick_target_root, tcp_actual)
        # 验收指标：对"按结束时躯干姿态折算的真实开关位置"的误差。
        # 开了重瞄后 total_mm 会一直 ≈ 漂移量（手臂故意不去旧目标了），
        # 看这个才知道到底打没打中开关。
        try:
            R0 = _torso_rotation(pick_torso) if pick_torso else None
            R1 = _torso_rotation(torso_end) if torso_end else None
            if (R0 is not None and R1 is not None
                    and pick_target_torso and pick_target_root):
                p0 = np.asarray(pick_target_torso, dtype=float)
                drifted = (np.asarray(pick_target_root, dtype=float)
                           + ((R1.T @ R0) @ p0 - p0))
                rec["tcp"]["drifted_target_root"] = [round(float(v), 4) for v in drifted]
                rec["tcp"]["total_vs_drifted_mm"] = mm(drifted.tolist(), tcp_actual)
        except Exception:
            pass

        rec["pick"] = {
            "pixel": context.get("pick_pixel"),
            "target_torso": pick_target_torso,
        }
        rec["torso_at_pick"] = pick_torso
        rec["torso_at_end"] = torso_end
        rec["torso_drift"] = state.torso_diag
        # 快照拷贝：采样线程可能还在往里 append
        rec["torso_trace"] = [dict(s) for s in list(trace)] if trace else None

        rec = _json_safe_value(rec)
        with state.execution_history_lock:
            state.execution_history.append(rec)
        if state.log_dir is not None:
            state.log_dir.mkdir(parents=True, exist_ok=True)
            path = state.log_dir / f"reach_{completed_at:%Y%m%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass    # 记日志永远不能把执行搞挂


def _finish_torso_diag() -> str:
    """执行结束时结算躯干漂移，写进 state 供页面展示，并返回一句话摘要。"""
    diag = _torso_drift(state.pick_torso, _read_torso())
    state.torso_diag = diag
    if not diag:
        return ""
    shift = diag.get("target_shift_mm")
    if shift is None:
        return ""
    note = f"；躯干较取点时转了 {diag.get('torso_rotation_deg', 0):.1f}°，目标漂移 {shift:.0f} mm"
    return note + ("（超过 10mm，够不着多半是躯干在动而不是手臂没到位）"
                   if shift > 10 else "")


def _start_torso_trace(ctl) -> tuple[list, threading.Event]:
    """执行期间 5Hz 采样躯干姿态 + 手臂跟随误差，带阶段标签。

    用来钉死"身体后仰到底发生在哪个阶段"：是轨迹回放时（手臂大幅抡出去，
    平衡控制器在配平）还是收尾阶段（手臂只挪一两度）。每段动作的两个
    快照分不清这件事，只有时间序列能。
    """
    samples: list[dict] = []
    stop = threading.Event()

    def run() -> None:
        t0 = time.monotonic()
        while not stop.is_set() and len(samples) < 600:
            row: dict[str, Any] = {"t": round(time.monotonic() - t0, 2),
                                   "phase": state.exec_phase}
            torso = _read_torso()
            if torso:
                row["waist_deg"] = [round(math.degrees(v), 3) for v in torso["waist_rad"]]
                row["imu_rpy_deg"] = [round(math.degrees(v), 3) for v in torso["imu_rpy"]]
            try:
                st = ctl.status()
                gap = np.asarray(st["cmd_rad"]) - np.asarray(st["measured_rad"])
                row["follow_sp_deg"] = round(math.degrees(float(gap[0])), 3)   # 肩俯仰
                row["follow_max_deg"] = round(math.degrees(float(np.max(np.abs(gap)))), 3)
            except Exception:
                pass
            samples.append(row)
            stop.wait(0.2)

    threading.Thread(target=run, name="reach-torso-trace", daemon=True).start()
    return samples, stop


def _exec_loop(q_list: list[np.ndarray], duration: float,
               push_tau: np.ndarray | None = None, speed: float = 0.2,
               label: str = "reach", command_start_q: np.ndarray | None = None,
               command_handoff: dict | None = None,
               execution_context: dict | None = None) -> None:
    ctl = state.controller
    trace, trace_stop = _start_torso_trace(ctl)
    log = dict(duration=duration, speed=speed, pushing=push_tau is not None,
               push_tau=push_tau, trace=trace, command_handoff=command_handoff,
               execution_context=execution_context)
    try:
        if command_start_q is None:
            raise RuntimeError("缺少上一帧已发送关节命令，拒绝启动轨迹")
        control_q_list = _build_control_waypoints(q_list, command_start_q)
        state.exec_phase = "traj"
        ctl.enable_jog()
        # 分段限速：普通段默认 0.2 慢而稳；带推力的快拨段放行到 0.4；
        # 调用方也可以按段指定（如收回段 0.4），都不超 --arm-max-speed 天花板
        if hasattr(ctl, "set_max_speed"):
            ctl.set_max_speed(max(0.4, speed) if push_tau is not None else speed)
        n = len(control_q_list)
        # 时长下限：限速滑动（矢量同步）跑完全程所需时间。短于它路点节拍会
        # 一直超前于指令，falling-behind 的关节仍会扭曲路径，所以自动拉长。
        travel = sum(float(np.max(np.abs(b - a)))
                     for a, b in zip(control_q_list, control_q_list[1:]))
        min_duration = travel / max(ctl.max_speed, 1e-6) * 1.1
        if duration < min_duration:
            duration = min_duration
            state.exec_message = f"时长过短，按限速拉长到 {duration:.1f}s"
        dt = duration / max(n - 1, 1)
        t0 = time.monotonic()
        for i, q in enumerate(control_q_list):
            if state.exec_cancel.is_set():
                ctl.disable_jog()
                state.exec_message = "已中止（保持当前位置）"
                _log_exec(label, "cancelled", q_list[-1], **log)
                return
            ctl.set_target(q)
            if push_tau is not None:
                # 前馈力矩渐入（前 30% 路点线性加满），避免突加力矩的抖动
                scale = min(1.0, (i + 1) / max(1, round(0.3 * n)))
                ctl.set_tau_ff(push_tau * scale)
            state.exec_progress = i / (n - 1)
            target_t = t0 + (i + 1) * dt
            while True:
                remaining = target_t - time.monotonic()
                if remaining <= 0 or state.exec_cancel.is_set():
                    break
                time.sleep(min(remaining, 0.05))
        # 最终目标已下发；等限速滑动真正到位再冻结（时长偏短时控制器会滞后）
        state.exec_message = "收敛中"
        state.exec_phase = "converge"
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not state.exec_cancel.is_set():
            status = ctl.status()
            gap = float(np.max(np.abs(
                np.asarray(status["desired_rad"]) - np.asarray(status["cmd_rad"]))))
            if gap < 1e-3:
                break
            time.sleep(0.1)

        # 推力模式：位置到不到位没有意义（被旋钮/表面顶着），收敛后持续
        # 顶 1.5s 把旋钮拨到底，然后撤力刚性保持。
        if push_tau is not None:
            if not state.exec_cancel.is_set():
                state.exec_message = "持续出力中"
                state.exec_phase = "push_hold"
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline and not state.exec_cancel.is_set():
                    time.sleep(0.05)
            # 撤力渐出：顶着的手臂/身体像压紧的弹簧，力矩瞬间清零会"啪"地
            # 向反方向回弹。这里力矩 0.65s 线性泄掉，同时把位置指令收回到
            # 实测位（限速滑动本身是平滑的），存的形变缓慢释放。
            state.exec_message = "撤力中"
            state.exec_phase = "release"
            try:
                ctl.set_target(ctl.read_measured())
            except Exception:
                pass
            for s in np.linspace(1.0, 0.0, 13):
                if state.exec_cancel.is_set():
                    break
                ctl.set_tau_ff(push_tau * float(s))
                time.sleep(0.05)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not state.exec_cancel.is_set():
                status = ctl.status()
                if float(np.max(np.abs(np.asarray(status["desired_rad"])
                                       - np.asarray(status["cmd_rad"])))) < 1e-3:
                    break
                time.sleep(0.05)
            ctl.disable_jog()   # 兜底清零主动出力
            state.exec_progress = 1.0
            cancelled = state.exec_cancel.is_set()
            state.exec_message = ("已中止（保持当前位置）" if cancelled
                                  else f"完成（推力段结束，已撤力保持{_finish_torso_diag()}）")
            _log_exec(label, "cancelled" if cancelled else "done", q_list[-1], **log)
            return

        sag = None
        target = q_list[-1]
        if not state.exec_cancel.is_set():
            state.exec_phase = "settle"
            # 躯干漂移不再在这里主动补偿（曾有"重瞄"逻辑：到位后按躯干姿态
            # 变化重解 IK 再挪 2~3cm，观感是到位后突然跳一下）。现在漂移交给
            # 分段模式的「再次选点」用当前相机实测修正，这里只测量、写日志。
            # 外环积分同样已移除：重力下垂由重力前馈扛。
            # 这里只等指令送达、给电机 ~0.3s 贴上来，然后测一次落点残差写日志。
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not state.exec_cancel.is_set():
                status = ctl.status()
                delivered = float(np.max(np.abs(
                    np.asarray(status["desired_rad"]) - np.asarray(status["cmd_rad"])))) < 1e-3
                if delivered:
                    break
                time.sleep(0.08)
            if not state.exec_cancel.is_set():
                time.sleep(0.3)
                status = ctl.status()
                measured = np.asarray(status["measured_rad"] or ctl.read_measured().tolist())
                sag = float(np.max(np.abs(target - measured)))

        ctl.disable_jog()
        state.exec_progress = 1.0
        sag_note = f"，落点残差 {sag:.3f} rad" if sag is not None else ""
        cancelled = state.exec_cancel.is_set()
        state.exec_message = ("已中止（保持当前位置）" if cancelled
                              else f"完成（刚性保持{sag_note}{_finish_torso_diag()}）")
        _log_exec(label, "cancelled" if cancelled else "done", target,
                  sag=sag, **log)
    except Exception as exc:
        try:
            ctl.stop()
        except Exception:
            pass
        state.exec_message = f"执行出错已停止: {exc}"
        _log_exec(label, f"error: {exc}", q_list[-1], **log)
    finally:
        trace_stop.set()
        state.exec_phase = "idle"
        state.exec_running = False


@router.post("/stop")
def reach_stop():
    """急停：中止执行线程并冻结在当前指令位。"""
    if state.controller is None:
        return JSONResponse({"ok": False, "error": "手臂未接管"}, status_code=409)
    state.exec_cancel.set()
    state.controller.stop()
    state.exec_message = "已急停（刚性保持）"
    return {"ok": True, **_exec_status()}
