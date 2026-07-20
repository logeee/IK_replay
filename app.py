from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.collision import ConfigurableCollisionChecker
from core.fk import compute_fk
from core.robot_config import PROJECT_ROOT, load_app_config, project_relative
from core.robot_model import RobotModel
from core.types import IKRequest, Pose, TrajectoryRequest
from ik.dummy_solver import DummyIKSolver
from ik.numerical_solver import NumericalIKSolver
from planners.linear import LinearTrajectoryPlanner
from planners.quintic import QuinticTrajectoryPlanner
from planners.switch_operation import H2SwitchOperationPlanner


WEB_DIR = PROJECT_ROOT / "web"
ASSETS_DIR = PROJECT_ROOT / "assets"


class PosePayload(BaseModel):
    xyz: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    rpy: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])


class FKPayload(BaseModel):
    robot: str | None = None
    chain_id: str = "left_arm"
    joints: list[float] | dict[str, float]
    tcp_offset: PosePayload | None = None


class IKPayload(BaseModel):
    robot: str | None = None
    chain_id: str = "left_arm"
    current_joints: list[float] | dict[str, float]
    target_pose: PosePayload
    tcp_offset: PosePayload | None = None
    solver: str | None = None
    seed: list[float] | dict[str, float] | None = None
    solver_options: dict[str, Any] = Field(default_factory=dict)


class TrajectoryPayload(BaseModel):
    robot: str | None = None
    chain_id: str = "left_arm"
    current_joints: list[float] | dict[str, float]
    target_joints: list[float] | dict[str, float]
    tcp_offset: PosePayload | None = None
    duration: float | None = None
    steps: int | None = None
    planner_type: str | None = None
    planner_options: dict[str, Any] = Field(default_factory=dict)
    check_collision: bool = True


class CollisionPayload(BaseModel):
    robot: str | None = None
    chain_id: str = "left_arm"
    joints: list[float] | dict[str, float] | None = None
    waypoints: list[dict[str, Any]] | None = None
    tcp_offset: PosePayload | None = None


class DemoPayload(BaseModel):
    robot: str | None = None
    chain_id: str = "left_arm"
    current_joints: list[float] | dict[str, float]
    target_pose: PosePayload
    tcp_offset: PosePayload | None = None
    solver: str | None = None
    solver_options: dict[str, Any] = Field(default_factory=dict)
    planner_type: str | None = None
    duration: float | None = None
    steps: int | None = None


class LegacyDemoPayload(BaseModel):
    robot: str | None = None
    chain_id: str | None = None
    arm: str | None = None
    current_joints: list[float] | dict[str, float]
    target_xyz: list[float] | None = None
    target_pose: PosePayload | None = None
    tcp_offset: Any = None
    solver: str | None = None
    solver_options: dict[str, Any] = Field(default_factory=dict)
    planner_type: str | None = None
    duration: float | None = None
    steps: int | None = None


class SwitchOperationPayload(BaseModel):
    robot: str = "h2"
    chain_id: str = "right_arm"
    current_joints: list[float] | dict[str, float] | None = None
    duration: float = 8.0
    steps: int = 120
    turn_angle_deg: float = 45.0
    approach_distance: float = 0.08
    switch_distance_m: float = 0.40
    switch_lateral_m: float = 0.08
    switch_height_m: float = 0.43
    lever_length: float = 0.075
    fingertip_length: float = 0.15
    use_current_posture: bool = True
    use_natural_posture: bool = False
    posture_weight: float = 0.008
    orientation_weight: float = 0.08
    start_angle_deg: float = -25.0
    end_angle_deg: float = 25.0
    path_type: str = "arc"


