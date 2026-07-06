from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.collision import SimpleCollisionChecker
from core.config import (
    DEFAULT_CURRENT_JOINTS,
    DEFAULT_DURATION,
    DEFAULT_STEPS,
    DEFAULT_TCP_OFFSET,
    PROJECT_ROOT,
    URDF_PATH,
    WEB_DIR,
    validate_arm,
)
from core.ik_solver import NumericalIKSolver
from core.robot_model import RobotModel
from core.trajectory import TrajectoryPlanner


class IKRequest(BaseModel):
    current_joints: list[float] | dict[str, float] = Field(default_factory=lambda: DEFAULT_CURRENT_JOINTS.copy())
    target_xyz: list[float] = Field(default_factory=lambda: [0.34, 0.28, 0.65])
    tcp_offset: list[float] = Field(default_factory=lambda: DEFAULT_TCP_OFFSET.copy())
    arm: Literal["left", "right"] = "left"


class TrajectoryRequest(BaseModel):
    current_joints: list[float] | dict[str, float] = Field(default_factory=lambda: DEFAULT_CURRENT_JOINTS.copy())
    target_joints: list[float] | dict[str, float]
    tcp_offset: list[float] = Field(default_factory=lambda: DEFAULT_TCP_OFFSET.copy())
    arm: Literal["left", "right"] = "left"
    duration: float = DEFAULT_DURATION
    steps: int = DEFAULT_STEPS


class CollisionRequest(BaseModel):
    joints: list[float] | dict[str, float] | None = None
    waypoints: list[dict[str, Any]] | None = None
    tcp_offset: list[float] = Field(default_factory=lambda: DEFAULT_TCP_OFFSET.copy())
    arm: Literal["left", "right"] = "left"


class DemoPlanRequest(BaseModel):
    current_joints: list[float] | dict[str, float] = Field(default_factory=lambda: DEFAULT_CURRENT_JOINTS.copy())
    target_xyz: list[float] = Field(default_factory=lambda: [0.34, 0.28, 0.65])
    tcp_offset: list[float] = Field(default_factory=lambda: DEFAULT_TCP_OFFSET.copy())
    arm: Literal["left", "right"] = "left"
    duration: float = DEFAULT_DURATION
    steps: int = DEFAULT_STEPS


app = FastAPI(title="G1-D Arm IK Demo", version="0.1.0")
robot = RobotModel(URDF_PATH)
ik_solver = NumericalIKSolver(robot)
trajectory_planner = TrajectoryPlanner(robot)
collision_checker = SimpleCollisionChecker(robot)

app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
app.mount("/assets", StaticFiles(directory=PROJECT_ROOT / "assets"), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/robot/metadata")
def robot_metadata() -> dict:
    left_tcp = robot.tcp_position(DEFAULT_CURRENT_JOINTS, "left", DEFAULT_TCP_OFFSET)
    right_tcp = robot.tcp_position(DEFAULT_CURRENT_JOINTS, "right", DEFAULT_TCP_OFFSET)
    metadata = robot.metadata()
    metadata.update(
        {
            "default_arm": "left",
            "default_current_joints": DEFAULT_CURRENT_JOINTS,
            "default_tcp_offset": DEFAULT_TCP_OFFSET,
            "default_targets": {
                "left": [float(left_tcp[0] + 0.04), float(left_tcp[1] + 0.04), float(left_tcp[2] + 0.03)],
                "right": [float(right_tcp[0] + 0.04), float(right_tcp[1] - 0.04), float(right_tcp[2] + 0.03)],
            },
        }
    )
    return metadata


@app.post("/api/ik/solve")
def solve_ik(request: IKRequest) -> dict:
    try:
        result = ik_solver.solve(request.current_joints, request.target_xyz, request.tcp_offset, request.arm)
        return {
            "arm": request.arm,
            "joint_names": robot.arm_joint_names(request.arm),
            **result.to_dict(),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/trajectory/plan")
def plan_trajectory(request: TrajectoryRequest) -> dict:
    try:
        waypoints = trajectory_planner.plan(
            request.current_joints,
            request.target_joints,
            request.arm,
            request.tcp_offset,
            request.duration,
            request.steps,
        )
        return {
            "arm": request.arm,
            "joint_names": robot.arm_joint_names(request.arm),
            "waypoint_count": len(waypoints),
            "waypoints": waypoints,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/collision/check")
def check_collision(request: CollisionRequest) -> dict:
    try:
        arm = validate_arm(request.arm)
        if request.waypoints is not None:
            checks = collision_checker.check_trajectory(request.waypoints, arm, request.tcp_offset)
            return {
                "arm": arm,
                "status": _overall_collision_status(checks),
                "collision_count": sum(1 for item in checks if item["status"] == "collision"),
                "near_count": sum(1 for item in checks if item["status"] == "near"),
                "checks": checks,
            }
        if request.joints is None:
            raise ValueError("provide either joints or waypoints")
        return {"arm": arm, **collision_checker.check_state(request.joints, arm, request.tcp_offset)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/demo/plan")
def demo_plan(request: DemoPlanRequest) -> dict:
    try:
        arm = validate_arm(request.arm)
        ik = ik_solver.solve(request.current_joints, request.target_xyz, request.tcp_offset, arm)
        waypoints = trajectory_planner.plan(
            request.current_joints,
            ik.target_joints,
            arm,
            request.tcp_offset,
            request.duration,
            request.steps,
        )
        collision_checks = collision_checker.check_trajectory(waypoints, arm, request.tcp_offset)
        for waypoint, check in zip(waypoints, collision_checks, strict=True):
            waypoint["collision"] = {
                "status": check["status"],
                "min_distance_m": check["min_distance_m"],
                "min_distance_mm": check["min_distance_mm"],
                "pair": check["pair"],
                "shapes": check["shapes"],
            }
        overall = _overall_collision_status(collision_checks)
        return {
            "arm": arm,
            "joint_names": robot.arm_joint_names(arm),
            "target_xyz": request.target_xyz,
            "tcp_offset": request.tcp_offset,
            "ik": ik.to_dict(),
            "trajectory": {
                "duration": request.duration,
                "waypoint_count": len(waypoints),
                "waypoints": waypoints,
            },
            "collision": {
                "status": overall,
                "collision_count": sum(1 for item in collision_checks if item["status"] == "collision"),
                "near_count": sum(1 for item in collision_checks if item["status"] == "near"),
                "checks": collision_checks,
            },
            "message": "Offline simulation only. No robot connection or control command is used.",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _overall_collision_status(checks: list[dict]) -> str:
    statuses = {item["status"] for item in checks}
    if "collision" in statuses:
        return "collision"
    if "near" in statuses:
        return "near"
    return "safe"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
