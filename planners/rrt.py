"""RRT-Connect 关节空间避障规划（不依赖 MoveIt/OMPL）。

策略：先试直线（大多数动作本来不撞，零额外开销），撞了才起树。
双树互相生长（RRT-Connect），碰撞检查复用 ConfigurableCollisionChecker
（自体胶囊 + 环境体素），找到路径后做随机 shortcut 平滑，再按固定
帧距重采样输出——下游执行链（关节插值 + 限速）完全不变。

采样带引导：一半均匀撒在限位内，一半撒在起终点连线附近（近电柜时
需要的"先收臂再绕"通常离直线不远，引导采样能把规划时间压到亚秒级）。
"""

from __future__ import annotations

import time
from collections import Counter

import numpy as np

from core.robot_model import RobotModel
from core.types import TrajectoryRequest, Waypoint

from .base import BaseTrajectoryPlanner

# 默认参数（planner_options 可覆盖）
STEP_RAD = 0.25          # 树延伸步长（关节空间 L∞）
EDGE_RES_RAD = 0.05      # 边碰撞检查分辨率
TIMEOUT_S = 6.0
MAX_SAMPLES = 4000
SHORTCUT_ITERS = 80
GUIDED_RATIO = 0.5       # 引导采样比例（撒在直线附近）
GUIDED_SIGMA = 0.5       # 引导采样的高斯半径（rad）
MARGIN_M = 0.01          # 规划安全裕量：离障碍不足 1cm 视为无效（免得擦着过）


def rrt_connect_path(model: RobotModel, checker, chain_id: str,
                     q0: np.ndarray, q1: np.ndarray, tcp_offset,
                     options: dict | None = None) -> list[np.ndarray]:
    """返回无碰撞折线路径 [q0, ..., q1]（含端点）。找不到抛 ValueError。

    直线本身无碰撞时直接返回 [q0, q1]（零额外开销）。
    """
    opts = options or {}
    step = float(opts.get("step_rad", STEP_RAD))
    res = float(opts.get("edge_res_rad", EDGE_RES_RAD))
    timeout = float(opts.get("timeout_s", TIMEOUT_S))
    max_samples = int(opts.get("max_samples", MAX_SAMPLES))
    rng = np.random.default_rng(int(opts.get("seed", 0)) or None)

    lower, upper = model.joint_limits(chain_id)
    lower = np.asarray(lower, dtype=float)
    upper = np.asarray(upper, dtype=float)

    # 安全裕量只对【环境障碍（墙）】生效，自碰撞对不受影响。几何上等价于
    # 把墙沿法线平移：+0.01 = 墙向机器人逼近 1cm（更保守），负值 = 墙向后
    # 退让 |m|（允许贴得更近，用户自担）。正裕量找不到路时自动降 0 重试。
    margin = float(opts.get("margin_m", MARGIN_M))

    # 端点豁免（允许碰撞表）：起点/终点都是真机实际到过的姿态（当前实测、
    # 录制路点），物理上显然没撞——模型在这些姿态上报的碰撞（大臂胶囊嵌进
    # 躯干盒、指尖贴面板墙等）一律视为包围体偏保守的误报。把端点上已存在
    # 的碰撞对记入豁免表，整次规划对这些"对"不做否决；端点之外新出现的
    # 碰撞对照查不误。
    # exempt_goal=False（取点规划用）：终点是【算出来的】姿态而非到过的
    # 姿态，它的碰撞是真警报，不豁免——否则会规划出以碰撞收尾的路径。
    endpoints = (q0, q1) if bool(opts.get("exempt_goal", True)) else (q0,)
    allowed: set[frozenset] = set()
    if checker is not None and getattr(checker, "enabled", False):
        for q in endpoints:
            st = checker.check_state([float(v) for v in q], chain_id, tcp_offset)
            for p in st.get("pairs") or []:
                if float(p["distance_m"]) <= 0.0:
                    allowed.add(frozenset((p["a"], p["b"])))
        if allowed:
            names = ", ".join(" ↔ ".join(sorted(k)) for k in allowed)
            print(f"[rrt] 端点为实际到过的姿态，既有碰撞对已豁免: {names}")

    # 规划失败诊断：每次否决采样点时记下是哪一对在拦路，超时后把
    # "拦路榜"写进报错，让用户知道到底是哪里过不去
    blockers: Counter = Counter()

    def valid(q: np.ndarray, m: float = margin) -> bool:
        if checker is None or not getattr(checker, "enabled", False):
            return True
        st = checker.check_state([float(v) for v in q], chain_id, tcp_offset)
        for p in st.get("pairs") or []:
            key = frozenset((p["a"], p["b"]))
            if key in allowed:
                continue
            d = float(p["distance_m"])
            is_env = any(n.startswith("environment") for n in key)
            # 环境（墙）门槛 = m：正值墙逼近、负值墙退让；自碰撞门槛恒为 0
            if d <= (m if is_env else 0.0):
                blockers[" ↔ ".join(sorted(key))] += 1
                return False
        return True

    def edge_free(a: np.ndarray, b: np.ndarray) -> bool:
        n = int(np.ceil(float(np.max(np.abs(b - a))) / res))
        for i in range(1, n):          # 端点本身经豁免表后必然有效
            if not valid(a + (b - a) * (i / n)):
                return False
        return True

    if edge_free(q0, q1):
        return [q0.copy(), q1.copy()]

    # ---- RRT-Connect ----
    # 树节点 = (关节角, 父节点下标)；两棵树轮流交换角色
    tree_a: list[tuple[np.ndarray, int]] = [(q0.copy(), -1)]
    tree_b: list[tuple[np.ndarray, int]] = [(q1.copy(), -1)]

    def nearest(tree, q):
        d = [float(np.max(np.abs(node[0] - q))) for node in tree]
        return int(np.argmin(d))

    def steer(q_from, q_to):
        d = float(np.max(np.abs(q_to - q_from)))
        if d <= step:
            return q_to.copy()
        return q_from + (q_to - q_from) * (step / d)

    def extend(tree, q_target):
        """朝 q_target 反复延伸直到到达或被挡，返回 (到达?, 末节点下标)。"""
        idx = nearest(tree, q_target)
        while True:
            q_new = steer(tree[idx][0], q_target)
            if not (valid(q_new) and edge_free(tree[idx][0], q_new)):
                return False, idx
            tree.append((q_new, idx))
            idx = len(tree) - 1
            if float(np.max(np.abs(q_new - q_target))) < 1e-9:
                return True, idx

    deadline = time.monotonic() + timeout
    path: list[np.ndarray] | None = None
    rounds = 0
    for it in range(max_samples):
        rounds = it + 1
        if time.monotonic() > deadline:
            break
        if rng.random() < GUIDED_RATIO:
            u = rng.random()
            q_rand = q0 + (q1 - q0) * u + rng.normal(0.0, GUIDED_SIGMA, size=q0.shape)
            q_rand = np.clip(q_rand, lower, upper)
        else:
            q_rand = rng.uniform(lower, upper)

        _, ia = extend(tree_a, q_rand)
        reached, ib = extend(tree_b, tree_a[ia][0])
        if reached:
            # 两棵树在 tree_a[ia] 处会师，回溯拼接
            seg_a: list[np.ndarray] = []
            i = ia
            while i >= 0:
                seg_a.append(tree_a[i][0])
                i = tree_a[i][1]
            seg_a.reverse()             # q0 → 会师点
            seg_b: list[np.ndarray] = []
            i = ib
            while i >= 0:
                seg_b.append(tree_b[i][0])
                i = tree_b[i][1]        # 会师点 → q1（tree_b 根是 q1）
            # tree_a/tree_b 每轮互换角色，拼接前确认首尾
            path = seg_a + seg_b[1:] if len(seg_b) and \
                float(np.max(np.abs(seg_a[-1] - seg_b[0]))) < 1e-9 else seg_a + seg_b
            if float(np.max(np.abs(path[0] - q0))) > 1e-6:
                path.reverse()
            break
        tree_a, tree_b = tree_b, tree_a

    if path is None:
        if margin > 0.0:
            # 带 1cm 裕量找不到（缝隙太窄）→ 降为无裕量最后再试一次
            retry = dict(opts)
            retry["margin_m"] = 0.0
            return rrt_connect_path(model, checker, chain_id, q0, q1,
                                    tcp_offset, retry)
        top = "、".join(f"{name}（拦 {cnt} 次）"
                        for name, cnt in blockers.most_common(3))
        hint = f"，主要被挡在: {top}" if top else ""
        raise ValueError(f"RRT 在 {timeout:.0f}s 内没找到无碰撞路径"
                         f"（采样 {rounds} 轮）{hint}"
                         "（可先重新扫障/挪位再试）")

    # ---- shortcut 平滑：随机抽两点尝试直连，能连就剪掉中间的绕行 ----
    for _ in range(int(opts.get("shortcut_iters", SHORTCUT_ITERS))):
        if len(path) <= 2:
            break
        i, j = sorted(rng.integers(0, len(path), size=2))
        if j - i < 2:
            continue
        if edge_free(path[i], path[j]):
            path = path[: i + 1] + path[j:]
    return path