config = load_app_config()
robots = {robot_id: RobotModel(robot_config) for robot_id, robot_config in config.robots.items()}
solvers = {
    robot_id: {
        "numerical": NumericalIKSolver(robot_model, config.ik),
        "dummy": DummyIKSolver(robot_model),
    }
    for robot_id, robot_model in robots.items()
}
planners = {
    robot_id: {
        "linear": LinearTrajectoryPlanner(robot_model),
        "quintic": QuinticTrajectoryPlanner(robot_model),
    }
    for robot_id, robot_model in robots.items()
}
collision_checkers = {robot_id: ConfigurableCollisionChecker(robot_model) for robot_id, robot_model in robots.items()}
switch_planner = H2SwitchOperationPlanner(robots["h2"], solvers["h2"]["numerical"])

app = FastAPI(title="IK Replay Debug Viewer", version="0.2.0")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/robot/metadata")
def robot_metadata(robot: str | None = Query(default=None)) -> dict[str, Any]:
    try:
        robot_id, robot_model = _select_robot(robot)
        metadata = robot_model.metadata()
        chain_defaults: dict[str, Any] = {}
        for chain_id in robot_model.chain_ids:
            initial = robot_model.initial_joints(chain_id)
            tcp_offset = robot_model.tcp_offset(chain_id)
            current_tcp = robot_model.tcp_pose(initial, chain_id, tcp_offset)
            y_sign = -1.0 if robot_model.chain_config(chain_id).panel_side == "left" else 1.0
            target_pose = Pose(
                xyz=[
                    float(current_tcp.xyz[0] + 0.04),
                    float(current_tcp.xyz[1] + 0.04 * y_sign),
                    float(current_tcp.xyz[2] + 0.03),
                ],
                rpy=current_tcp.rpy,
            )
            chain_defaults[chain_id] = {
                "default_target_pose": target_pose.to_dict(),
            }
            if robot_id == "h2" and chain_id == "right_arm":
                chain_defaults[chain_id]["switch_natural_posture"] = robot_model.named_chain_joints(
                    H2SwitchOperationPlanner.NATURAL_POINTING_SEED, chain_id
                )

        for chain_id, chain_data in chain_defaults.items():
            metadata["chains"][chain_id].update(chain_data)

        metadata.update(
            {
                "active_robot": robot_id,
                "available_robots": [
                    {
                        "name": robot_id,
                        "display_name": robot_config.display_name,
                        "urdf_path": project_relative(robot_config.urdf_path),
                    }
                    for robot_id, robot_config in config.robots.items()
                ],
                "available_ik_solvers": sorted(solvers[robot_id]),
                "active_ik_solver": str(config.ik.get("solver", "numerical")),
                "available_planners": sorted(planners[robot_id]),
                "active_planner": str(config.trajectory.get("planner", "quintic")),
                "trajectory_defaults": {
                    "duration": float(config.trajectory.get("duration", 4.0)),
                    "steps": int(config.trajectory.get("steps", 80)),
                },
                "collision": collision_checkers[robot_id].metadata(),
                "offline_only": True,
                "config_paths": {
                    "default": project_relative(PROJECT_ROOT / "config" / "default.yaml"),
                    "robot": project_relative(config.robots[robot_id].urdf_path),
                },
            }
        )
        return metadata
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/fk")
def fk(payload: FKPayload) -> dict[str, Any]:
    try:
        robot_id, robot_model = _select_robot(payload.robot)
        tcp_offset = _pose_or_default(payload.tcp_offset, robot_model.tcp_offset(payload.chain_id))
        result = compute_fk(robot_model, payload.chain_id, payload.joints, tcp_offset)
        return {"success": True, "robot": robot_id, "chain_id": payload.chain_id, **result}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ik/solve")
