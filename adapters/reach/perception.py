"""相机感知：视频流、取点与表面平面拟合、深度扫描环境障碍。"""

from __future__ import annotations

import math
import time

import numpy as np
from fastapi.responses import JSONResponse, StreamingResponse

from .state import _read_joints, _read_torso, router, state


@router.get("/stream")
def reach_stream():
    def gen():
        while True:
            data = state.camera.get_jpeg()
            if data is None:
                time.sleep(0.2)
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                   + data + b"\r\n")
            time.sleep(0.05)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-cache"})


# --------------- 取点 ---------------


@router.post("/pick")
def reach_pick(body: dict):
    """Body: {"u": int, "v": int, "approach_offset_m": float?}

    approach_offset_m：沿被点表面的法线、朝机器人方向后退的距离
    （即垂直于障碍物平面的间隙），默认 0.015；负值 = 指令位置压入表面，
    接触后位置误差消不掉，电机持续出力（掰开关用）。
    平面拟合失败时退化为沿相机视线后退。
    """
    if not state.enabled:
        return JSONResponse({"ok": False, "error": "reach 未启用"}, status_code=409)
    try:
        u, v = int(body["u"]), int(body["v"])
        offset = float(body.get("approach_offset_m", 0.015))
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "需要整数 u、v"}, status_code=400)

    result = state.camera.pick(u, v)
    if not result.get("ok"):
        return JSONResponse(result, status_code=502)

    p_cam = np.asarray(result["p_camera"], dtype=float)
    dist = float(np.linalg.norm(p_cam))
    if dist <= offset + 0.05:
        return JSONResponse(
            {"ok": False, "error": f"目标离相机太近（{dist:.2f} m），无法应用接近偏移"},
            status_code=400)

    # 拟合目标表面平面（接近偏移沿它的法线退；横移的"左"方向也以它定义）
    state.plane = _fit_surface_plane(p_cam)
    if state.plane is not None:
        # 沿表面法线（指向机器人一侧）退 offset：垂直于障碍物平面的真实间隙，
        # 不受视线斜射角影响，也没有沿面的横向偏移
        n_cam = np.asarray(state.plane["normal_cam"], dtype=float)
        p_cam_goal = p_cam + offset * n_cam
        offset_mode = "plane_normal"
    else:
        p_cam_goal = p_cam * (1.0 - offset / dist)  # 兜底：沿视线退
        offset_mode = "camera_ray"

    def to_frame(T, p):
        return (T[:3, :3] @ p + T[:3, 3]).tolist()

    p_root_surface = to_frame(state.T_cam2root, p_cam)
    # 目标附近的环境障碍豁免：指尖要贴近表面，目标周围一小块不算障碍
    if state.collision_checker is not None:
        state.collision_checker.set_environment_exclusions(
            [(p_root_surface, state.target_exclusion_m)])

    # 记下此刻的躯干姿态：目标从这一刻起被"冻结"在 torso 系里，
    # 之后躯干只要转了，同一坐标就不再对准那个开关（执行完会给出偏差）
    state.pick_target_torso = to_frame(state.T_cam2torso, p_cam_goal)
    state.pick_target_root = to_frame(state.T_cam2root, p_cam_goal)
    state.pick_pixel = [u, v]
    state.pick_torso = _read_torso()
    state.torso_diag = None

    return {
        "ok": True,
        "pixel": [u, v],
        "depth_mm": result["depth_mm"],
        "p_camera": p_cam.tolist(),
        "approach_offset_m": offset,
        "offset_mode": offset_mode,
        "p_torso_surface": to_frame(state.T_cam2torso, p_cam),
        "p_torso": state.pick_target_torso,
        "p_root": to_frame(state.T_cam2root, p_cam_goal),
        "p_root_surface": p_root_surface,
        "plane": state.plane,
    }


