"""路点 / 动作序列 / 横移的录制、落盘与回放执行。

左右臂归属（core/arm_assets.py，安全）：本服务只在一条臂上运行
（state.chain_id）。录制落盘一律写 ``arm`` 字段并给名字加 ``R-``/``L-``
前缀；列表接口只回本臂的文件（无归属标记 / 异臂的文件一律不显示，
``?scope=all`` 也只放开认领过滤、不放开臂过滤）；回放执行前再校验一次，
不一致 → 409。左臂绝不会拿到右臂的轨迹。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi.responses import JSONResponse

from core import arm_assets

from .execution import (
    _exec_loop,
    _exec_status,
    _resolve_exec_backend,
    _tcp_position,
    _validated_command_snapshot,
)
from .state import _read_joints, router, state


# --------------- 左右臂归属 ---------------


def _own_arm() -> str:
    """本服务运行的臂（--chain），所有录制 / 回放都以它为准。"""
    return state.chain_id


def _load_dir_json(directory: Path | None, *, by_mtime: bool = True) -> list[dict]:
    """读目录下全部 *.json（附 file 字段），坏文件跳过。"""
    if directory is None or not directory.is_dir():
        return []
    paths = directory.glob("*.json")
    if by_mtime:
        paths = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    else:
        paths = sorted(paths)
    items: list[dict] = []
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        data["file"] = path.name
        items.append(data)
    return items


def _split_by_arm(items: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """按本臂过滤；顺带统计被挡掉的异臂 / 无标记文件数（诊断字段）。"""
    own = _own_arm()
    kept: list[dict] = []
    stats = {"foreign_arm": 0, "unmarked": 0}
    for item in items:
        declared = arm_assets.asset_arm(item)
        if declared == own:
            kept.append(item)
        elif declared is None:
            stats["unmarked"] += 1
        else:
            stats["foreign_arm"] += 1
    return kept, stats


def _arm_reject(exc: Exception) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc), "arm": _own_arm()},
                        status_code=409)


# --------------- 中间路点（录制 / 落盘 / 复用） ---------------


def _visible_by_claims(data: dict, allowed: set | None) -> bool:
    """认领可见性（18000 配置）：认领集合内可见；本组合自己录的也可见
    （来源戳匹配——新录的还没进启动快照，不能刚存完就"消失"）。"""
    if allowed is None:
        return True
    if str(data.get("name") or "") in allowed:
        return True
    combo = data.get("recorded_combo")
    active = state.active_combo
    return (isinstance(combo, dict) and bool(active)
            and combo.get("arm") == active.get("arm")
            and combo.get("hand_id") == active.get("hand_id"))


def _recorded_combo_stamp() -> dict | None:
    """当前激活组合的来源戳（camera-only 等无组合场景为 None）。"""
    if not state.active_combo:
        return None
    return {
        "arm": state.active_combo.get("arm"),
        "hand_id": state.active_combo.get("hand_id"),
    }


def _safe_waypoint_file(filename: str) -> Path | None:
    """防路径穿越：只允许目录内的 *.json 纯文件名。"""
    if not filename.endswith(".json") or "/" in filename or "\\" in filename or ".." in filename:
        return None
    return state.waypoints_dir / filename


def _load_waypoints() -> list[dict]:
    """本臂的全部位点（异臂 / 无归属标记的已剔除）。"""
    return _split_by_arm(_load_dir_json(state.waypoints_dir))[0]


def _load_waypoint_for_arm(filename: str, label: str = "位点") -> dict:
    """读并校验一个位点文件必须属于本臂；否则抛 ArmMismatch / OSError。"""
    path = _safe_waypoint_file(filename)
    if path is None or not path.is_file():
        raise FileNotFoundError(f"{label}文件不存在: {filename}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{label}文件不是 JSON object: {filename}")
    arm_assets.check_asset_arm(data, _own_arm(), label)
    data["file"] = path.name
    return data


@router.get("/waypoints")
def reach_waypoints(scope: str = ""):
    """位点列表（只含本臂）。默认再按认领可见性过滤（激活组合已启用能力的
    生效位点 + 本组合自己录的）；?scope=all 放开认领过滤看本臂全池。
    异臂 / 无归属标记的文件任何 scope 都不显示（arm_hidden 给出被挡数量）。"""
    own_items, arm_stats = _split_by_arm(_load_dir_json(state.waypoints_dir))
    if scope == "all":
        visible = own_items
    else:
        visible = [w for w in own_items
                   if _visible_by_claims(w, state.visible_waypoints)]
    return {"waypoints": visible, "total": len(own_items),
            "hidden": len(own_items) - len(visible),
            "filtered": len(visible) != len(own_items) or (
                scope != "all" and state.visible_waypoints is not None),
            "arm": _own_arm(), "arm_hidden": arm_stats,
            "combo": state.active_combo}


@router.post("/waypoints")
def reach_record_waypoint(body: dict):
    """把真机当前关节角录制为命名路点，每个路点单独落盘为
    data/waypoints/<名字>_<时间戳>.json。Body: {"name": str}

    典型流程：接管手臂 → 卸力 → 人手摆到中间位 → 恢复保持 → 录制。
    """
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "路点需要名字"}, status_code=400)
    if "/" in name or "\\" in name or ".." in name:
        return JSONResponse({"ok": False, "error": "名字不能包含路径分隔符"}, status_code=400)
    try:
        # 臂归属：名字加 R-/L- 前缀（用户已写同臂前缀则保持；写了异臂前缀拒绝）
        name = arm_assets.prefixed_name(_own_arm(), name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    try:
        q = [float(v) for v in _read_joints()]
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"读不到真机关节: {exc}"}, status_code=503)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    item = {
        "name": name,
        "arm": _own_arm(),
        "chain_id": state.chain_id,
        "named_joints": dict(zip(state.joint_names, q)),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    combo = _recorded_combo_stamp()
    if combo:
        # 来源戳：认领可见性下，自己录的位点即刻可见（无需先认领）
        item["recorded_combo"] = combo
    state.waypoints_dir.mkdir(parents=True, exist_ok=True)
    path = state.waypoints_dir / f"{name}_{stamp}.json"
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2))
    item["file"] = path.name
    return {"ok": True, "waypoint": item}


@router.delete("/waypoints/{filename}")
def reach_delete_waypoint(filename: str):
    path = _safe_waypoint_file(filename)
    if path is None:
        return JSONResponse({"ok": False, "error": "非法文件名"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": f"没有文件 {filename!r}"}, status_code=404)
    try:
        # 只能删本臂的文件（另一条臂的资产对本服务不可见，也不可删）
        _load_waypoint_for_arm(filename)
    except arm_assets.ArmMismatch as exc:
        return _arm_reject(exc)
    except (OSError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    path.unlink()
    return {"ok": True}


# --------------- 动作序列（多个路点按序回放，一键调用，纯关节回放无 IK） ---------------


def _safe_sequence_file(filename: str) -> Path | None:
    if not filename.endswith(".json") or "/" in filename or "\\" in filename or ".." in filename:
        return None
    return state.sequences_dir / filename


@router.get("/sequences")
def reach_sequences(scope: str = ""):
    """动作序列列表（只含本臂）。默认再按认领可见性过滤（激活组合已启用
    能力认领的动作 + 本组合自己录的）；?scope=all 放开认领过滤看本臂全池。
    异臂 / 无归属标记的文件任何 scope 都不显示。"""
    own_items, arm_stats = _split_by_arm(_load_dir_json(state.sequences_dir))
    if scope == "all":
        visible = own_items
    else:
        visible = [s for s in own_items
                   if _visible_by_claims(s, state.visible_sequences)]
    return {"sequences": visible, "total": len(own_items),
            "hidden": len(own_items) - len(visible),
            "filtered": len(visible) != len(own_items) or (
                scope != "all" and state.visible_sequences is not None),
            "arm": _own_arm(), "arm_hidden": arm_stats,
            "combo": state.active_combo}


@router.post("/sequences")
def reach_save_sequence(body: dict):
    """把一组路点按顺序存为动作序列。Body: {"name": str, "waypoints": [路点文件名...]}

    序列只存路点文件名的引用；执行由前端逐段触发（关节空间插值回放，无 IK）。
    """
    name = str(body.get("name") or "").strip()
    files = body.get("waypoints") or []
    if not name:
        return JSONResponse({"ok": False, "error": "序列需要名字"}, status_code=400)
    if "/" in name or "\\" in name or ".." in name:
        return JSONResponse({"ok": False, "error": "名字不能包含路径分隔符"}, status_code=400)
    if not files:
        return JSONResponse({"ok": False, "error": "序列至少要有 1 个路点"}, status_code=400)
    try:
        name = arm_assets.prefixed_name(_own_arm(), name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    for f in files:
        try:
            # 序列引用的每个路点都必须是本臂的
            _load_waypoint_for_arm(str(f), "路点")
        except arm_assets.ArmMismatch as exc:
            return _arm_reject(exc)
        except Exception:
            return JSONResponse({"ok": False, "error": f"路点文件不存在: {f}"}, status_code=400)
    item = {
        "name": name,
        "arm": _own_arm(),
        "chain_id": state.chain_id,
        "waypoints": [str(f) for f in files],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    combo = _recorded_combo_stamp()
    if combo:
        # 来源戳：这条动作是哪个「臂+手型号」组合在位时录的（进公共池后
        # 18000 页面按它显示出处；认领可见性下自己录的即刻可见）
        item["recorded_combo"] = combo
    state.sequences_dir.mkdir(parents=True, exist_ok=True)
    path = state.sequences_dir / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2))
    item["file"] = path.name
    # 新动作进公共池后上报 18000 自动认领：按动作名命中录制组合下哪些
    # 能力条目的起手式正则就归谁（拨/扭互不污染）。失败不影响保存，
    # 可到 18000 配置页手动认领。
    claim: dict | None = None
    if state.active_combo:
        from core.capability_client import CapabilityUnavailable, claim_sequence

        arm = str(state.active_combo.get("arm") or "")
        hand_id = str(state.active_combo.get("hand_id") or "")
        try:
            result = claim_sequence(
                state.capability_url or None, arm, hand_id, name)
            targets = result.get("claimed_to") or []
            claim = {"ok": True, "claimed_to": targets}
            if targets:
                labels = "、".join(
                    str(t.get("label") or t.get("capability_id") or "?")
                    for t in targets)
                print(f"[reach] 序列「{name}」已入公共池，自动认领给：{labels}")
            else:
                print(f"[reach] 序列「{name}」已入公共池，但没命中 "
                      f"{arm}+{hand_id} 任何条目的起手式正则——"
                      "留池，可到 18000 页面手动认领")
        except CapabilityUnavailable as exc:
            claim = {"ok": False, "error": str(exc)}
            print(f"[reach] ⚠ 序列「{name}」自动认领失败：{exc}")
    return {"ok": True, "sequence": item, "claim": claim}


SEQ_REPLAY_DRIFT_RAD = 0.5   # 起点漂移超过它才重新规划（正常复用不会触发）


def _sequence_plan_worker(conn, q0_list, target_lists, target_names, margin):
    """fork 子进程里跑序列 RRT 规划（独享 GIL，不被控制环/相机线程拖慢）。

    fork 继承父进程内存：robot_model、collision_checker（含墙平面/豁免球）
    直接可用；对 checker 的改动只影响子进程副本。结果经管道回传：
    ("ok", frames) 或 ("err", 消息)。子进程不碰 DDS/相机。
    """
    try:
        from core.types import Pose
        from planners.rrt import densify, rrt_connect_path

        tcp_offset = Pose(xyz=list(state.p_tool))
        checker = state.collision_checker
        q0 = np.asarray(q0_list, dtype=float)
        targets = [np.asarray(t, dtype=float) for t in target_lists]

        if checker is not None:
            # 路点是人工验证过的，可能本来就贴着柜面——在其 TCP 周围开豁免球
            spheres = list(checker.environment_exclusions)
            for tgt in targets:
                tcp = _tcp_position(tgt.tolist())
                if tcp:
                    spheres.append((tcp, state.target_exclusion_m))
            checker.set_environment_exclusions(spheres)
            if checker.enabled:
                # 预检仅作日志：端点碰撞按误报豁免（见 rrt_connect_path）
                for name, tgt in [("当前姿态", q0)] + list(zip(target_names, targets)):
                    chk = checker.check_state(tgt.tolist(), state.chain_id, tcp_offset)
                    if chk["status"] != "collision":
                        continue
                    pair = chk.get("pair") or {}
                    depth = abs(float(chk.get("min_distance_mm") or 0.0))
                    print(f"[reach] 路点「{name}」模型碰撞已豁免: "
                          f"{pair.get('a', '?')} ↔ {pair.get('b', '?')}"
                          f"（嵌入 {depth:.0f}mm，实际到过的姿态视为误报）")

        full: list[np.ndarray] = [q0]
        for i, (name, tgt) in enumerate(zip(target_names, targets), 1):
            try:
                leg = rrt_connect_path(
                    state.robot_model, checker, state.chain_id,
                    full[-1], tgt, tcp_offset,
                    {"margin_m": margin, "timeout_s": 20.0})
            except ValueError as exc:
                conn.send(("err", f"第{i}段（→「{name}」）{exc}"))
                return
            full.extend(leg[1:])
        q_list = densify(full, frame_rad=0.04)
        conn.send(("ok", [[float(v) for v in q] for q in q_list]))
    except Exception as exc:  # noqa: BLE001 —— 子进程任何崩溃都回传给父进程
        conn.send(("err", f"规划子进程异常: {exc}"))
    finally:
        conn.close()


@router.post("/sequences/run")
def reach_run_sequence(body: dict):
    """一键执行已保存的动作序列。供无界面的自动化封装直接调用。

    执行方式（v2）：第一次运行用「直线优先、撞了才 RRT-Connect」逐段规划出
    无碰撞轨迹，并把完整轨迹帧录进序列文件（trajectory 字段）；之后运行直接
    回放录制轨迹——不算 RRT、不算 IK、不做碰撞检查，请求即执行。
    起点与录制起点漂移超过 0.5 rad → 拒绝执行（409），绝不隐式重规划：
    通用规划器不知道录制时人工绕开的障碍，覆盖已验证轨迹更是事故源。
    只有「文件里还没有轨迹」或显式 replan=true 才会规划并写入文件。

    Body: {"file": str, "joint_speed": float=0.35, "max_speed_rad_s": float=0.4,
           "replan": bool=false,    # replan=true 强制丢弃录制轨迹重规划
           "margin_m": float=0.01}  # 墙面退让：正=墙逼近(更保守)，负=墙后退
    首次规划（或 replan）不直接执行：把轨迹录进文件并回传 preview 帧给前端
    仿真回放，用户确认后再次调用走"录播"路径才真机执行。
    进度用 GET /exec_status 轮询，POST /stop 急停。
    """
    if state.controller is None:
        return JSONResponse({"ok": False, "error": "手臂未接管"}, status_code=409)
    path = _safe_sequence_file(str(body.get("file") or ""))
    if path is None or not path.is_file():
        return JSONResponse({"ok": False, "error": "序列文件不存在"}, status_code=404)
    try:
        seq = json.loads(path.read_text())
        # 臂归属（安全）：序列本身与它引用的每个路点都必须属于本臂，
        # 任何一个不是 → 拒绝执行，不做任何兜底
        arm_assets.check_asset_arm(seq, _own_arm(), "序列")
        targets = []
        target_names = []
        for fname in seq.get("waypoints") or []:
            wp = _load_waypoint_for_arm(str(fname), "路点")
            targets.append(np.asarray([float(wp["named_joints"][n])
                                       for n in state.joint_names], dtype=float))
            target_names.append(str(wp.get("name") or fname))
        if not targets:
            raise ValueError("序列不含路点")
    except arm_assets.ArmMismatch as exc:
        print(f"[reach] !!! 拒绝执行序列 {path.name}: {exc}")
        return _arm_reject(exc)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"序列/路点读取失败: {exc}"}, status_code=400)

    joint_speed = float(np.clip(float(body.get("joint_speed") or 0.35), 0.05, 0.5))
    speed = float(np.clip(float(body.get("max_speed_rad_s") or 0.4), 0.05, 0.5))
    margin = float(np.clip(float(body.get("margin_m", 0.01)), -0.05, 0.05))

    with state.exec_lock:
        if state.exec_running:
            return JSONResponse({"ok": False, "error": "已有轨迹在执行中"}, status_code=409)
        try:
            q0 = np.asarray(state.controller.read_measured(), dtype=float)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"读不到真机关节: {exc}"}, status_code=503)

        # ---- 优先回放录制轨迹（免 RRT/IK/碰撞检查，零计算耗时） ----
        planned = False
        q_list: list[np.ndarray] | None = None
        rec = seq.get("trajectory")
        if rec and not bool(body.get("replan")):
            frames = [np.asarray(f, dtype=float) for f in rec.get("frames") or []]
            if frames and frames[0].shape == q0.shape:
                drift = float(np.max(np.abs(frames[0] - q0)))
                if drift <= SEQ_REPLAY_DRIFT_RAD:
                    q_list = [q0] + frames   # 从当前实测平滑接入第一帧
                else:
                    # 起点漂移绝不隐式重规划：通用规划器不知道录制时人工
                    # 绕开的障碍，而且重规划会覆盖文件里已验证的轨迹。
                    return JSONResponse(
                        {"ok": False,
                         "error": f"起点与录制起点相差 {drift:.2f} rad"
                                  f"（>{SEQ_REPLAY_DRIFT_RAD}），已拒绝执行。"
                                  f"请先把手臂移回该序列的录制起点再运行；"
                                  f"确要丢弃已录轨迹重新规划请显式传 replan=true"},
                        status_code=409)

        if q_list is None:
            # ---- 首次（或工况变了）：fork 子进程跑 RRT。规划是纯 Python
            # 计算，在服务进程里会和 50Hz 控制环/相机线程抢 GIL（离线 2s 的
            # 问题在线 6s 都解不完）；子进程独享 GIL，恢复 2s 级 ----
            ctx = mp.get_context("fork")
            rx, tx = ctx.Pipe(duplex=False)
            proc = ctx.Process(target=_sequence_plan_worker, daemon=True,
                               args=(tx, q0.tolist(),
                                     [t.tolist() for t in targets],
                                     target_names, margin))
            proc.start()
            tx.close()
            if rx.poll(60.0):
                plan_status, payload = rx.recv()
            else:
                plan_status, payload = "err", "规划子进程 60s 无响应"
            proc.join(timeout=1.0)
            if proc.is_alive():
                proc.kill()
            if plan_status != "ok":
                print(f"[reach] 序列规划失败: {payload}")
                return JSONResponse({"ok": False, "error": f"规划失败: {payload}"},
                                    status_code=422)
            q_list = [np.asarray(f, dtype=float) for f in payload]
            planned = True
            try:
                seq["trajectory"] = {
                    "frames": [[round(float(v), 5) for v in q] for q in q_list],
                    "joint_names": list(state.joint_names),
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                    "planner": "line-else-rrt",
                }
                path.write_text(json.dumps(seq, ensure_ascii=False, indent=1))
            except Exception as exc:
                print(f"[reach] 序列轨迹录制失败: {exc}")

        total_travel = sum(float(np.max(np.abs(b - a)))
                           for a, b in zip(q_list, q_list[1:]))
        duration = max(1.0, total_travel / joint_speed)

        if planned:
            # 刚规划出来的轨迹不直接执行：回传抽稀后的帧给前端仿真回放，
            # 用户看过确认后再按一次 ▶ ——那时轨迹已录进文件，走录播路径执行
            from core.types import Pose
            tcp_offset = Pose(xyz=list(state.p_tool))
            stride = max(1, len(q_list) // 150)
            picks = list(range(0, len(q_list), stride))
            if picks[-1] != len(q_list) - 1:
                picks.append(len(q_list) - 1)
            frames_preview = []
            for k in picks:
                joints = [float(v) for v in q_list[k]]
                frames_preview.append({
                    "named_joints": state.robot_model.named_chain_joints(
                        joints, state.chain_id),
                    "tcp_pose": state.robot_model.tcp_pose(
                        joints, state.chain_id, tcp_offset),
                    "link_poses": state.robot_model.link_poses(
                        joints, state.chain_id),
                })
            return {"ok": True, "preview": True, "planned": True,
                    "replayed": False, "frames": len(q_list),
                    "duration_s": round(duration, 2),
                    "preview_frames": frames_preview}
        label = f"序列:{str(seq.get('name') or path.stem)}"[:32]
        # 运动后端与 /execute 同一套规则：body.motion_backend 缺省用 18000 默认；
        # 选 pink 需运行时可用且已锚定，否则 409（不进执行线程）
        exec_backend, backend_error = _resolve_exec_backend(
            body.get("motion_backend"), label=label, allow_pink=False,
            allow_timed=False)
        if backend_error:
            return JSONResponse({"ok": False, "error": backend_error}, status_code=409)
        ctl_status = state.controller.status()
        if ctl_status.get("float"):
            return JSONResponse(
                {"ok": False, "error": "手臂仍在卸力拖动模式，请先恢复刚性保持"},
                status_code=409,
            )
        try:
            command_start, snapshot_meta = _validated_command_snapshot(
                state.controller.command_snapshot(), len(state.joint_names)
            )
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"无法取得连续控制起点: {exc}"},
                status_code=409,
            )
        command_handoff = {
            "planned_start_rad": q_list[0].tolist(),
            "measured_start_rad": q0.tolist(),
            "last_sent_start_rad": command_start.tolist(),
            "support_gap_rad": (command_start - q0).tolist(),
            "support_gap_max_rad": float(
                np.max(np.abs(command_start - q0))
            ),
            "snapshot_sequence": snapshot_meta["sequence"],
            "snapshot_age_ms": snapshot_meta["age_ms"],
            "last_sent_tau_ff_nm": snapshot_meta["tau_ff_nm"],
            "source": "sequence_replay",
        }

        state.exec_cancel.clear()
        state.exec_running = True
        state.exec_progress = 0.0
        state.exec_message = "执行中"
        state.exec_backend = exec_backend
        state.exec_thread = threading.Thread(
            target=_exec_loop,
            args=(q_list, duration),
            kwargs={
                "motion_backend": exec_backend,
                "push_tau": None,
                "speed": speed,
                "label": label,
                "command_start_q": command_start,
                "command_handoff": command_handoff,
            },
            daemon=True,
        )
        state.exec_thread.start()
    return {"ok": True, "duration_s": round(duration, 2), "frames": len(q_list),
            "planned": planned, "replayed": not planned, **_exec_status()}


@router.delete("/sequences/{filename}")
def reach_delete_sequence(filename: str):
    path = _safe_sequence_file(filename)
    if path is None:
        return JSONResponse({"ok": False, "error": "非法文件名"}, status_code=400)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": f"没有文件 {filename!r}"}, status_code=404)
    try:
        seq = json.loads(path.read_text())
        arm_assets.check_asset_arm(seq, _own_arm(), "序列")
    except arm_assets.ArmMismatch as exc:
        return _arm_reject(exc)
    except (OSError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"序列读取失败: {exc}"}, status_code=400)
    path.unlink()
    return {"ok": True}


# --------------- 横移录制（免 IK 回放） ---------------
# 逐点 IK 一次 6cm 要 ~6s（纯 Python FK + 数值雅可比）。机器人正视电柜时
# 横移方向在根系里是常量、起点姿态也基本重复，所以把算好的整段路点存下来，
# 以后同工况直接按"当前起点 + 录制关节增量"回放，免掉全部 IK。
# 是否可回放由前端把关：距离一致 + 方向夹角 <10° + 起点关节偏差 <0.1 rad。


def sidestep_name(step_cm: float) -> str:
    """横移录制名：sidestep_L10cm / sidestep_R6cm（L/R 是**横移方向**：正=左移）。"""
    return f"sidestep_{'L' if step_cm > 0 else 'R'}{abs(step_cm):.0f}cm"


@router.get("/sidesteps")
def reach_list_sidesteps():
    """横移录制列表（只含本臂）。前端按距离 / 方向 / 起点匹配后直接把
    录制关节送 /execute 回放，所以这里的臂过滤就是安全关口：另一条臂的
    横移文件根本不会到前端手里。"""
    own_items, arm_stats = _split_by_arm(
        _load_dir_json(state.sidesteps_dir, by_mtime=False))
    return {"sidesteps": own_items, "arm": _own_arm(), "arm_hidden": arm_stats}


@router.post("/sidesteps")
def reach_save_sidestep(body: dict):
    """保存一次横移规划（同臂同距离覆盖旧的）。
    Body: {"step_cm": float, "direction_root": [3], "waypoints": [{named_joints, tcp_pose}...]}
    落盘 data/sidesteps/<R|L>-sidestep_<L|R><n>cm.json：开头的 R-/L- 是臂归属，
    后面的 L/R 是横移方向。
    """
    try:
        step_cm = float(body["step_cm"])
        direction = [float(v) for v in body["direction_root"]]
        waypoints = list(body["waypoints"])
        if len(waypoints) < 2:
            raise ValueError("路点太少")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"参数非法: {exc}"}, status_code=400)
    item = {
        "name": arm_assets.prefixed_name(_own_arm(), sidestep_name(step_cm)),
        "arm": _own_arm(),
        "chain_id": state.chain_id,
        "step_cm": step_cm,
        "direction_root": direction,
        "waypoints": waypoints,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        # 前端送来的关节名必须是本臂的（防止别处拼出的异臂关节角落盘）
        arm_assets.check_asset_arm(item, _own_arm(), "横移录制")
    except arm_assets.ArmMismatch as exc:
        return _arm_reject(exc)
    state.sidesteps_dir.mkdir(parents=True, exist_ok=True)
    path = state.sidesteps_dir / f"{item['name']}.json"
    path.write_text(json.dumps(item, ensure_ascii=False))
    item["file"] = path.name
    return {"ok": True, "sidestep": item}