def densify(path: list[np.ndarray], frame_rad: float = 0.04) -> list[np.ndarray]:
    """折线按帧距重采样（L∞ ≤ frame_rad），供执行/落盘。"""
    out = [path[0]]
    for a, b in zip(path, path[1:]):
        n = max(1, int(np.ceil(float(np.max(np.abs(b - a))) / frame_rad)))
        for i in range(1, n + 1):
            out.append(a + (b - a) * (i / n))
    return out


class RRTConnectTrajectoryPlanner(BaseTrajectoryPlanner):
    name = "rrt"

    def __init__(self, robot_model: RobotModel, collision_checker):
        self.robot_model = robot_model
        self.collision_checker = collision_checker

    def plan(self, request: TrajectoryRequest) -> list[Waypoint]:
        model = self.robot_model
        q0 = model.coerce_chain_joints(request.current_joints, request.chain_id)
        q1 = model.coerce_chain_joints(request.target_joints, request.chain_id)
        path = rrt_connect_path(model, self.collision_checker, request.chain_id,
                                q0, q1, request.tcp_offset, request.planner_options)

        steps = max(2, min(1000, int(request.steps)))
        duration = max(0.05, float(request.duration))
        # 按弧长均匀取 steps 帧（直线退化时与 linear 完全一致）
        seg_len = [float(np.max(np.abs(b - a))) for a, b in zip(path, path[1:])]
        cum = np.concatenate([[0.0], np.cumsum(seg_len)])
        total = float(cum[-1]) or 1.0
        waypoints: list[Waypoint] = []
        for idx in range(steps):
            u = idx / (steps - 1)
            s = u * total
            k = int(np.searchsorted(cum, s, side="right") - 1)
            k = min(k, len(path) - 2)
            local = (s - cum[k]) / (seg_len[k] or 1.0)
            q = path[k] + (path[k + 1] - path[k]) * local
            waypoints.append(Waypoint(
                index=idx,
                t=float(duration * u),
                joints=[float(v) for v in q],
                named_joints=model.named_chain_joints(q, request.chain_id),
                tcp_pose=model.tcp_pose(q, request.chain_id, request.tcp_offset),
                link_poses=model.link_poses(q, request.chain_id),
            ))
        return waypoints