@router.post("/confirm_pointcloud_pick")
def confirm_pointcloud_pick(body: dict):
    """确认冻结 RGB-D 中选出的三维点，并生成与 /pick 相同的规划目标。

    Body:
      p_camera_surface: 微调后的相机系表面点
      pixel: 原始 RGB 像素（可选）
      plane: 从同一冻结深度拟合出的相机系平面（可选）
      approach_offset_m: 沿平面法线向机器人侧后退，语义与 /pick 相同
    """
    if not state.enabled or state.T_cam2root is None or state.T_cam2torso is None:
        return JSONResponse(
            {"ok": False, "error": "手眼标定尚未就绪"}, status_code=409
        )
    try:
        p_cam = np.asarray(body["p_camera_surface"], dtype=float).reshape(3)
        offset = float(body.get("approach_offset_m", 0.0))
        pixel_value = body.get("pixel")
        pixel = (
            [int(pixel_value[0]), int(pixel_value[1])]
            if pixel_value is not None
            else [-1, -1]
        )
        adjustment = np.asarray(
            body.get("adjustment_camera_m", [0.0, 0.0, 0.0]),
            dtype=float,
        ).reshape(3)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"三维目标参数非法: {exc}"}, status_code=400
        )
    if (
        not np.isfinite(p_cam).all()
        or not np.isfinite(adjustment).all()
        or not math.isfinite(offset)
        or p_cam[2] <= 0.05
    ):
        return JSONResponse(
            {"ok": False, "error": "三维目标包含非有限值或深度过小"},
            status_code=400,
        )
    dist = float(np.linalg.norm(p_cam))
    if dist <= abs(offset) + 0.05 or dist > 5.0:
        return JSONResponse(
            {"ok": False, "error": f"三维目标距离异常（{dist:.2f} m）"},
            status_code=400,
        )

    plane = _pointcloud_plane(body.get("plane"), p_cam)
    if plane is not None:
        normal_cam = np.asarray(plane["normal_cam"], dtype=float)
        p_cam_goal = p_cam + offset * normal_cam
        offset_mode = "plane_normal"
    else:
        p_cam_goal = p_cam * (1.0 - offset / dist)
        offset_mode = "camera_ray"
    if p_cam_goal[2] <= 0.05:
        return JSONResponse(
            {"ok": False, "error": "接近偏移后的目标深度过小"},
            status_code=400,
        )

    def to_frame(T, point):
        return (T[:3, :3] @ point + T[:3, 3]).tolist()

    p_root_surface = to_frame(state.T_cam2root, p_cam)
    if state.collision_checker is not None:
        state.collision_checker.set_environment_exclusions(
            [(p_root_surface, state.target_exclusion_m)]
        )
    state.plane = plane
    state.pick_target_torso = to_frame(state.T_cam2torso, p_cam_goal)
    state.pick_target_root = to_frame(state.T_cam2root, p_cam_goal)
    state.pick_pixel = pixel
    state.pick_torso = _read_torso()
    state.torso_diag = None

    return {
        "ok": True,
        "selection_mode": "frozen_rgbd_pointcloud",
        "source_frame_id": body.get("source_frame_id"),
        "pixel": pixel,
        "depth_mm": float(p_cam[2] * 1000.0),
        "p_camera": p_cam.tolist(),
        "adjustment_camera_m": adjustment.tolist(),
        "approach_offset_m": offset,
        "offset_mode": offset_mode,
        "p_torso_surface": to_frame(state.T_cam2torso, p_cam),
        "p_torso": state.pick_target_torso,
        "p_root": state.pick_target_root,
        "p_root_surface": p_root_surface,
        "plane": plane,
    }


