"""Dual-arm workspace; legacy unscoped URLs retain the startup default arm."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, FileResponse
from starlette.concurrency import run_in_threadpool

from .state import ReachState, current_state, use_state, configure
from .dual_controller import DualArmController

ROOT = Path(__file__).resolve().parents[2]
ARMS = ("left_arm", "right_arm")
NO_CALIBRATION = {"pick", "confirm_pointcloud_pick", "latest_pick", "attach_pick_record",
                  "scan_obstacles", "plan_cartesian", "plan_arc", "plan_axis_last", "plan_orientation", "tcp/select",
                  "turn", "align_yaw"}
CONTROL = {"arm", "disarm", "hand_move", "execute", "sequences/run", "turn", "align_yaw",
           "tcp/select", "pink/anchor"}


def load_compensation(path, version, arm, hand_id):
    from core.gravity_profiles import load_registry, active_profile
    from core.payload_import import parameters_from_payload
    path = Path(path).expanduser().resolve()
    raw = json.loads(path.read_text())
    if "mass_kg" in raw:
        profile = {"version": "payload", "label": path.name,
                   "parameters": parameters_from_payload(raw, arm)}
    else:
        profile = active_profile(load_registry(path), version or None)
        compatible = profile.get("compatibility")
        if compatible and (compatible["arm"] != arm or compatible["hand_id"] != hand_id):
            raise ValueError("补偿版本与手臂/手型号不匹配")
    return {"config_path": str(path), "version": profile["version"], "label": profile["label"],
            "effective_parameters": profile["parameters"], "cli_overrides": {}}


class ArmWorkspace:
    def __init__(self, args, snapshot, primary, hand_runtime):
        self.args, self.snapshot = args, snapshot
        self.default_arm = primary.chain_id
        self.states = {primary.chain_id: primary}
        self.primary = primary
        self.lock = asyncio.Lock()
        self.owner = DualArmController(args.network_interface)
        self.selections = {}
        primary.hand_runtime = hand_runtime
        if hand_runtime is not None:
            hand_runtime.scoped_api = True
        primary.workspace = self
        primary.workspace_enabled = True
        self._setup_controller(primary)
        self._setup_collision(primary)
        for arm in ARMS:
            if arm != primary.chain_id:
                entry = ReachState()
                entry.chain_id = arm
                entry.workspace_enabled = False
                entry.workspace = self
                self.states[arm] = entry

    def _setup_controller(self, entry):
        params = dict(entry.gravity_profile.get("effective_parameters") or {})
        self.owner.configure(entry.chain_id.removesuffix("_arm"), {
            **params, "max_speed_rad_s": self.args.arm_max_speed,
            "kp": self.args.arm_kp, "kd": self.args.arm_kd,
            "kp_wrist": self.args.arm_kp_wrist, "kd_wrist": self.args.arm_kd_wrist,
        })
        entry.arm_factory = (lambda: self.owner.acquire(entry.chain_id.removesuffix("_arm"))) \
            if entry.provider_reader is not None else None

    def _setup_collision(self, entry):
        checker = entry.collision_checker
        if checker is not None and self.primary.motors_reader is not None:
            checker.other_arm_provider = lambda chain: checker.other_arm_snapshot
            self.refresh_collision(entry)

    def refresh_collision(self, entry):
        if entry.collision_checker is None or self.primary.motors_reader is None:
            return
        other_arm = "right_arm" if entry.chain_id == "left_arm" else "left_arm"
        other = self.states.get(other_arm)
        indices = list(range(22, 29)) if other_arm == "right_arm" else list(range(15, 22))
        values = self.primary.motors_reader(indices)
        if values is None or len(values) != 7 or not np.all(np.isfinite(values)):
            raise RuntimeError("无法取得另一侧手臂姿态，暂不能规划/执行")
        from core.types import Pose
        tcp = Pose(xyz=list(other.p_tool)) if other is not None and other.p_tool is not None \
            else entry.robot_model.tcp_offset(other_arm)
        entry.collision_checker.other_arm_snapshot = (
            other_arm, dict(zip(entry.robot_model.joint_names(other_arm), values)), tcp)

    def running(self):
        return [s for s in self.states.values() if s.exec_running or s.align_running]

    def check_other_path(self, entry, q_list):
        if self.primary.motors_reader is None:
            return
        from core.types import Pose
        self.refresh_collision(entry)
        checker = entry.collision_checker
        other_arm = "right_arm" if entry.chain_id == "left_arm" else "left_arm"
        tcp = Pose(xyz=list(entry.p_tool))
        path = np.asarray(q_list, dtype=float)
        for i in range(max(1, len(path) - 1)):
            q0, q1 = path[i], path[min(i + 1, len(path) - 1)]
            count = max(2, int(np.ceil(np.max(np.abs(q1 - q0)) / 0.04)) + 1)
            for q in np.linspace(q0, q1, count):
                check = checker.check_state(dict(zip(entry.joint_names, q)), entry.chain_id, tcp)
                for pair in check["pairs"]:
                    if pair["b"].startswith(other_arm + "_") and pair["distance_m"] <= 0:
                        raise RuntimeError(f"轨迹与另一侧手臂相交: {pair['a']} / {pair['b']}")

    def control_error(self, entry, operation, body):
        if operation == "align_yaw" and body.get("stop"):
            return None
        running = self.running()
        if operation in {"hand_move", "disarm", "arm", "pink/anchor", "tcp/select", "turn", "align_yaw"} and running:
            return "有手臂正在执行，请先停止轨迹"
        if operation in {"execute", "sequences/run", "turn", "align_yaw"}:
            if running:
                return "第一版双臂轨迹串行：已有手臂正在执行"
            if self.owner.error:
                return self.owner.error
            for arm, other in self.states.items():
                if other.controller is not None and other.controller.status().get("float"):
                    return f"{arm} 仍在卸力，请先恢复保持再执行轨迹"
        if operation == "disarm" and len(self.owner.active) > 1:
            return "另一侧仍接管中；本侧可保持或卸力，交还本体请使用「释放双臂」"
        return None

    def configure_arm(self, arm, body):
        if arm not in ARMS:
            raise ValueError("arm 必须是 left_arm/right_arm")
        old = self.states[arm]
        if self.running() or self.owner.engaged:
            raise ValueError("修改启用组合或补偿文件前请先停止并释放双臂")
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是 boolean")
        if not enabled:
            old.workspace_enabled = False
            self.selections[arm] = {**self.selections.get(arm, {}), **body, "enabled": False}
            return
        from core.capability_client import fetch_snapshot
        from core.capability_registry import (calib_abs_path, enabled_capabilities,
                                              claimed_sequence_names, claimed_waypoint_names)
        from core.calibration_bundle import compose_bound_calibration
        from core.hand_runtime import HandRuntime, build_hand_runtime_config, apply_mount_profile
        from core.collision import ConfigurableCollisionChecker
        from ik.numerical_solver import NumericalIKSolver
        from reach_server import _validate_camera_identity
        self.snapshot = fetch_snapshot(self.args.capability_url)
        registry = deepcopy(self.snapshot["registry"])
        hand_id = str(body.get("hand_id") or (old.active_combo or {}).get("hand_id") or "")
        hand = next((h for h in registry["hands"] if h["id"] == hand_id), None)
        if hand is None or hand.get("design_side") != arm.removesuffix("_arm"):
            raise ValueError("请选择属于该侧的手型号")
        combo = {"arm": arm, "hand_id": hand_id,
                 "camera_role": body.get("camera_role") or "head",
                 "motion_backend": body.get("motion_backend") or "legacy",
                 "mount_profile_id": body.get("mount_profile_id") or hand["mount_profiles"][0]["id"]}
        if combo["motion_backend"] not in {"legacy", "legacy_timed", "pink"}:
            raise ValueError("无效运动后端")
        # Both panels share the connected camera, never silently use a different extrinsic.
        if combo["camera_role"] != (self.primary.active_combo or {}).get("camera_role", "head"):
            raise ValueError("当前双臂窗口共享相机，请选择与启动相机相同的 camera_role")
        registry["active"] = combo
        composed = compose_bound_calibration(registry, arm, hand_id, combo["camera_role"])
        calibration = composed[0] if composed else None
        legacy_path = calib_abs_path(arm, hand_id)
        if calibration is None and legacy_path.is_file():
            calibration = json.loads(legacy_path.read_text())
        if calibration is not None:
            calibration, _ = apply_mount_profile(registry, calibration)
            if self.primary.camera is not None:
                _validate_camera_identity(calibration, self.primary.camera.info())
        gravity = load_compensation(body.get("gravity_file") or self.args.gravity_profiles,
                                    body.get("gravity_version"), arm, hand_id)
        entry = ReachState()
        entry.workspace, entry.workspace_enabled = self, True
        entry.hand_runtime = None
        model = self.primary.robot_model
        reader = None
        if self.primary.motors_reader is not None:
            indices = list(range(15, 22)) if arm == "left_arm" else list(range(22, 29))
            reader = lambda: np.asarray(self.primary.motors_reader(indices), dtype=float)
        with use_state(entry):
            configure(camera=self.primary.camera, wrist_camera=self.primary.wrist_camera,
                      robot_model=model, robot_id=self.primary.robot_id, chain_id=arm,
                      calib_path=None, calibration=calibration,
                      robot_only=self.primary.robot_only,
                      collision_checker=ConfigurableCollisionChecker(model),
                      ik_solver=NumericalIKSolver(model, self.primary.ik_solver.default_options),
                      joints_reader=reader, torso_reader=self.primary.torso_reader,
                      motors_reader=self.primary.motors_reader,
                      tool_out_mm=float(hand.get("tool_out_mm") or 0),
                      yolo_base=self.primary.yolo_base, gravity_profile=gravity,
                      settle_trim=self.primary.settle_trim)
            entry.active_combo, entry.robot_identity = combo, registry.get("robot")
            entry.capability_url = self.primary.capability_url
            entry.visible_sequences, entry.visible_waypoints = set(), set()
            for cap in enabled_capabilities(registry, arm, hand_id):
                entry.visible_sequences.update(claimed_sequence_names(registry, cap["id"]))
                entry.visible_waypoints.update(claimed_waypoint_names(registry, cap["id"], self.snapshot.get("sequence_pool") or []))
            if calibration is not None:
                hc = build_hand_runtime_config(registry=registry, calibration=calibration,
                                               chain_id=arm, expected_wrist_link=model.end_link(arm),
                                               service_url=str(body.get("hand_service_url") or self.args.hand_service_url),
                                               assets_root=self.args.hand_assets_root)
                if hc:
                    entry.hand_runtime = HandRuntime(hc, verify_tls=self.args.hand_service_verify_tls)
                    entry.hand_runtime.scoped_api = True
                    if body.get("hand_port"):
                        entry.hand_runtime.connection_options = {"port": str(body["hand_port"])}
                from .tcp import apply_startup_default
                apply_startup_default()
            entry.motion_backend = combo["motion_backend"]
            entry.exec_backend = entry.motion_backend
            try:
                from .execution_pink import PinkRuntime
                from .lowstate import H2LowStateSampler, MockLowStateSampler
                sampler = self.primary.pink_runtime.sampler if self.primary.pink_runtime is not None else (
                    H2LowStateSampler(self.args.network_interface) if reader is not None else MockLowStateSampler())
                entry.pink_runtime = PinkRuntime(arm_side=arm.removesuffix("_arm"), sampler=sampler,
                                                 wrist_link=model.end_link(arm))
                # Both arms use exactly the same world anchor and estimator.
                if self.primary.pink_runtime is not None:
                    entry.pink_runtime.world_frame = self.primary.pink_runtime.world_frame
                    entry.pink_runtime._lock = self.primary.pink_runtime._lock
            except (ImportError, RuntimeError, ValueError) as exc:
                entry.pink_error = str(exc)
                if entry.motion_backend == "pink":
                    entry.motion_backend = entry.exec_backend = "legacy"
        self._setup_controller(entry)
        self._setup_collision(entry)
        self.states[arm] = entry
        if arm == self.default_arm:
            # Legacy API clients remain attached to the default side after reconfiguration.
            self.primary = entry
        self.selections[arm] = {**combo, "enabled": True, "gravity_file": gravity["config_path"],
                                "gravity_version": body.get("gravity_version") or "",
                                "hand_port": str(body.get("hand_port") or ""),
                                "hand_service_url": str(body.get("hand_service_url") or self.args.hand_service_url)}

    def info(self):
        from .service import reach_status
        from core.gravity_profiles import load_registry
        gravity = load_registry(self.args.gravity_profiles)
        entries = {}
        for arm, entry in self.states.items():
            with use_state(entry):
                entries[arm] = {"enabled": entry.workspace_enabled,
                                "selection": self.selections.get(arm) or {
                                    **(entry.active_combo or {}),
                                    "gravity_file": entry.gravity_profile.get("config_path", str(self.args.gravity_profiles)),
                                    "gravity_version": entry.gravity_profile.get("version", "")},
                                "status": reach_status() if entry.enabled else None}
                entries[arm]["align_running"] = entry.align_running
        return {"ok": True, "default_arm": self.default_arm, "arms": entries,
                "gravity_profiles": gravity["versions"],
                "gravity_active_version": gravity["active_version"],
                "runtime_available": True,
                "hands": self.snapshot["registry"].get("hands", []),
                "control_error": self.owner.error,
                "shared_control_active": self.owner.engaged}


class ArmContextMiddleware:
    def __init__(self, app, workspace):
        self.app, self.workspace = app, workspace

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        scoped_arm = path.startswith("/api/arms/")
        arm = self.workspace.default_arm
        if path.startswith("/api/arms/"):
            parts = path.split("/", 4)
            if len(parts) != 5 or parts[3] not in ARMS:
                return await JSONResponse({"ok": False, "error": "未知手臂"}, 404)(scope, receive, send)
            arm = parts[3]
            scope = dict(scope, path="/api/" + parts[4], raw_path=("/api/" + parts[4]).encode())
            path = scope["path"]
        entry = self.workspace.states[arm]
        operation = path.removeprefix("/api/reach/")
        body = {}
        if scope["method"] == "POST" and path.startswith("/api/") and not path.startswith("/api/dual/"):
            raw_body = await Request(scope, receive).body()
            try:
                body = json.loads(raw_body) if raw_body else {}
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if scoped_arm and path in {"/api/trajectory/plan", "/api/collision/check", "/api/demo/plan", "/api/demo/solve_and_plan"} and body.get("chain_id", arm) != arm:
                return await JSONResponse({"ok": False, "error": "规划手臂与当前窗口不一致"}, 409)(scope, receive, send)
            consumed = False
            original_receive = receive
            async def replay_receive():
                nonlocal consumed
                if consumed:
                    return await original_receive()
                consumed = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            receive = replay_receive
        with use_state(entry):
            if path.startswith("/api/") and not path.startswith("/api/dual/"):
                # The shared scene needs these read-only calculations even
                # when the default arm is inactive. Control remains blocked.
                if not entry.workspace_enabled and path not in {
                    "/api/reach/status", "/api/robot/metadata", "/api/fk", "/api/collision/check",
                }:
                    return await JSONResponse({"ok": False, "error": "该侧尚未启用"}, 409)(scope, receive, send)
                if operation in NO_CALIBRATION and not entry.handeye_ready:
                    return await JSONResponse({"ok": False, "error": "该侧尚无可用标定，视觉选点/笛卡尔规划不可用"}, 409)(scope, receive, send)
            mutation = scope["method"] == "POST" and operation in CONTROL
            if mutation:
                async with self.workspace.lock:
                    error = self.workspace.control_error(entry, operation, body)
                    if error:
                        return await JSONResponse({"ok": False, "error": error}, 409)(scope, receive, send)
                    return await self._run(scope, receive, send, entry)
            return await self._run(scope, receive, send, entry)

    async def _run(self, scope, receive, send, entry):
        path = scope["path"]
        if scope["method"] == "POST" and ("plan" in path or path.endswith(("/execute", "/run", "/check"))):
            try:
                await run_in_threadpool(self.workspace.refresh_collision, entry)
            except Exception as exc:
                return await JSONResponse({"ok": False, "error": str(exc)}, 409)(scope, receive, send)
        return await self.app(scope, receive, send)


def install_workspace(app, args, snapshot, primary, hand_runtime):
    workspace = ArmWorkspace(args, snapshot, primary, hand_runtime)
    router = APIRouter()

    @router.get("/arms")
    def page():
        return FileResponse(ROOT / "web/dual.html", headers={"Cache-Control": "no-store"})

    @router.get("/api/dual/status")
    def status():
        return workspace.info()

    @router.post("/api/dual/reload-config/{arm}")
    async def reload_config(arm: str):
        """Consume one desired arm selection from 18000 and apply it in memory."""
        from core.capability_client import fetch_snapshot
        async with workspace.lock:
            try:
                latest = await run_in_threadpool(
                    fetch_snapshot, workspace.args.capability_url)
                desired = ((latest.get("arm_workspace") or {}).get("arms") or {}).get(arm)
                if arm not in ARMS or not isinstance(desired, dict):
                    raise ValueError("18000 未返回该侧双臂配置")
                selection = desired.get("selection") or desired
                await run_in_threadpool(workspace.configure_arm, arm, selection)
                workspace.snapshot = latest
                return workspace.info()
            except (OSError, ValueError, RuntimeError) as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, 409)

    @router.post("/api/dual/config/{arm}")
    async def obsolete_config_writer(arm: str):
        return JSONResponse({
            "ok": False,
            "error": "双臂配置已迁移至18000，请调用 /api/capability/arms/{arm}",
        }, 410)

    @router.post("/api/dual/gravity/import")
    async def obsolete_gravity_import():
        return JSONResponse({
            "ok": False,
            "error": "重力补偿导入已迁移至18000",
        }, 410)

    @router.post("/api/dual/hand_move")
    async def unload(body: dict):
        async with workspace.lock:
            from .execution import reach_hand_move
            entries = [s for s in workspace.states.values() if s.workspace_enabled and s.controller is not None]
            if workspace.running() or not entries:
                return JSONResponse({"ok": False, "error": "请先接管手臂并停止轨迹"}, 409)
            if any(s.controller.status().get("jog_enabled") for s in entries):
                return JSONResponse({"ok": False, "error": "请先停止点动"}, 409)
            if not isinstance(body.get("on"), bool):
                return JSONResponse({"ok": False, "error": "on 必须为 boolean"}, 400)
            # The publisher cannot observe a half-applied paired transition.
            def apply_both():
                with workspace.owner.lock:
                    for entry in entries:
                        with use_state(entry):
                            result = reach_hand_move(body)
                            if isinstance(result, JSONResponse):
                                return result
            result = await run_in_threadpool(apply_both)
            if result is not None:
                return result
            return workspace.info()

    @router.post("/api/dual/disarm")
    async def release():
        async with workspace.lock:
            if workspace.running():
                return JSONResponse({"ok": False, "error": "请先停止双臂轨迹"}, 409)
            for entry in workspace.states.values():
                entry.cabinet_plan_revision += 1
            await run_in_threadpool(workspace.owner.shutdown)
            for entry in workspace.states.values():
                entry.controller = None
            return workspace.info()

    @router.post("/api/dual/stop")
    async def stop():
        from .execution import reach_stop
        from .locomotion import reach_align_yaw
        for entry in workspace.states.values():
            with use_state(entry):
                if entry.align_running:
                    await run_in_threadpool(reach_align_yaw, {"stop": True})
                if entry.controller is not None:
                    await run_in_threadpool(reach_stop)
        return {"ok": True}

    app.include_router(router)
    app.add_middleware(ArmContextMiddleware, workspace=workspace)
    from fastapi.middleware.cors import CORSMiddleware
    # 18000 may optionally read runtime status or ask this process to reload a
    # saved selection. 18001 never persists desired configuration.
    app.add_middleware(CORSMiddleware,
                       allow_origin_regex=r"https?://[^/]+:(18000|5173)",
                       allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])
    app.state.arm_workspace = workspace
    try:
        saved = (snapshot.get("arm_workspace") or {}).get("arms") or {}
        for arm, item in saved.items():
            selection = item.get("selection") if isinstance(item, dict) else None
            if arm in ARMS and isinstance(selection, dict):
                workspace.configure_arm(arm, selection)
    except Exception as exc:
        print(f"[reach] 18000 双臂配置应用失败，保留可用侧: {exc}")
    return workspace
