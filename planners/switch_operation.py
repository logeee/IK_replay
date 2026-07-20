from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from core.robot_model import RobotModel
from core.types import IKRequest, Pose, Waypoint
from ik.numerical_solver import NumericalIKSolver


@dataclass(frozen=True)
class SwitchOperationResult:
    waypoints: list[Waypoint]
    stages: list[str]
    contact_states: list[str]
    lever_angles_deg: list[float]
    pivot_xyz: list[float]
    panel_normal: list[float]
    lever_length: float
    approach_distance: float
    turn_angle_deg: float
    fingertip_tcp: Pose
    constraint_scale: float
    posture_reference_joints: list[float]
    start_tip_xyz: list[float]
    end_tip_xyz: list[float]
    path_type: str


class H2SwitchOperationPlanner:
    """Plan an H2 right-index-finger sweep over a pendulum-style switch.

    The switch lies on a vertical YZ panel in front of the robot. Its lever
    starts vertically downward. The fingertip first approaches along +X and
    then follows the lever tip around the pivot, clockwise or counterclockwise.
    IK is solved at every Cartesian contact sample using the previous solution
    as the next seed, which keeps the redundant 7-DoF arm continuous.
    """

    SAFE_CROSS_BODY_SEED = [-0.94, -0.44, 1.23, 0.19, 0.37, 0.18, 0.27]
    NATURAL_POINTING_SEED = [-0.60, -0.20, 0.37, -0.40, -1.71, 0.20, 0.15]

    def __init__(self, robot_model: RobotModel, solver: NumericalIKSolver):
        self.robot_model = robot_model
        self.solver = solver

    def plan(
        self,
        current_joints,
        duration: float = 8.0,
        steps: int = 120,
        turn_angle_deg: float = 45.0,
        approach_distance: float = 0.08,
        switch_distance_m: float = 0.40,
        switch_lateral_m: float = 0.08,
        switch_height_m: float = 0.43,
        lever_length: float = 0.075,
        fingertip_length: float = 0.15,
        use_current_posture: bool = True,
        use_natural_posture: bool = False,
        posture_weight: float = 0.008,
        orientation_weight: float = 0.08,
        start_angle_deg: float = -25.0,
        end_angle_deg: float = 25.0,
        path_type: str = "arc",
        keyframe_only: bool = True,
    ) -> SwitchOperationResult:
        chain_id = "right_arm"
        total_steps = max(70, min(400, int(steps)))
        duration = max(2.0, float(duration))
        angle = math.radians(max(-80.0, min(80.0, float(turn_angle_deg))))
        approach = max(0.04, min(0.18, float(approach_distance)))
        lever = max(0.04, min(0.12, float(lever_length)))
        finger = max(0.10, min(0.22, float(fingertip_length)))
        pivot = np.asarray(
            [
                max(0.22, min(0.55, float(switch_distance_m))),
                max(-0.02, min(0.24, float(switch_lateral_m))),
                max(0.10, min(0.90, float(switch_height_m))),
            ],
            dtype=float,
        )
        panel_normal = np.asarray([1.0, 0.0, 0.0])
        fingertip_tcp = Pose([finger, 0.0, 0.0], [0.0, 0.0, 0.0])
        q_start = self.robot_model.coerce_chain_joints(current_joints, chain_id)
        if use_natural_posture:
            q_posture = np.asarray(self.NATURAL_POINTING_SEED, dtype=float)
        elif use_current_posture:
            q_posture = q_start.copy()
        else:
            q_posture = np.asarray(self.SAFE_CROSS_BODY_SEED, dtype=float)
        reference_rpy = self.robot_model.tcp_pose(q_posture, chain_id, fingertip_tcp).rpy

        if keyframe_only:
            return self._plan_keyframes(
                pivot=pivot,
                lever=lever,
                fingertip_tcp=fingertip_tcp,
                q_posture=q_posture,
                reference_rpy=reference_rpy,
                posture_weight=posture_weight,
                orientation_weight=orientation_weight,
                start_angle=math.radians(max(-80.0, min(80.0, float(start_angle_deg)))),
                end_angle=math.radians(max(-80.0, min(80.0, float(end_angle_deg)))),
                path_type=path_type,
                duration=duration,
                total_steps=total_steps,
                panel_normal=panel_normal,
                approach=approach,
            )

        initial_tip = self._lever_tip(pivot, lever, 0.0)
        pre_tip = initial_tip - approach * panel_normal
        q_pre, constraint_scale = self._solve_position(
            pre_tip,
            q_posture,
            fingertip_tcp,
            q_posture,
            reference_rpy,
            posture_weight,
            orientation_weight,
            1.0,
        )

        # Five duplicate boundaries are removed while joining six phases.
        counts = self._allocate_counts(total_steps + 5, [0.27, 0.15, 0.05, 0.30, 0.07, 0.16])
        samples: list[tuple[np.ndarray, str, str, float]] = []

        self._append_joint_segment(samples, q_start, q_pre, counts[0], "移动到左胸前预接近位", "clear", 0.0)

        q_seed = q_pre
        for index in range(1, counts[1]):
            u = index / max(1, counts[1] - 1)
            target = pre_tip + panel_normal * (approach * self._quintic(u))
            q_seed, constraint_scale = self._solve_position(
                target,
                q_seed,
                fingertip_tcp,
                q_posture,
                reference_rpy,
                posture_weight,
                orientation_weight,
                constraint_scale,
            )
            samples.append((q_seed, "指尖直线接近拨杆", "clear" if index < counts[1] - 1 else "touch", 0.0))
        q_contact = q_seed.copy()

        for _ in range(1, counts[2]):
            samples.append((q_contact.copy(), "指尖接触拨杆末端", "touch", 0.0))

        for index in range(1, counts[3]):
            u = index / max(1, counts[3] - 1)
            theta = angle * self._quintic(u)
            target = self._lever_tip(pivot, lever, theta)
            q_seed, constraint_scale = self._solve_position(
                target,
                q_seed,
                fingertip_tcp,
                q_posture,
                reference_rpy,
                posture_weight,
                orientation_weight,
                constraint_scale,
            )
            samples.append((q_seed, "指尖沿圆弧拨动开关", "pushing", math.degrees(theta)))
        q_final_contact = q_seed.copy()

        for _ in range(1, counts[4]):
            samples.append((q_final_contact.copy(), "保持并确认开关位置", "touch", math.degrees(angle)))

        final_tip = self._lever_tip(pivot, lever, angle)
        for index in range(1, counts[5]):
            u = index / max(1, counts[5] - 1)
            target = final_tip - panel_normal * (approach * self._quintic(u))
            q_seed, constraint_scale = self._solve_position(
                target,
                q_seed,
                fingertip_tcp,
                q_posture,
                reference_rpy,
                posture_weight,
                orientation_weight,
                constraint_scale,
            )
            samples.append((q_seed, "指尖离开并向后撤离", "clear" if index > 1 else "touch", math.degrees(angle)))

        waypoints: list[Waypoint] = []
        stages: list[str] = []
        contacts: list[str] = []
        lever_angles: list[float] = []
        for index, (q, stage, contact, lever_angle) in enumerate(samples):
            t = duration * index / max(1, len(samples) - 1)
            waypoints.append(
                Waypoint(
                    index=index,
                    t=float(t),
                    joints=[float(v) for v in q],
                    named_joints=self.robot_model.named_chain_joints(q, chain_id),
                    tcp_pose=self.robot_model.tcp_pose(q, chain_id, fingertip_tcp),
                    link_poses=self.robot_model.link_poses(q, chain_id),
                )
            )
            stages.append(stage)
            contacts.append(contact)
            lever_angles.append(float(lever_angle))

        return SwitchOperationResult(
            waypoints=waypoints,
            stages=stages,
            contact_states=contacts,
            lever_angles_deg=lever_angles,
            pivot_xyz=[float(v) for v in pivot],
            panel_normal=[float(v) for v in panel_normal],
            lever_length=lever,
            approach_distance=approach,
            turn_angle_deg=math.degrees(angle),
            fingertip_tcp=fingertip_tcp,
            constraint_scale=constraint_scale,
            posture_reference_joints=[float(v) for v in q_posture],
            start_tip_xyz=[float(v) for v in initial_tip],
            end_tip_xyz=[float(v) for v in final_tip],
            path_type="arc",
        )

    def _plan_keyframes(
        self,
        pivot: np.ndarray,
        lever: float,
        fingertip_tcp: Pose,
        q_posture: np.ndarray,
        reference_rpy: list[float],
        posture_weight: float,
        orientation_weight: float,
        start_angle: float,
        end_angle: float,
        path_type: str,
        duration: float,
        total_steps: int,
        panel_normal: np.ndarray,
        approach: float,
    ) -> SwitchOperationResult:
        chain_id = "right_arm"
        mode = "linear" if str(path_type).lower() == "linear" else "arc"
        start_tip = self._lever_tip(pivot, lever, start_angle)
        end_tip = self._lever_tip(pivot, lever, end_angle)
        q_seed = q_posture.copy()
        constraint_scale = 1.0
        waypoints: list[Waypoint] = []
        stages: list[str] = []
        contacts: list[str] = []
        lever_angles: list[float] = []

        for index in range(total_steps):
            u = index / max(1, total_steps - 1)
            blend = self._quintic(u)
            theta = start_angle + (end_angle - start_angle) * blend
            if mode == "linear":
                target = start_tip + (end_tip - start_tip) * blend
            else:
                target = self._lever_tip(pivot, lever, theta)
            q_seed, constraint_scale = self._solve_position(
                target,
                q_seed,
                fingertip_tcp,
                q_posture,
                reference_rpy,
                posture_weight,
                orientation_weight,
                constraint_scale,
            )
            stage = "初始动作" if index == 0 else "最终动作" if index == total_steps - 1 else (
                "直线连接" if mode == "linear" else "圆弧连接"
            )
            contact = "touch" if index in {0, total_steps - 1} else "pushing"
            waypoints.append(
                Waypoint(
                    index=index,
                    t=float(duration * u),
                    joints=[float(v) for v in q_seed],
                    named_joints=self.robot_model.named_chain_joints(q_seed, chain_id),
                    tcp_pose=self.robot_model.tcp_pose(q_seed, chain_id, fingertip_tcp),
                    link_poses=self.robot_model.link_poses(q_seed, chain_id),
                )
            )
            stages.append(stage)
            contacts.append(contact)
            lever_angles.append(math.degrees(theta))

        return SwitchOperationResult(
            waypoints=waypoints,
            stages=stages,
            contact_states=contacts,
            lever_angles_deg=lever_angles,
            pivot_xyz=[float(v) for v in pivot],
            panel_normal=[float(v) for v in panel_normal],
            lever_length=lever,
            approach_distance=approach,
            turn_angle_deg=math.degrees(end_angle - start_angle),
            fingertip_tcp=fingertip_tcp,
            constraint_scale=constraint_scale,
            posture_reference_joints=[float(v) for v in q_posture],
            start_tip_xyz=[float(v) for v in start_tip],
            end_tip_xyz=[float(v) for v in end_tip],
            path_type=mode,
        )

    def _solve_position(
        self,
        xyz: np.ndarray,
        seed: np.ndarray,
        tcp_offset: Pose,
        posture_reference: np.ndarray,
        reference_rpy: list[float],
        posture_weight: float,
        orientation_weight: float,
        initial_constraint_scale: float,
    ) -> tuple[np.ndarray, float]:
        chain_id = "right_arm"
        scales = []
        scale = max(0.0, min(1.0, float(initial_constraint_scale)))
        for candidate in (scale, scale * 0.5, scale * 0.25, 0.0):
            if candidate not in scales:
                scales.append(candidate)
        best_result = None
        for candidate in scales:
            result = self.solver.solve(
                IKRequest(
                    chain_id=chain_id,
                    current_joints=[float(v) for v in seed],
                    target_pose=Pose([float(v) for v in xyz], [float(v) for v in reference_rpy]),
                    tcp_offset=tcp_offset,
                    base_link=self.robot_model.base_link(chain_id),
                    end_link=self.robot_model.end_link(chain_id),
                    joint_names=self.robot_model.joint_names(chain_id),
                    seed=[float(v) for v in seed],
                    solver_options={
                        "solve_orientation": False,
                        "max_iterations": 400,
                        "regularization_weight": 0.002,
                        "tolerance_mm": 2.0,
                        "soft_orientation_weight": max(
                            0.0, min(0.3, float(orientation_weight))
                        )
                        * candidate,
                        "posture_weight": max(0.0, min(0.05, float(posture_weight)))
                        * candidate,
                        "posture_reference": [float(v) for v in posture_reference],
                    },
                )
            )
            if best_result is None or result.error_mm < best_result.error_mm:
                best_result = result
            if result.success:
                return np.asarray(result.target_joints, dtype=float), candidate
        raise ValueError(
            f"H2 fingertip switch IK failed at {xyz.tolist()} after relaxing posture constraints: "
            f"{best_result.message}"
        )

    @staticmethod
    def _lever_tip(pivot: np.ndarray, length: float, theta: float) -> np.ndarray:
        # Rotation about panel normal (+X); theta sign selects CW/CCW in the panel view.
        return pivot + np.asarray([0.0, length * math.sin(theta), -length * math.cos(theta)])

    @classmethod
    def _append_joint_segment(
        cls, samples, q0, q1, count: int, stage: str, contact: str, lever_angle: float
    ) -> None:
        for index in range(count):
            u = index / max(1, count - 1)
            blend = cls._quintic(u)
            samples.append((q0 + (q1 - q0) * blend, stage, contact, lever_angle))

    @staticmethod
    def _quintic(u: float) -> float:
        u = max(0.0, min(1.0, float(u)))
        return 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5

    @staticmethod
    def _allocate_counts(total: int, weights: list[float]) -> list[int]:
        counts = [max(3, int(round(total * weight))) for weight in weights]
        while sum(counts) > total:
            index = max(range(len(counts)), key=lambda i: counts[i])
            if counts[index] <= 3:
                break
            counts[index] -= 1
        while sum(counts) < total:
            index = max(range(len(weights)), key=lambda i: weights[i])
            counts[index] += 1
        return counts