def _pointcloud_plane(value: dict | None, p_cam: np.ndarray) -> dict | None:
    """把冻结深度拟合的相机系平面转换成 Reach 使用的根系平面。"""
    if not value:
        return None
    try:
        center_cam = np.asarray(
            value.get("center_cam", p_cam), dtype=float
        ).reshape(3)
        normal_cam = np.asarray(value["normal_cam"], dtype=float).reshape(3)
        norm = float(np.linalg.norm(normal_cam))
        if (
            not np.isfinite(center_cam).all()
            or not np.isfinite(normal_cam).all()
            or norm < 1e-6
        ):
            return None
        normal_cam /= norm
        if float(np.dot(normal_cam, -center_cam)) < 0:
            normal_cam = -normal_cam
        rotation = state.T_cam2root[:3, :3]
        normal_root = rotation @ normal_cam
        center_root = rotation @ center_cam + state.T_cam2root[:3, 3]
        facing = -normal_root
        left_root = np.cross(np.array([0.0, 0.0, 1.0]), facing)
        left_norm = float(np.linalg.norm(left_root))
        if left_norm < 1e-3:
            return None
        left_root /= left_norm
        left_root -= float(np.dot(left_root, normal_root)) * normal_root
        left_root /= float(np.linalg.norm(left_root))
        return {
            "center_root": center_root.tolist(),
            "normal_root": normal_root.tolist(),
            "normal_cam": normal_cam.tolist(),
            "left_root": left_root.tolist(),
            "rms_mm": float(value.get("rms_mm", 0.0)),
            "points": int(value.get("points", 0)),
            "radius_m": float(value.get("radius_m", 0.12)),
            "source": "frozen_rgbd",
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _fit_surface_plane(p_cam_surface: np.ndarray, radius: float = 0.12) -> dict | None:
    """在被点表面点周围拟合平面（SVD 最小二乘），返回根系下的
    法线（指向机器人）、"左"方向（面向平面时的左，嵌在平面内）等。
    拟合失败（点太少/平面水平）返回 None。"""
    snap = state.camera.depth_snapshot()
    if snap is None:
        return None
    depth_mm, (fx, fy, cx, cy) = snap
    h, w = depth_mm.shape
    stride = max(1, int(round(max(h, w) / 320)))
    d = depth_mm[::stride, ::stride].astype(float) / 1000.0
    vs, us = np.mgrid[0:h:stride, 0:w:stride]
    valid = (d > 0.15) & (d < 3.0)
    pts = np.stack([(us[valid] - cx) * d[valid] / fx,
                    (vs[valid] - cy) * d[valid] / fy,
                    d[valid]], axis=1)
    near = pts[np.linalg.norm(pts - p_cam_surface, axis=1) < radius]
    if len(near) < 50:
        return None

    center = near.mean(axis=0)
    q = near - center
    _, _, vt = np.linalg.svd(q, full_matrices=False)
    n = vt[2]
    rms = float(np.sqrt(np.mean((q @ n) ** 2)))
    if float(np.dot(n, -center)) < 0:
        n = -n  # 法线指向相机（即机器人一侧）

    R = state.T_cam2root[:3, :3]
    n_root = R @ n
    center_root = R @ center + state.T_cam2root[:3, 3]
    facing = -n_root                      # 机器人 → 平面
    up = np.array([0.0, 0.0, 1.0])
    left = np.cross(up, facing)           # 面向平面时的左手方向
    norm = float(np.linalg.norm(left))
    if norm < 1e-3:
        return None                       # 平面接近水平，"左"无定义
    left /= norm
    left -= float(np.dot(left, n_root)) * n_root   # 嵌入平面内
    left /= float(np.linalg.norm(left))
    return {
        "center_root": center_root.tolist(),
        "normal_root": n_root.tolist(),
        "normal_cam": n.tolist(),   # 相机系法线（指向机器人一侧），接近偏移沿它退
        "left_root": left.tolist(),
        "rms_mm": rms * 1000.0,
        "points": int(len(near)),
        "radius_m": radius,
    }


def _fit_view_plane(dmin: float, dmax: float) -> dict:
    """整幅深度图（限定深度范围）拟合平面，返回垂直度指标。失败时 ok=False。"""
    snap = state.camera.depth_snapshot()
    if snap is None:
        return {"ok": False, "error": "拿不到深度帧"}
    depth_mm, (fx, fy, cx, cy) = snap
    h, w = depth_mm.shape
    stride = max(1, int(round(max(h, w) / 240)))
    d = depth_mm[::stride, ::stride].astype(float) / 1000.0
    vs, us = np.mgrid[0:h:stride, 0:w:stride]
    measured = d > 0.05                       # 有回波的像素（0 = 无效）
    valid = measured & (d > dmin) & (d < dmax)
    n_meas = int(measured.sum())
    if valid.sum() < 200:
        return {"ok": False, "error": f"深度在 {dmin:.2f}~{dmax:.2f} m 内的点太少"
                                      f"（{int(valid.sum())} 个），请靠近/对准柜面"}
    pts = np.stack([(us[valid] - cx) * d[valid] / fx,
                    (vs[valid] - cy) * d[valid] / fy,
                    d[valid]], axis=1)

    def fit(p):
        c = p.mean(axis=0)
        q = p - c
        _, _, vt = np.linalg.svd(q, full_matrices=False)
        n = vt[2]
        return c, n, float(np.sqrt(np.mean((q @ n) ** 2)))

    # 两遍拟合：第一遍全量，第二遍剔除 3σ 残差外点（柜门把手、边缘飞点）
    center, n, rms = fit(pts)
    resid = np.abs((pts - center) @ n)
    inlier = resid < max(0.008, 3.0 * rms)
    if inlier.sum() >= 200:
        center, n, rms = fit(pts[inlier])
    if float(np.dot(n, -center)) < 0:
        n = -n                                # 法线指向相机一侧

    # 相机系：x 右、y 下、z 前。垂直时 n = (0,0,-1)
    yaw_err = math.degrees(math.atan2(float(n[0]), float(-n[2])))
    pitch_err = math.degrees(math.atan2(float(n[1]), float(-n[2])))
    tilt = math.degrees(math.acos(float(np.clip(-n[2], -1.0, 1.0))))
    n_root = (None if state.T_cam2root is None
              else (state.T_cam2root[:3, :3] @ n).tolist())

    return {
        "ok": True,
        "yaw_err_deg": yaw_err,
        "pitch_err_deg": pitch_err,
        "tilt_deg": tilt,
        "distance_m": float(abs(np.dot(n, center))),
        "normal_cam": n.tolist(),
        "normal_root": n_root,
        "rms_mm": rms * 1000.0,
        "points_used": int(inlier.sum()),
        "in_range_ratio": float(valid.sum()) / max(1, n_meas),
        "dmin": dmin,
        "dmax": dmax,
    }


# --------------- 环境障碍物（深度相机扫描） ---------------


def _self_filter(points_root: np.ndarray, margin: float) -> np.ndarray:
    """剔除属于机器人自身（手臂/躯干/头）的点，避免自己把自己当障碍。"""
    checker = state.collision_checker
    try:
        q = [float(v) for v in _read_joints()]
        joints = dict(zip(state.joint_names, q))
    except Exception:
        joints = {}
    from core.types import Pose

    transforms = state.robot_model.forward_kinematics(joints)
    # 用标定的 p_tool 当 TCP，让 hand 胶囊/tcp 球覆盖到真实指尖，过滤更完整
    tcp_pose = state.robot_model.tcp_pose(
        joints, state.chain_id, Pose(xyz=list(state.p_tool)))
    shapes = checker._build_shapes(transforms, state.chain_id, tcp_pose)

    keep = np.ones(len(points_root), dtype=bool)
    for shape in shapes:
        d = shape.data
        if shape.kind == "sphere":
            dist = np.linalg.norm(points_root - np.asarray(d["center"]), axis=1) - d["radius"]
        elif shape.kind == "capsule":
            a, b = np.asarray(d["a"]), np.asarray(d["b"])
            ab = b - a
            denom = float(np.dot(ab, ab))
            t = (np.clip((points_root - a) @ ab / denom, 0.0, 1.0)
                 if denom > 1e-12 else np.zeros(len(points_root)))
            dist = np.linalg.norm(points_root - (a + t[:, None] * ab), axis=1) - d["radius"]
        elif shape.kind == "box":
            R = np.asarray(d["rotation"])
            local = (points_root - np.asarray(d["center"])) @ R
            outside = np.maximum(np.abs(local) - np.asarray(d["half_extents"]), 0.0)
            dist = np.linalg.norm(outside, axis=1)
        else:
            continue
        keep &= dist > margin
    return points_root[keep]


@router.post("/scan_obstacles")
def reach_scan_obstacles(body: dict | None = None):
    """扫一帧深度图 → 躯干系体素障碍物，注入碰撞检查。

    Body(可选): {"voxel_m": 0.05, "max_range_m": 1.5, "self_margin_m": 0.10}
    建议扫描时把手臂放低（移出电柜方向视野），残留的手臂点会被自体过滤兜底。
    """
    if state.collision_checker is None:
        return JSONResponse({"ok": False, "error": "碰撞检查器未注入"}, status_code=409)
    body = body or {}
    voxel = float(body.get("voxel_m", 0.05))
    max_range = float(body.get("max_range_m", 1.5))
    self_margin = float(body.get("self_margin_m", 0.10))

    snap = state.camera.depth_snapshot()
    if snap is None:
        return JSONResponse({"ok": False, "error": "还没有深度帧"}, status_code=502)
    depth_mm, (fx, fy, cx, cy) = snap
    h, w = depth_mm.shape

    stride = max(1, int(round(max(h, w) / 240)))  # 采样到 ~240 列，够 5cm 体素用
    d = depth_mm[::stride, ::stride].astype(float) / 1000.0
    vs, us = np.mgrid[0:h:stride, 0:w:stride]
    valid = (d > 0.15) & (d < max_range)
    z = d[valid]
    u = us[valid].astype(float)
    v = vs[valid].astype(float)
    pts_cam = np.stack([(u - cx) * z / fx, (v - cy) * z / fy, z], axis=1)

    pts_root = pts_cam @ state.T_cam2root[:3, :3].T + state.T_cam2root[:3, 3]
    pts_root = _self_filter(pts_root, self_margin)
    if not len(pts_root):
        return JSONResponse({"ok": False, "error": "过滤后没有剩余点（全是自身/超范围？）"},
                            status_code=400)

    # 体素化去重
    idx = np.unique(np.floor(pts_root / voxel).astype(np.int64), axis=0)
    centers = (idx + 0.5) * voxel

    # 拟合竖直墙面并向下补全：相机只能看到柜面上半部分，视野之下没有
    # 体素，手在低处照样会撞。柜面理论上竖直 → 在水平投影上 RANSAC 拟合
    # 直线（对旁边的杂物鲁棒），从地面补到扫描顶部，一起进碰撞环境。
    wall, wall_plane = _fit_wall_voxels(pts_root, voxel, _ground_z())

    state.obstacles = centers
    state.wall = wall
    state.wall_plane = wall_plane
    state.obstacle_voxel = voxel
    if wall_plane is not None:
        # 拟合成功：碰撞环境只用这面【解析平面】（半空间，零膨胀）。
        # 体素球表示会把几毫米厚的柜面加厚成 ~7.5cm 的球层（球半径
        # 0.75*voxel + 相邻球重叠），近柜规划基本无路可走；平面距离
        # 精确到毫米。体素 centers/wall 仅留作前端可视化。
        state.collision_checker.set_environment([], radius=voxel * 0.75)
        cz = wall_plane["center"][2]
        ext = 0.10   # 柜面可能比相机视野宽：矩形边界各外扩 10cm
        state.collision_checker.set_environment_planes([{
            "point": wall_plane["center"],
            "normal": wall_plane["normal"],
            "dir": wall_plane["dir"],
            "u_range": [wall_plane["u_range"][0] - ext,
                        wall_plane["u_range"][1] + ext],
            "v_range": [wall_plane["z_range"][0] - cz,
                        wall_plane["z_range"][1] - cz + ext],
        }])
    else:
        # 兜底：没拟合出主导墙面（没对着柜子/杂物太多）退回体素球
        state.collision_checker.set_environment(centers, radius=voxel * 0.75)
        state.collision_checker.set_environment_planes([])
    return {"ok": True, "count": int(len(centers)), "voxel_m": voxel,
            "wall_count": 0 if wall is None else int(len(wall)),
            "plane_only": wall_plane is not None,
            "raw_points": int(len(pts_root))}


def _ground_z() -> float:
    """地面在根系（骨盆）下方的高度：全零姿态最低连杆 z 再留 5cm 余量。"""
    try:
        transforms = state.robot_model.forward_kinematics({})
        return float(min(T[2, 3] for T in transforms.values())) - 0.05
    except Exception:
        return -0.9   # H2 骨盆离地约 0.8m 的兜底值


def _fit_wall_voxels(pts_root: np.ndarray, voxel: float, z_floor: float
                     ) -> tuple[np.ndarray | None, dict | None]:
    """从扫描点拟合竖直墙面，返回 (补全体素中心, 平面几何)；失败 (None, None)。

    做法：点云投影到水平面（x,y），RANSAC 拟合直线（= 竖直平面的迹线），
    内点的横向范围决定墙宽，z 从地面（z_floor，根系为骨盆、地面在负半轴）
    一直铺到扫描最高点。
    """
    if len(pts_root) < 80:
        return None, None
    xy = pts_root[:, :2]
    rng = np.random.default_rng(0)
    best_inliers = None
    n = len(xy)
    for _ in range(200):
        i, j = rng.integers(0, n, size=2)
        d = xy[j] - xy[i]
        norm = float(np.hypot(*d))
        if norm < 0.05:
            continue
        # 直线法向（水平面内）
        nvec = np.array([-d[1], d[0]]) / norm
        dist = np.abs((xy - xy[i]) @ nvec)
        inliers = dist < 0.03
        if best_inliers is None or inliers.sum() > best_inliers.sum():
            best_inliers = inliers
    if best_inliers is None or best_inliers.sum() < max(60, 0.3 * n):
        return None, None   # 没有占主导的竖直面（可能没对着柜子）

    pin = pts_root[best_inliers]
    # 内点最小二乘精修：直线方向 = xy 协方差主轴
    center_xy = pin[:, :2].mean(axis=0)
    q = pin[:, :2] - center_xy
    _, _, vt = np.linalg.svd(q, full_matrices=False)
    dir_xy = vt[0] / np.linalg.norm(vt[0])
    n_xy = np.array([-dir_xy[1], dir_xy[0]])   # 水平法向
    # 法线指向机器人一侧（根原点在法线负侧 → 翻号）
    if float(np.dot(n_xy, -center_xy)) < 0:
        n_xy = -n_xy

    t = q @ dir_xy                       # 沿墙横向坐标
    t_lo, t_hi = float(t.min()), float(t.max())
    z_top = float(pin[:, 2].max())
    if z_top <= z_floor + 0.1:
        return None, None

    ts = np.arange(t_lo, t_hi + voxel / 2, voxel)
    zs = np.arange(z_floor + voxel / 2, z_top, voxel)
    if not len(ts) or not len(zs):
        return None, None
    grid_t, grid_z = np.meshgrid(ts, zs)
    wall = np.empty((grid_t.size, 3))
    wall[:, 0] = center_xy[0] + grid_t.ravel() * dir_xy[0]
    wall[:, 1] = center_xy[1] + grid_t.ravel() * dir_xy[1]
    wall[:, 2] = grid_z.ravel()
    plane = {
        "center": [float(center_xy[0]), float(center_xy[1]),
                   float((z_top + z_floor) / 2)],
        "normal": [float(n_xy[0]), float(n_xy[1]), 0.0],
        "dir": [float(dir_xy[0]), float(dir_xy[1]), 0.0],
        "width_m": float(t_hi - t_lo),
        "height_m": float(z_top - z_floor),
        # 碰撞用的矩形边界：u 沿 dir（相对 center），z 为绝对高度
        "u_range": [float(t_lo), float(t_hi)],
        "z_range": [float(z_floor), float(z_top)],
    }
    return wall, plane


@router.post("/clear_obstacles")
def reach_clear_obstacles():
    if state.collision_checker is not None:
        state.collision_checker.clear_environment()
    state.obstacles = None
    state.wall = None
    state.wall_plane = None
    return {"ok": True, "count": 0}


@router.get("/obstacles")
def reach_obstacles():
    return {
        "count": 0 if state.obstacles is None else int(len(state.obstacles)),
        "voxel_m": state.obstacle_voxel,
        "centers": [] if state.obstacles is None else state.obstacles.tolist(),
        "wall_count": 0 if state.wall is None else int(len(state.wall)),
        "wall_centers": [] if state.wall is None else state.wall.tolist(),
        "wall_plane": state.wall_plane,
    }
