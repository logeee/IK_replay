#!/usr/bin/env python3
"""Generate offline opening candidates: tuck inward, lift, then approach.

Outputs are deliberately kept outside the live waypoint/sequence libraries.
X is the robot URDF root forward axis with the waist at its model reference.
The setback is relative to the preparation TCP, NOT a measured wall clearance.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as app_module
from core.types import IKRequest, Pose


def smooth(u):
    return u**3 * (10 - 15*u + 6*u*u)


def forward_extent(shapes):
    """Maximum X of the configured arm capsules/spheres, not just the TCP."""
    values = []
    for name, shape in shapes.items():
        if not name.startswith("left_arm_"):
            continue
        if shape["kind"] == "sphere":
            values.append(shape["center"][0] + shape["radius"])
        elif shape["kind"] == "capsule":
            values.append(max(shape["a"][0], shape["b"][0]) + shape["radius"])
    if not values:
        raise ValueError("No arm collision geometry")
    return max(values)


def generate_one(endpoint_path, start, model, solver, checker, setback, retreat_x,
                 inward_m=.05):
    endpoint = json.loads(endpoint_path.read_text())
    assert endpoint["arm"] == endpoint["chain_id"] == "left_arm"
    names = model.joint_names("left_arm")
    q0 = np.array([start["named_joints"][n] for n in names])
    qg = np.array([endpoint["named_joints"][n] for n in names])
    tool = Pose(**endpoint["tcp_offset"])
    p0 = model.tcp_pose(q0, "left_arm", tool)
    pg = model.tcp_pose(qg, "left_arm", tool)
    x_lift = min(p0.xyz[0], pg.xyz[0]-setback, retreat_x)
    # Left arm: positive Y is the robot's left, so decreasing positive Y
    # moves toward the body's centre. Never tuck across the centre line.
    inward_y = max(0., p0.xyz[1]-inward_m)
    retracted = [x_lift, inward_y, p0.xyz[2]]
    lifted = [x_lift, pg.xyz[1], pg.xyz[2]]
    r0, rg = Rotation.from_euler("xyz", [p0.rpy, pg.rpy])
    rotations = Slerp([0, 1], Rotation.concatenate([r0, rg]))
    frames = [q0]
    phases = ["retract"]
    max_ik_mm = 0.0
    stages = [
        ("retract", p0.xyz, retracted, 0., 0.),
        ("lift", retracted, lifted, 0., 1.),
        ("approach", lifted, pg.xyz, 1., 1.),
    ]
    for phase, a, b, ra, rb in stages:
        steps = max(20, math.ceil(np.linalg.norm(np.array(b)-a)/.004))
        for u in np.linspace(0, 1, steps+1)[1:]:
            s = float(smooth(u))
            xyz = (np.array(a)*(1-s)+np.array(b)*s).tolist()
            angle = ra+(rb-ra)*s
            anchor = frames[-1]
            if phase == "approach":
                anchor = frames[-1]*(1-s)+qg*s
            result = solver.solve(IKRequest(
                chain_id="left_arm", current_joints=frames[-1].tolist(),
                target_pose=Pose(xyz=xyz, rpy=rotations(angle).as_euler("xyz").tolist()),
                tcp_offset=tool, base_link=model.base_link("left_arm"),
                end_link=model.end_link("left_arm"), joint_names=names,
                seed=frames[-1].tolist(), solver_options={
                    "solve_orientation": True, "tolerance_mm": 1.0,
                    "rotation_tolerance_deg": 1.0, "rotation_weight": .3,
                    "regularization_weight": .001,
                    "regularization_anchor": anchor.tolist(), "max_iterations": 240,
                }))
            if not result.success:
                raise ValueError(f"{phase}: {result.message}")
            max_ik_mm = max(max_ik_mm, result.error_mm)
            frames.append(np.array(result.target_joints)); phases.append(phase)
    # Preserve the recorded endpoint's actual joint configuration. Validate this
    # final connection just like every other segment; it has no exemptions.
    frames.append(qg); phases.append("approach")
    dense, dense_phases = [q0], ["retract"]
    for i, (a, b) in enumerate(zip(frames, frames[1:]), start=1):
        count = max(1, math.ceil(float(np.max(np.abs(b-a)))/.005))
        for t in np.linspace(0, 1, count+1)[1:]:
            dense.append(a+t*(b-a)); dense_phases.append(phases[i])
    dense = np.array(dense)
    lower, upper = model.joint_limits("left_arm")
    if not (np.all(dense >= lower) and np.all(dense <= upper)):
        raise ValueError("Joint limit violation")
    xyz, extents, clearances = [], [], []
    start_extent = forward_extent(checker.check_state(q0, "left_arm", tool)["shapes"])
    goal_extent = forward_extent(checker.check_state(qg, "left_arm", tool)["shapes"])
    for q, phase in zip(dense, dense_phases):
        fk = np.array(model.tcp_pose(q, "left_arm", tool).xyz)
        check = checker.check_state(q, "left_arm", tool)
        if check["status"] not in ("safe", "near"):
            raise ValueError(f"{phase}: model {check['status']}: {check.get('pair')}")
        if check["min_distance_mm"] < 10.:
            raise ValueError(f"{phase}: robot model clearance is below 10 mm")
        x_cap = p0.xyz[0] if phase == "retract" else x_lift if phase == "lift" else pg.xyz[0]
        if fk[0] > x_cap+.002:
            raise ValueError(f"{phase}: forward X constraint exceeded: {fk[0]} > {x_cap}")
        if phase == "approach" and abs(fk[2]-pg.xyz[2]) > .002:
            raise ValueError("Approach before reaching preparation height")
        extent = forward_extent(check["shapes"])
        extent_cap = start_extent if phase == "retract" else goal_extent-setback if phase == "lift" else goal_extent
        if extent > extent_cap+.002:
            raise ValueError(f"{phase}: arm forward extent {extent} exceeds {extent_cap}")
        if phase == "retract" and (fk[1] > p0.xyz[1]+.002 or abs(fk[2]-p0.xyz[2]) > .002):
            raise ValueError("Initial tuck must move inward before lifting")
        xyz.append(fk.tolist()); extents.append(extent)
        clearances.append(check["min_distance_mm"])
    linear = np.linspace(q0, qg, 301)
    linear_xyz, linear_extents = [], []
    for q in linear:
        linear_xyz.append(model.tcp_pose(q, "left_arm", tool).xyz)
        linear_extents.append(forward_extent(checker.check_state(q, "left_arm", tool)["shapes"]))
    # Conservative timing metadata for review, not a controller command.
    durations = np.maximum(np.max(np.abs(np.diff(dense, axis=0)), axis=1)/.20, .02)
    times = np.concatenate([[0.], np.cumsum(durations)])
    lift_mask = np.array(dense_phases) == "lift"
    payload = {
        "schema_version": 1, "status": "offline_candidate_not_enabled",
        "name": endpoint["name"].replace("预备点测试", "避让起手候选"),
        "arm": "left_arm", "chain_id": "left_arm", "distance_m": endpoint["distance_m"],
        "start_waypoint": start["name"], "endpoint_file": endpoint_path.name,
        "tcp_offset": tool.to_dict(), "setback_from_preparation_tcp_m": setback,
        "inward_m": inward_m, "inward_y_m": inward_y,
        "lift_tcp_x_limit_m": x_lift, "coordinate_frame": "URDF root, waist at model reference",
        "trajectory": {"joint_names": names, "frames": dense.tolist(),
                       "time_s": times.tolist(), "phases": dense_phases,
                       "tcp_xyz_m": xyz, "arm_forward_extent_x_m": extents},
        "linear_comparison": {"tcp_xyz_m": linear_xyz, "arm_forward_extent_x_m": linear_extents},
        "validation": {
            "samples": len(dense), "max_ik_error_mm": max_ik_mm,
            "min_robot_model_clearance_mm": min(clearances),
            "max_joint_step_rad": float(np.max(np.abs(np.diff(dense,axis=0)))),
            "lift_max_tcp_x_m": float(np.array(xyz)[lift_mask,0].max()),
            "lift_max_arm_extent_x_m": float(np.array(extents)[lift_mask].max()),
            "linear_max_tcp_x_m": float(np.array(linear_xyz)[:,0].max()),
            "linear_max_arm_extent_x_m": max(linear_extents),
            "start_and_endpoint_joints_exact": bool(np.array_equal(dense[0],q0) and np.array_equal(dense[-1],qg)),
            "wall_clearance_verified": False,
            "note": "Geometric motion-style prototype using the left arm model only; no image input or live execution.",
        },
    }
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--setback-m", type=float, default=.09)
    parser.add_argument("--retreat-x-m", type=float, default=.28)
    parser.add_argument("--inward-m", type=float, default=.05)
    parser.add_argument("--distance", type=float)
    args = parser.parse_args()
    out = args.output.resolve()
    if out == ROOT/"data" or ROOT/"data" in out.parents:
        raise ValueError("Offline candidates must not be written to the live data library")
    if not .03 <= args.setback_m <= .18 or not .15 <= args.retreat_x_m <= .40 or not 0 <= args.inward_m <= .08:
        raise ValueError("Invalid setback, inward amount or retreat X")
    out.mkdir(parents=True, exist_ok=True)
    paths = sorted((ROOT/"data/waypoints").glob("L-0.*-左到右预备点测试_*.json"))
    if args.distance is not None:
        paths = [p for p in paths if abs(json.loads(p.read_text())["distance_m"]-args.distance)<1e-8]
    if not paths:
        raise ValueError("No requested preparation waypoints found")
    start = json.loads((ROOT/"data/waypoints/L-起手点测试_20260721_042250.json").read_text())
    model = app_module.robots["h2"]
    solver = app_module.solvers["h2"]["numerical"]
    checker = app_module.collision_checkers["h2"]
    summary = []
    for path in paths:
        try:
            data = generate_one(path,start,model,solver,checker,args.setback_m,args.retreat_x_m,args.inward_m)
            target = out/(data["name"]+".json")
            target.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
            row = {"distance_m":data["distance_m"],"ok":True,"file":target.name,**data["validation"]}
        except ValueError as exc:
            row = {"distance_m":json.loads(path.read_text())["distance_m"],"ok":False,"error":str(exc)}
        summary.append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    return 0 if all(r["ok"] for r in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