def solve_ik(payload: IKPayload) -> dict[str, Any]:
    try:
        robot_id, robot_model = _select_robot(payload.robot)
        solver_name = _select_name(payload.solver, config.ik.get("solver", "numerical"), solvers[robot_id])
        request = IKRequest(
            chain_id=payload.chain_id,
            current_joints=payload.current_joints,
            target_pose=_pose(payload.target_pose),
            tcp_offset=_pose_or_default(payload.tcp_offset, robot_model.tcp_offset(payload.chain_id)),
            base_link=robot_model.base_link(payload.chain_id),
            end_link=robot_model.end_link(payload.chain_id),
            joint_names=robot_model.joint_names(payload.chain_id),
            seed=payload.seed,
            solver_options=payload.solver_options,
        )
        result = solvers[robot_id][solver_name].solve(request)
        return {
            "robot": robot_id,
            "chain_id": payload.chain_id,
            "solver": solver_name,
            "base_link": robot_model.base_link(payload.chain_id),
            "end_link": robot_model.end_link(payload.chain_id),
            "joint_names": robot_model.joint_names(payload.chain_id),
            **result.to_dict(),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/trajectory/plan")
def plan_trajectory(payload: TrajectoryPayload) -> dict[str, Any]:
    try:
        robot_id, robot_model = _select_robot(payload.robot)
        planner_name = _select_name(payload.planner_type, config.trajectory.get("planner", "quintic"), planners[robot_id])
        request = TrajectoryRequest(
            chain_id=payload.chain_id,
            current_joints=payload.current_joints,
            target_joints=payload.target_joints,
            tcp_offset=_pose_or_default(payload.tcp_offset, robot_model.tcp_offset(payload.chain_id)),
            duration=float(payload.duration if payload.duration is not None else config.trajectory.get("duration", 4.0)),
            steps=int(payload.steps if payload.steps is not None else config.trajectory.get("steps", 80)),
            planner_options=payload.planner_options,
        )
        waypoints = planners[robot_id][planner_name].plan(request)
        waypoint_dicts = [waypoint.to_dict() for waypoint in waypoints]
        collision_summary = None
        if payload.check_collision:
            collision_checks = collision_checkers[robot_id].check_trajectory(waypoints, payload.chain_id, request.tcp_offset)
            collision_summary = collision_checkers[robot_id].summarize_checks(collision_checks)
            for waypoint, check in zip(waypoint_dicts, collision_checks, strict=True):
                waypoint["collision"] = _frame_collision_summary(check)
        return {
            "robot": robot_id,
            "chain_id": payload.chain_id,
            "planner": planner_name,
            "duration": request.duration,
            "steps": request.steps,
            "waypoint_count": len(waypoints),
            "joint_names": robot_model.joint_names(payload.chain_id),
            "waypoints": waypoint_dicts,
            "collision": collision_summary,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/collision/check")
def check_collision(payload: CollisionPayload) -> dict[str, Any]:
    try:
        robot_id, robot_model = _select_robot(payload.robot)
        robot_model.chain_config(payload.chain_id)
        tcp_offset = _pose_or_default(payload.tcp_offset, robot_model.tcp_offset(payload.chain_id))
        checker = collision_checkers[robot_id]
        if payload.waypoints is not None:
            checks = checker.check_trajectory(payload.waypoints, payload.chain_id, tcp_offset)
            return {
                "robot": robot_id,
                "chain_id": payload.chain_id,
                **checker.summarize_checks(checks),
            }
        if payload.joints is None:
            raise ValueError("provide either joints or waypoints")
        return {
            "robot": robot_id,
            "chain_id": payload.chain_id,
            **checker.check_state(payload.joints, payload.chain_id, tcp_offset),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/demo/solve_and_plan")
def solve_and_plan(payload: DemoPayload) -> dict[str, Any]:
    try:
        ik_response = solve_ik(
            IKPayload(
                robot=payload.robot,
                chain_id=payload.chain_id,
                current_joints=payload.current_joints,
                target_pose=payload.target_pose,
                tcp_offset=payload.tcp_offset,
                solver=payload.solver,
                solver_options=payload.solver_options,
            )
        )
        trajectory_response = plan_trajectory(
            TrajectoryPayload(
                robot=payload.robot,
                chain_id=payload.chain_id,
                current_joints=payload.current_joints,
                target_joints=ik_response["target_joints"],
                tcp_offset=payload.tcp_offset,
                duration=payload.duration,
                steps=payload.steps,
                planner_type=payload.planner_type,
            )
        )
        robot_id, robot_model = _select_robot(payload.robot)
        return {
            "robot": robot_id,
            "chain_id": payload.chain_id,
            "ik": ik_response,
            "trajectory": trajectory_response,
            "collision": trajectory_response.get("collision"),
            "target_pose": _pose(payload.target_pose).to_dict(),
            "tcp_offset": _pose_or_default(payload.tcp_offset, robot_model.tcp_offset(payload.chain_id)).to_dict(),
            "message": "Offline IK replay only. No robot connection or command execution is used.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/demo/h2_switch_operation")
def h2_switch_operation(payload: SwitchOperationPayload) -> dict[str, Any]:
    try:
        if payload.robot != "h2" or payload.chain_id != "right_arm":
            raise ValueError("the first switch-operation demo supports H2 right_arm only")
        result = switch_planner.plan(
            current_joints=(
                payload.current_joints
                if payload.current_joints is not None
                else H2SwitchOperationPlanner.NATURAL_POINTING_SEED
            ),
            duration=payload.duration,
            steps=payload.steps,
            turn_angle_deg=payload.turn_angle_deg,
            approach_distance=payload.approach_distance,
            switch_distance_m=payload.switch_distance_m,
            switch_lateral_m=payload.switch_lateral_m,
            switch_height_m=payload.switch_height_m,
            lever_length=payload.lever_length,
            fingertip_length=payload.fingertip_length,
            use_current_posture=payload.use_current_posture,
            use_natural_posture=payload.use_natural_posture,
            posture_weight=payload.posture_weight,
            orientation_weight=payload.orientation_weight,
            start_angle_deg=payload.start_angle_deg,
            end_angle_deg=payload.end_angle_deg,
            path_type=payload.path_type,
            keyframe_only=True,
        )
        waypoint_dicts = [waypoint.to_dict() for waypoint in result.waypoints]
        checks = collision_checkers["h2"].check_trajectory(
            result.waypoints, "right_arm", result.fingertip_tcp
        )
        collision = collision_checkers["h2"].summarize_checks(checks)
        for frame, check, stage, contact, lever_angle in zip(
            waypoint_dicts,
            checks,
            result.stages,
            result.contact_states,
            result.lever_angles_deg,
            strict=True,
        ):
            frame["collision"] = _frame_collision_summary(check)
            frame["stage"] = stage
            frame["contact"] = contact
            frame["switch_angle_deg"] = lever_angle
        contact_index = next(
            (index for index, contact in enumerate(result.contact_states) if contact == "touch"),
            0,
        )
        contact_links = result.waypoints[contact_index].link_poses
        elbow_xyz = contact_links["right_elbow_link"].xyz
        wrist_xyz = contact_links["right_wrist_yaw_link"].xyz
        return {
            "robot": "h2",
            "chain_id": "right_arm",
            "coordinate_frame": "pelvis",
            "axis_convention": {"x": "forward", "y": "left", "z": "up"},
            "units": {"position": "m", "joint_angle": "rad", "switch_angle": "deg", "time": "s"},
            "duration": float(payload.duration),
            "waypoint_count": len(waypoint_dicts),
            "waypoints": waypoint_dicts,
            "collision": collision,
            "switch": {
                "pivot_xyz": result.pivot_xyz,
                "panel_normal": result.panel_normal,
                "lever_length": result.lever_length,
                "approach_distance": result.approach_distance,
                "turn_angle_deg": result.turn_angle_deg,
                "fingertip_tcp": result.fingertip_tcp.to_dict(),
                "initial_label": "竖直",
                "motion": "clockwise" if result.turn_angle_deg > 0 else "counterclockwise",
                "start_tip_xyz": result.start_tip_xyz,
                "end_tip_xyz": result.end_tip_xyz,
                "path_type": result.path_type,
            },
            "reference_posture": {
                "elbow_xyz": elbow_xyz,
                "wrist_xyz": wrist_xyz,
                "elbow_minus_wrist_z_m": float(elbow_xyz[2] - wrist_xyz[2]),
                "elbow_below_wrist": bool(elbow_xyz[2] < wrist_xyz[2]),
                "constraint_scale": result.constraint_scale,
                "constraints_relaxed": bool(result.constraint_scale < 0.999),
                "posture_reference_joints": robots["h2"].named_chain_joints(
                    result.posture_reference_joints, "right_arm"
                ),
            },
            "message": "H2 right-fingertip pendulum-switch replay; no command is sent to a robot.",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/demo/plan")
def legacy_demo_plan(payload: LegacyDemoPayload) -> dict[str, Any]:
    try:
        robot_id, robot_model = _select_robot(payload.robot)
        chain_id = _legacy_chain_id(payload)
        tcp_offset = _legacy_tcp_offset(payload.tcp_offset, robot_model.tcp_offset(chain_id))
        target_pose = payload.target_pose
        solver_options = dict(payload.solver_options)
        if target_pose is None:
            if payload.target_xyz is None:
                raise ValueError("provide target_pose or target_xyz")
            current_tcp = robot_model.tcp_pose(payload.current_joints, chain_id, tcp_offset)
            target_pose = PosePayload(xyz=payload.target_xyz, rpy=current_tcp.rpy)
            solver_options.setdefault("solve_orientation", False)
        return solve_and_plan(
            DemoPayload(
                robot=robot_id,
                chain_id=chain_id,
                current_joints=payload.current_joints,
                target_pose=target_pose,
                tcp_offset=PosePayload(xyz=tcp_offset.xyz, rpy=tcp_offset.rpy),
                solver=payload.solver,
                solver_options=solver_options,
                planner_type=payload.planner_type,
                duration=payload.duration,
                steps=payload.steps,
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _select_robot(requested: str | None) -> tuple[str, RobotModel]:
    robot_id = str(requested or config.active_robot)
    if robot_id not in robots:
        raise ValueError(f"unknown robot {robot_id!r}; available: {sorted(robots)}")
    return robot_id, robots[robot_id]


def _pose(payload: PosePayload) -> Pose:
    if len(payload.xyz) != 3 or len(payload.rpy) != 3:
        raise ValueError("pose.xyz and pose.rpy must both contain 3 values")
    return Pose(xyz=[float(v) for v in payload.xyz], rpy=[float(v) for v in payload.rpy])


def _pose_or_default(payload: PosePayload | None, default: Pose) -> Pose:
    return _pose(payload) if payload is not None else default


def _select_name(requested: str | None, fallback: Any, registry: dict[str, Any]) -> str:
    name = str(requested or fallback)
    if name not in registry:
        raise ValueError(f"unknown option {name!r}; available: {sorted(registry)}")
    return name


def _legacy_chain_id(payload: LegacyDemoPayload) -> str:
    if payload.chain_id:
        return payload.chain_id
    if payload.arm in {"left", "right"}:
        return f"{payload.arm}_arm"
    if payload.arm:
        return payload.arm
    return "left_arm"


def _legacy_tcp_offset(raw: Any, default: Pose) -> Pose:
    if raw is None:
        return default
    if isinstance(raw, PosePayload):
        return _pose(raw)
    if isinstance(raw, dict):
        return _pose(PosePayload(**raw))
    if isinstance(raw, list):
        if len(raw) != 3:
            raise ValueError("legacy tcp_offset list must contain 3 values")
        return Pose(xyz=[float(v) for v in raw], rpy=[0.0, 0.0, 0.0])
    raise ValueError("unsupported tcp_offset format")


def _frame_collision_summary(check: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": check["status"],
        "status_label": check["status_label"],
        "min_distance_m": check["min_distance_m"],
        "min_distance_mm": check["min_distance_mm"],
        "pair": check["pair"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
