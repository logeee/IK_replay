"""把本项目规划器产出的关节路点变成世界系 TCP 参考轨迹（cuRobo 的替代品）。

同事的中间件里这一步由 cuRobo 完成：``q_nominal(t)`` → FK → ``world_T_tcp(t)``。
这里输入改为 ``ik/`` + ``planners/`` 已经算好的关节路点列表（与 legacy 后端
``_exec_loop`` 收到的完全是同一份数据），做三件事：

1. 时间分配：路点等时距（与 legacy 相同），再按关节速度上限做整体时间缩放
   （移植 ``time_scale_for_pink_bandwidth`` 的 0.9×上限策略）；
2. FK：用 PINK 控制器自带的 pinocchio 模型算 ``root_T_tcp``（TCP frame 与
   QP 完全一致）；
3. 提升到世界系：``world_T_tcp = world_T_root_ref @ root_T_tcp``，其中
   ``world_T_root_ref`` 是**规划所依据的**躯干位姿（取点时刻），之后执行中躯干
   再怎么动，参考轨迹在世界系里都不变——这正是补偿的来源。

另提供恢复重规划用的关节空间五次多项式直连，以及最终保持段的静止参考。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from control.trajectory_reference import WorldTCPReferenceTrajectory

FKFunction = Callable[[np.ndarray], np.ndarray]
"""``q[7] -> root_T_tcp[4,4]``；由 ``PinkArmController.root_T_tcp_actual(q).homogeneous`` 提供。"""

BANDWIDTH_TARGET_FRACTION = 0.90  # 与同事 time_scale_for_pink_bandwidth 一致


@dataclass(frozen=True)
class ReferencePlan:
    """一段可被 :class:`control.tracking_session.TrackingSession` 跟踪的参考。"""

    reference: WorldTCPReferenceTrajectory
    time_s: np.ndarray             # [N]
    q_nominal: np.ndarray          # [N, 7]，PINK 次级姿态任务的参考
    world_T_root_ref: np.ndarray   # 规划所依据的 world_T_root
    time_scale: float
    requested_duration_s: float
    max_qdot_rad_s: float          # 缩放后路径上的最大关节速度
    kind: str                      # "PATH" | "RECOVERY_QUINTIC" | "HOLD"

    def __post_init__(self) -> None:
        times = np.asarray(self.time_s, dtype=np.float64)
        q = np.asarray(self.q_nominal, dtype=np.float64)
        if times.ndim != 1 or q.ndim != 2 or q.shape[0] != times.size:
            raise ValueError("time_s [N] and q_nominal [N,7] must agree")
        if not np.allclose(times, self.reference.time_s):
            raise ValueError("q_nominal timestamps must match the TCP reference")
        object.__setattr__(self, "time_s", times.copy())
        object.__setattr__(self, "q_nominal", q.copy())
        object.__setattr__(
            self, "world_T_root_ref", np.asarray(self.world_T_root_ref, dtype=np.float64).copy()
        )

    @property
    def duration_s(self) -> float:
        return self.reference.duration_s

    @property
    def goal_q(self) -> np.ndarray:
        return self.q_nominal[-1].copy()

    @property
    def goal_world_T_tcp(self) -> np.ndarray:
        return self.reference.world_T_tcp[-1].copy()

    def sample_posture(self, t_s: float) -> np.ndarray:
        """线性插值的 ``q_nominal(t)``（移植 middleware.sample_nominal_posture）。"""
        t = float(np.clip(t_s, self.time_s[0], self.time_s[-1]))
        return np.asarray(
            [np.interp(t, self.time_s, self.q_nominal[:, k]) for k in range(self.q_nominal.shape[1])],
            dtype=np.float64,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "duration_s": self.duration_s,
            "requested_duration_s": self.requested_duration_s,
            "time_scale": self.time_scale,
            "max_qdot_rad_s": self.max_qdot_rad_s,
            "waypoints": int(self.time_s.size),
            "goal_world_tcp_xyz": self.goal_world_T_tcp[:3, 3].tolist(),
        }


def _validate_q_list(q_list: Sequence[np.ndarray]) -> np.ndarray:
    q = np.asarray([np.asarray(item, dtype=np.float64).reshape(-1) for item in q_list])
    if q.ndim != 2 or q.shape[0] < 2:
        raise ValueError("need at least two joint waypoints")
    if not np.all(np.isfinite(q)):
        raise ValueError("joint waypoints contain NaN/inf")
    return q


def _validate_transform(name: str, value: np.ndarray) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{name} must be one finite 4x4 transform")
    return matrix.copy()


def _lift(fk: FKFunction, q_path: np.ndarray, world_T_root_ref: np.ndarray) -> np.ndarray:
    return np.asarray(
        [world_T_root_ref @ np.asarray(fk(q), dtype=np.float64) for q in q_path],
        dtype=np.float64,
    )


def _dedupe_consecutive(q: np.ndarray, atol: float = 1e-9) -> np.ndarray:
    """去掉连续重复路点（0 时距会让参考轨迹的时间戳非严格递增）。"""
    keep = [0]
    for i in range(1, q.shape[0]):
        if not np.allclose(q[i], q[keep[-1]], atol=atol):
            keep.append(i)
    if len(keep) == 1:
        keep.append(q.shape[0] - 1)
        if keep[0] == keep[1]:
            return q[[0, 0]]
    return q[keep]


def build_reference_plan(
    q_list: Sequence[np.ndarray],
    duration_s: float,
    *,
    fk: FKFunction,
    world_T_root_ref: np.ndarray,
    max_qdot_rad_s: float,
    bandwidth_fraction: float = BANDWIDTH_TARGET_FRACTION,
) -> ReferencePlan:
    """规划器路点 → 等时距 + 速度限幅时间缩放 → 世界系 TCP 参考。"""
    q = _dedupe_consecutive(_validate_q_list(q_list))
    world_T_root_ref = _validate_transform("world_T_root_ref", world_T_root_ref)
    requested = float(duration_s)
    if not np.isfinite(requested) or requested <= 0.0:
        raise ValueError("duration_s must be positive and finite")
    if not np.isfinite(max_qdot_rad_s) or max_qdot_rad_s <= 0.0:
        raise ValueError("max_qdot_rad_s must be positive and finite")

    n = q.shape[0]
    time_s = np.linspace(0.0, requested, n)
    dt = requested / (n - 1)
    step_qdot = np.max(np.abs(np.diff(q, axis=0))) / dt if n > 1 else 0.0
    target = float(max_qdot_rad_s) * float(bandwidth_fraction)
    scale = max(1.0, step_qdot / target) if step_qdot > 0.0 else 1.0
    if np.allclose(q[0], q[-1]) and step_qdot == 0.0:
        # 纯保持：参考轨迹仍需两个严格递增的时间戳
        time_s = np.array([0.0, requested])
        q = q[[0, -1]]
        n = 2
    time_s = time_s * scale
    world_T_tcp = _lift(fk, q, world_T_root_ref)
    reference = WorldTCPReferenceTrajectory(time_s, world_T_tcp)
    return ReferencePlan(
        reference=reference,
        time_s=time_s,
        q_nominal=q,
        world_T_root_ref=world_T_root_ref,
        time_scale=float(scale),
        requested_duration_s=requested,
        max_qdot_rad_s=float(step_qdot / scale),
        kind="PATH",
    )


def quintic_profile(s: np.ndarray) -> np.ndarray:
    s = np.clip(np.asarray(s, dtype=np.float64), 0.0, 1.0)
    return 10.0 * s**3 - 15.0 * s**4 + 6.0 * s**5


def build_recovery_plan(
    q_start: np.ndarray,
    q_goal: np.ndarray,
    *,
    fk: FKFunction,
    world_T_root_ref: np.ndarray,
    max_qdot_rad_s: float,
    samples: int = 25,
    min_duration_s: float = 0.5,
    bandwidth_fraction: float = BANDWIDTH_TARGET_FRACTION,
) -> ReferencePlan:
    """故障恢复：从当前关节角到原目标关节角的五次多项式直连。

    恢复时手臂就在原轨迹附近、自由空间里，不需要 RRT；毫秒级即可完成。
    五次多项式的峰值速度 = 1.875 × 平均速度，按此定时长以满足速度上限。
    """
    q0 = np.asarray(q_start, dtype=np.float64).reshape(-1)
    q1 = np.asarray(q_goal, dtype=np.float64).reshape(-1)
    if q0.shape != q1.shape or not np.all(np.isfinite(q0)) or not np.all(np.isfinite(q1)):
        raise ValueError("q_start/q_goal must be finite and of equal shape")
    world_T_root_ref = _validate_transform("world_T_root_ref", world_T_root_ref)
    travel = float(np.max(np.abs(q1 - q0)))
    target = float(max_qdot_rad_s) * float(bandwidth_fraction)
    duration = max(float(min_duration_s), 1.875 * travel / target if travel > 0.0 else 0.0)
    s = np.linspace(0.0, 1.0, max(2, int(samples)))
    q_path = q0[None, :] + quintic_profile(s)[:, None] * (q1 - q0)[None, :]
    time_s = s * duration
    reference = WorldTCPReferenceTrajectory(time_s, _lift(fk, q_path, world_T_root_ref))
    peak_qdot = 1.875 * travel / duration if duration > 0.0 else 0.0
    return ReferencePlan(
        reference=reference,
        time_s=time_s,
        q_nominal=q_path,
        world_T_root_ref=world_T_root_ref,
        time_scale=1.0,
        requested_duration_s=duration,
        max_qdot_rad_s=float(peak_qdot),
        kind="RECOVERY_QUINTIC",
    )


def build_hold_plan(
    world_T_tcp_goal: np.ndarray,
    q_posture: np.ndarray,
    *,
    world_T_root_ref: np.ndarray,
    duration_s: float,
) -> ReferencePlan:
    """静止世界系目标（到位后的保持段）。"""
    goal = _validate_transform("world_T_tcp_goal", world_T_tcp_goal)
    q = np.asarray(q_posture, dtype=np.float64).reshape(1, -1)
    duration = float(duration_s)
    if not np.isfinite(duration) or duration <= 0.0:
        raise ValueError("duration_s must be positive and finite")
    time_s = np.array([0.0, duration])
    reference = WorldTCPReferenceTrajectory(time_s, np.stack([goal, goal]))
    return ReferencePlan(
        reference=reference,
        time_s=time_s,
        q_nominal=np.repeat(q, 2, axis=0),
        world_T_root_ref=_validate_transform("world_T_root_ref", world_T_root_ref),
        time_scale=1.0,
        requested_duration_s=duration,
        max_qdot_rad_s=0.0,
        kind="HOLD",
    )
