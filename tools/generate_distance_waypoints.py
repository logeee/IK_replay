#!/usr/bin/env python3
"""From one arm waypoint, generate distance-indexed TCP-X retargeted waypoints.

The reference waypoint is kept unchanged. Targets on either side are solved from
the nearest already-solved distance so adjacent joint solutions remain continuous.
Dry-run is the default; pass --apply to write the JSON files.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from core import arm_assets  # noqa: E402
from core.types import IKRequest, Pose  # noqa: E402


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _distance_values(start: float, stop: float, step: float) -> list[float]:
    start_cm = round(start * 100)
    stop_cm = round(stop * 100)
    step_cm = round(step * 100)
    if step_cm <= 0 or start_cm > stop_cm:
        raise ValueError("距离范围或步长无效")
    values = [value / 100.0 for value in range(start_cm, stop_cm + 1, step_cm)]
    if any(abs(round(value * 100) / 100 - value) > 1e-9 for value in values):
        raise ValueError("距离必须按整厘米表示")
    return values


def _solve(
    *,
    solver: Any,
    model: Any,
    chain_id: str,
    tool: Pose,
    seed: dict[str, float],
    target_pose: Pose,
) -> tuple[Any, str]:
    common = {
        "chain_id": chain_id,
        "current_joints": seed,
        "target_pose": target_pose,
        "tcp_offset": tool,
        "base_link": model.base_link(chain_id),
        "end_link": model.end_link(chain_id),
        "joint_names": model.joint_names(chain_id),
        "seed": seed,
    }
    result = solver.solve(
        IKRequest(
            **common,
            solver_options={
                "solve_orientation": True,
                "tolerance_mm": 2.0,
                "rotation_tolerance_deg": 2.0,
                "regularization_weight": 0.001,
                "max_iterations": 320,
            },
        )
    )
    if result.success:
        return result, "strict_orientation"
    result = solver.solve(
        IKRequest(
            **common,
            solver_options={
                "solve_orientation": True,
                "tolerance_mm": 2.0,
                "rotation_tolerance_deg": 8.0,
                "rotation_weight": 0.05,
                "regularization_weight": 0.0002,
                "max_iterations": 320,
            },
        )
    )
    return result, "position_priority"


def generate(args: argparse.Namespace) -> list[dict[str, Any]]:
    source_path = args.source.resolve()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    arm = arm_assets.check_asset_arm(source, str(source.get("arm") or ""), "母版位点")
    chain_id = str(source.get("chain_id") or "")
    if chain_id != arm:
        raise ValueError(f"母版 chain_id={chain_id!r} 与 arm={arm!r} 不一致")
    joints = source.get("named_joints")
    if not isinstance(joints, dict) or not joints:
        raise ValueError("母版位点没有 named_joints")

    robot_id = str(source.get("robot") or "h2")
    model = app_module.robots[robot_id]
    solver = app_module.solvers[robot_id]["numerical"]
    checker = app_module.collision_checkers[robot_id]
    tool = model.tcp_offset(chain_id)
    reference_pose = model.tcp_pose(joints, chain_id, tool)

    distances = _distance_values(args.minimum, args.maximum, args.step)
    reference = round(args.reference, 2)
    if reference not in distances:
        raise ValueError("基准距离必须位于生成范围内")
    source_base = arm_assets.strip_arm_prefix(str(source["name"]))
    suffix = re.sub(r"^\d+(?:\.\d+)?-?", "", source_base, count=1)
    prefix = arm_assets.arm_prefix(arm)
    timestamp = args.timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    solved: dict[float, dict[str, float]] = {reference: dict(joints)}
    results: list[dict[str, Any]] = []

    branches = [
        sorted((value for value in distances if value < reference), reverse=True),
        sorted(value for value in distances if value > reference),
    ]
    for branch in branches:
        seed = dict(joints)
        for distance in branch:
            offset = round(distance - reference, 10)
            target_pose = Pose(
                xyz=[
                    float(reference_pose.xyz[0]) + offset,
                    float(reference_pose.xyz[1]),
                    float(reference_pose.xyz[2]),
                ],
                rpy=list(reference_pose.rpy),
            )
            result, solve_mode = _solve(
                solver=solver,
                model=model,
                chain_id=chain_id,
                tool=tool,
                seed=seed,
                target_pose=target_pose,
            )
            if not result.success:
                raise RuntimeError(
                    f"{distance:.2f}m IK 失败: {result.error_mm:.2f}mm / "
                    f"{math.degrees(result.error_rotation):.2f}°"
                )
            collision = checker.check_state(
                result.named_target_joints, chain_id, tool
            )
            if collision.get("status") == "collision":
                pair = collision.get("pair") or {}
                raise RuntimeError(
                    f"{distance:.2f}m 发生碰撞: "
                    f"{pair.get('a', '?')} ↔ {pair.get('b', '?')}"
                )
            solved[distance] = result.named_target_joints
            seed = result.named_target_joints
            name = f"{prefix}{distance:.2f}-{suffix}"
            output_path = source_path.parent / f"{name}_{timestamp}.json"
            if output_path.exists() and not args.force:
                raise FileExistsError(f"{output_path.name} 已存在")
            payload: dict[str, Any] = {
                "name": name,
                "chain_id": chain_id,
                "named_joints": result.named_target_joints,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "arm": arm,
                "distance_m": distance,
                "generated_from": source_path.name,
                "reference_distance_m": reference,
                "offset_root_m": [offset, 0.0, 0.0],
                "tcp_pose": result.tcp_pose.to_dict(),
                "tcp_offset": tool.to_dict(),
                "ik": {
                    "mode": solve_mode,
                    "error_mm": result.error_mm,
                    "error_rotation_deg": math.degrees(result.error_rotation),
                    "collision_status": collision.get("status"),
                    "minimum_model_clearance_mm": collision.get("min_distance_mm"),
                },
            }
            for key in ("recorded_combo", "arrival_speed_rad_s"):
                if key in source:
                    payload[key] = source[key]
            if args.apply:
                _atomic_json(output_path, payload)
            results.append(
                {
                    "distance_m": distance,
                    "file": output_path.name,
                    "mode": solve_mode,
                    "error_mm": result.error_mm,
                    "collision": collision.get("status"),
                    "clearance_mm": collision.get("min_distance_mm"),
                }
            )

    results.append(
        {
            "distance_m": reference,
            "file": source_path.name,
            "mode": "recorded_reference",
            "error_mm": 0.0,
            "collision": checker.check_state(joints, chain_id, tool).get("status"),
            "clearance_mm": checker.check_state(joints, chain_id, tool).get(
                "min_distance_mm"
            ),
        }
    )
    return sorted(results, key=lambda item: item["distance_m"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--reference", type=float, required=True)
    parser.add_argument("--minimum", type=float, required=True)
    parser.add_argument("--maximum", type=float, required=True)
    parser.add_argument("--step", type=float, default=0.01)
    parser.add_argument("--timestamp", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    rows = generate(args)
    action = "已写入" if args.apply else "dry-run"
    for row in rows:
        print(
            f"[{action}] {row['distance_m']:.2f}m  {row['mode']:<20} "
            f"err={row['error_mm']:.3f}mm  {row['collision']} "
            f"clearance={row['clearance_mm']:.1f}mm  {row['file']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
