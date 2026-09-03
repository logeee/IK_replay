"""YOLO-mask cabinet panel fitting over an in-memory :class:`PointCloud`."""

from __future__ import annotations

import logging
import sys
from typing import Any, NoReturn

import cv2
import numpy as np

from .cabinet_wall_frame import fit_dominant_plane
from .pointcloud_core import PointCloud, detection_pixel_mask

# 独立 handler：无论宿主进程日志配置如何，都保证打到 stdout（服务日志文件）
logger = logging.getLogger("panel_fit")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [面板拟合] %(message)s", "%H:%M:%S")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def fit_yolo_panel_rectangle(
    points_xyz: np.ndarray,
    *,
    threshold_m: float = 0.004,
    min_points: int = 100,
    min_inlier_ratio: float = 0.35,
    grid_cell_m: float = 0.002,
    seed: int = 0,
    preferred_axes_camera: tuple[Any, Any] | None = None,
) -> dict[str, Any]:
    """Fit a local panel plane and a supported orthogonal L-shaped outline.

    When ``preferred_axes_camera`` provides wall X and Z, the rectangle
    orientation comes from the wall frame rather than boundary Hough voting.
    """
    dbg: dict[str, Any] = {}

    def fail(message: str) -> NoReturn:
        """失败即把逐阶段调试数据整体打进服务日志，便于直接定位。"""
        lines = [f"拟合失败：{message}"]
        lines += [f"    {key} = {value}" for key, value in dbg.items()]
        logger.warning("\n".join(lines))
        error = ValueError(message)
        error.debug = dict(dbg)  # type: ignore[attr-defined]
        raise error

    points = np.asarray(points_xyz, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("YOLO Mask 点云必须是 N×3 数组")
    points = points[np.isfinite(points).all(axis=1)]
    dbg["mask内有效点"] = int(points.shape[0])
    if points.shape[0] < min_points:
        fail(f"YOLO Mask 内有效点不足：{points.shape[0]} < {min_points}")
    if not 0.001 <= threshold_m <= 0.02:
        raise ValueError("YOLO 面板平面阈值必须在 1–20 mm 之间")

    coarse = fit_dominant_plane(
        points,
        threshold_m=threshold_m,
        iterations=160,
        min_inlier_ratio=0.20,
        seed=seed,
    )
    coarse_center = np.asarray(
        coarse["center_camera_m"], dtype=np.float64
    )
    coarse_normal = np.asarray(coarse["normal_camera"], dtype=np.float64)
    signed_distances = (points - coarse_center) @ coarse_normal
    plane_mask = np.abs(signed_distances) <= threshold_m
    dbg["平面粗拟合"] = (
        f"内点 {int(plane_mask.sum())}/{points.shape[0]}"
        f" 比例 {float(plane_mask.mean()):.1%}（阈值 {threshold_m*1000:.0f}mm）"
    )
    if int(plane_mask.sum()) < min_points:
        fail("YOLO Mask 内局部平面内点不足")
    inlier_ratio = float(plane_mask.mean())
    if inlier_ratio < min_inlier_ratio:
        fail(f"YOLO 面板平面内点比例仅 {inlier_ratio:.1%}")

    inliers = points[plane_mask]
    center = inliers.mean(axis=0)
    _, _, vh = np.linalg.svd(inliers - center, full_matrices=False)
    normal = vh[-1] / np.linalg.norm(vh[-1])
    if float(normal @ center) < 0:
        normal = -normal
    final_distances = (points - center) @ normal
    plane_mask = np.abs(final_distances) <= threshold_m
    inliers = points[plane_mask]
    if inliers.shape[0] < min_points:
        fail("局部平面精化后内点不足")
    center = inliers.mean(axis=0)
    _, _, vh = np.linalg.svd(inliers - center, full_matrices=False)
    normal = vh[-1] / np.linalg.norm(vh[-1])
    if float(normal @ center) < 0:
        normal = -normal
    residuals = (inliers - center) @ normal

    basis_x = np.array([1.0, 0.0, 0.0])
    basis_x -= float(basis_x @ normal) * normal
    if float(np.linalg.norm(basis_x)) < 1e-6:
        basis_x = np.array([0.0, -1.0, 0.0])
        basis_x -= float(basis_x @ normal) * normal
    basis_x /= np.linalg.norm(basis_x)
    basis_z = np.cross(normal, basis_x)
    basis_z /= np.linalg.norm(basis_z)
    planar_basis = np.column_stack((basis_x, basis_z))
    planar = (inliers - center) @ planar_basis

    low = np.quantile(planar, 0.005, axis=0)
    high = np.quantile(planar, 0.995, axis=0)
    extent = high - low
    dbg["平面精化"] = (
        f"内点 {inliers.shape[0]} rms {float(np.sqrt(np.mean(residuals**2)))*1000:.1f}mm"
        f" 范围 {extent[0]*1000:.0f}×{extent[1]*1000:.0f}mm"
    )
    if float(np.min(extent)) < 0.02:
        fail("YOLO 面板平面范围太小，无法识别长短边")
    cell_size = max(grid_cell_m, float(np.max(extent)) / 900.0)
    grid_shape = np.ceil(extent / cell_size).astype(np.int64) + 5
    grid_shape = np.maximum(grid_shape, 8)
    grid = np.zeros((int(grid_shape[1]), int(grid_shape[0])), np.uint8)
    cells = np.rint((planar - low) / cell_size).astype(np.int64) + 2
    valid_cells = (
        (cells[:, 0] >= 0)
        & (cells[:, 0] < grid.shape[1])
        & (cells[:, 1] >= 0)
        & (cells[:, 1] < grid.shape[0])
    )
    grid[cells[valid_cells, 1], cells[valid_cells, 0]] = 1
    # A fixed 5x5 close kernel visibly reshapes tiny panels.
    close_size = 3 if int(min(grid.shape)) < 64 else 5
    close_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (close_size, close_size)
    )
    grid = cv2.morphologyEx(grid, cv2.MORPH_CLOSE, close_kernel)
    component_count, component_labels, stats, _ = (
        cv2.connectedComponentsWithStats(grid, connectivity=8)
    )
    dbg["栅格"] = (
        f"cell {cell_size*1000:.1f}mm 尺寸 {grid.shape[1]}×{grid.shape[0]}"
        f" 连通块 {component_count - 1} 个"
    )
    if component_count <= 1:
        fail("YOLO 面板平面没有稳定连通区域")
    largest_label = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    component = (component_labels == largest_label).astype(np.uint8)
    # Coplanar mask bleed must not drag the rectangle extent off the panel.
    in_component = np.zeros(planar.shape[0], dtype=bool)
    in_component[valid_cells] = (
        component_labels[cells[valid_cells, 1], cells[valid_cells, 0]]
        == largest_label
    )
    component_planar = planar[in_component]
    dbg["主连通块点数"] = int(component_planar.shape[0])
    if component_planar.shape[0] < min_points:
        fail("面板主连通区域内点不足")
    contours, _ = cv2.findContours(
        component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    if not contours:
        fail("YOLO 面板平面无法提取外轮廓")
    contour = max(contours, key=cv2.contourArea)
    boundary = np.zeros_like(component)
    cv2.drawContours(boundary, [contour], -1, 255, 1)
    min_line_pixels = max(8, int(min(grid.shape) * 0.10))
    max_gap_m = min(0.012, float(np.max(extent)) * 0.05)
    hough = cv2.HoughLinesP(
        boundary,
        rho=1,
        theta=np.pi / 360.0,
        threshold=max(8, min_line_pixels // 2),
        minLineLength=min_line_pixels,
        maxLineGap=max(2, int(round(max_gap_m / cell_size))),
    )
    dbg["霍夫参数"] = (
        f"minLineLength {min_line_pixels}px(={min_line_pixels*cell_size*1000:.0f}mm)"
        f" 检出 {0 if hough is None else len(hough)} 条"
    )
    if hough is None or len(hough) < 2:
        fail("面板外轮廓没有足够的直线边支持")

    segments: list[dict[str, Any]] = []
    total_segment_length = 0.0
    for raw_line in np.asarray(hough).reshape(-1, 4):
        x1, y1, x2, y2 = [float(value) for value in raw_line]
        start_2d = low + (np.array([x1, y1]) - 2.0) * cell_size
        end_2d = low + (np.array([x2, y2]) - 2.0) * cell_size
        delta = end_2d - start_2d
        length = float(np.linalg.norm(delta))
        if length < 0.012:
            continue
        direction = delta / length
        total_segment_length += length
        segments.append(
            {
                "start": start_2d,
                "end": end_2d,
                "midpoint": (start_2d + end_2d) * 0.5,
                "direction": direction,
                "length": length,
            }
        )
    dbg["直线段"] = (
        f"{len(segments)} 段 合计 {total_segment_length*1000:.0f}mm"
        "（要求 ≥2 段且合计 ≥30mm）"
    )
    if len(segments) < 2 or total_segment_length < 0.03:
        fail("面板边界直线长度不足")
    direction_tolerance_cos = float(np.cos(np.radians(15.0)))

    def aligned_segment_support(
        axis_a_candidate: np.ndarray, axis_b_candidate: np.ndarray
    ) -> float:
        support = 0.0
        for segment in segments:
            direction = np.asarray(segment["direction"])
            alignment = max(
                abs(float(direction @ axis_a_candidate)),
                abs(float(direction @ axis_b_candidate)),
            )
            if alignment >= direction_tolerance_cos:
                support += float(segment["length"])
        return support

    # Prefer cabinet axes; fall back when they project poorly locally.
    axis_a: np.ndarray | None = None
    axis_b: np.ndarray | None = None
    orientation_source = "boundary-hough"
    if preferred_axes_camera is not None:
        projected_axes: list[np.ndarray] = []
        for axis_camera in preferred_axes_camera:
            axis_3d = np.asarray(axis_camera, dtype=np.float64)
            if (
                axis_3d.shape != (3,)
                or not np.isfinite(axis_3d).all()
                or float(np.linalg.norm(axis_3d)) < 1e-6
            ):
                projected_axes = []
                break
            axis_3d = axis_3d / np.linalg.norm(axis_3d)
            in_plane = axis_3d - float(axis_3d @ normal) * normal
            in_plane_length = float(np.linalg.norm(in_plane))
            if in_plane_length < 0.7:
                dbg["墙轴投影"] = (
                    f"投影长度 {in_plane_length:.2f} < 0.7，"
                    "墙轴弃用 → 回退霍夫定向"
                )
                projected_axes = []
                break
            projected_axes.append(
                planar_basis.T @ (in_plane / in_plane_length)
            )
        if len(projected_axes) == 2:
            axis_a = projected_axes[0] / np.linalg.norm(projected_axes[0])
            perpendicular = np.array([-axis_a[1], axis_a[0]])
            axis_b = (
                perpendicular
                if float(perpendicular @ projected_axes[1]) >= 0
                else -perpendicular
            )
            orientation_source = "wall-frame"
            orientation_concentration = float(
                aligned_segment_support(axis_a, axis_b)
                / total_segment_length
            )

    if axis_a is None or axis_b is None:
        best_orientation: tuple[float, np.ndarray, np.ndarray] | None = None
        best_orientation_score = -1.0
        for candidate in segments:
            axis_a_candidate = np.asarray(candidate["direction"])
            axis_b_candidate = np.array(
                [-axis_a_candidate[1], axis_a_candidate[0]]
            )
            support_a = 0.0
            support_b = 0.0
            for segment in segments:
                direction = np.asarray(segment["direction"])
                length = float(segment["length"])
                alignment_a = abs(float(direction @ axis_a_candidate))
                alignment_b = abs(float(direction @ axis_b_candidate))
                if max(alignment_a, alignment_b) < direction_tolerance_cos:
                    continue
                if alignment_a >= alignment_b:
                    support_a += length
                else:
                    support_b += length
            aligned_support = support_a + support_b
            balance = min(support_a, support_b) / max(
                support_a, support_b, 1e-9
            )
            score = aligned_support * (0.5 + 0.5 * balance)
            if score > best_orientation_score:
                best_orientation_score = score
                best_orientation = (
                    aligned_support,
                    axis_a_candidate,
                    axis_b_candidate,
                )
        assert best_orientation is not None
        aligned_support, axis_a, axis_b = best_orientation
        orientation_concentration = float(
            aligned_support / total_segment_length
        )
        dbg["定向"] = (
            f"{orientation_source} 集中度 {orientation_concentration:.2f}"
            "（回退路径要求 ≥0.35）"
        )
        if orientation_concentration < 0.35:
            fail("面板边界方向不稳定")

    def dense_extent(positions: np.ndarray) -> tuple[float, float]:
        """Trim sparse tails (for example mask bleed) after quantiles."""
        low_q, high_q = np.quantile(positions, [0.005, 0.995])
        span = float(high_q - low_q)
        if span < cell_size * 4:
            return float(low_q), float(high_q)
        bin_count = int(np.ceil(span / cell_size))
        counts, bin_edges = np.histogram(
            positions,
            bins=bin_count,
            range=(float(low_q), float(high_q)),
        )
        occupied = counts[counts > 0]
        cutoff = max(1.0, 0.25 * float(np.median(occupied)))
        dense_bins = np.flatnonzero(counts >= cutoff)
        if dense_bins.size == 0:
            return float(low_q), float(high_q)
        return (
            float(bin_edges[dense_bins[0]]),
            float(bin_edges[dense_bins[-1] + 1]),
        )

    positions_a = component_planar @ axis_a
    positions_b = component_planar @ axis_b
    a_min, a_max = dense_extent(positions_a)
    b_min, b_max = dense_extent(positions_b)
    if a_max - a_min >= b_max - b_min:
        long_axis_2d, short_axis_2d = axis_a, axis_b
        long_min, long_max = float(a_min), float(a_max)
        short_min, short_max = float(b_min), float(b_max)
    else:
        long_axis_2d, short_axis_2d = axis_b, -axis_a
        long_min, long_max = float(b_min), float(b_max)
        short_positions = component_planar @ short_axis_2d
        short_min, short_max = dense_extent(short_positions)

    offset_tolerance = float(
        np.clip(
            0.12 * min(long_max - long_min, short_max - short_min),
            max(0.004, threshold_m + cell_size),
            0.015,
        )
    )
    dbg.setdefault(
        "定向",
        f"{orientation_source} 集中度 {orientation_concentration:.2f}",
    )
    dbg["长短轴范围mm"] = (
        f"长轴 [{long_min*1000:.0f}, {long_max*1000:.0f}]"
        f" 短轴 [{short_min*1000:.0f}, {short_max*1000:.0f}]"
        f" 找边容差 ±{offset_tolerance*1000:.1f}mm 平行容差 ±15°"
    )
    dbg["直线段明细"] = "; ".join(
        (
            lambda direction, midpoint, length: (
                f"长{length*1000:.0f}mm"
                f" 与长轴夹角{np.degrees(np.arccos(np.clip(abs(float(direction @ long_axis_2d)), 0, 1))):.0f}°"
                f" 短向@{float(midpoint @ short_axis_2d)*1000:.0f}"
                f" 长向@{float(midpoint @ long_axis_2d)*1000:.0f}"
            )
        )(
            np.asarray(seg["direction"]),
            np.asarray(seg["midpoint"]),
            float(seg["length"]),
        )
        for seg in sorted(
            segments, key=lambda item: -float(item["length"])
        )[:24]
    )

    def supported_offset(
        axis_direction: np.ndarray,
        offset_direction: np.ndarray,
        expected: float,
    ) -> tuple[float, float]:
        candidates: list[tuple[float, float]] = []
        for segment in segments:
            alignment = abs(
                float(np.asarray(segment["direction"]) @ axis_direction)
            )
            offset = float(
                np.asarray(segment["midpoint"]) @ offset_direction
            )
            if (
                alignment >= direction_tolerance_cos
                and abs(offset - expected) <= offset_tolerance
            ):
                candidates.append((offset, float(segment["length"])))
        if not candidates:
            return expected, 0.0
        # Use only the strongest parallel cluster so bleed cannot pull edges.
        cluster_width = threshold_m + cell_size
        best_members: list[tuple[float, float]] = []
        best_weight = -1.0
        for anchor_offset, _ in candidates:
            members = [
                item
                for item in candidates
                if abs(item[0] - anchor_offset) <= cluster_width
            ]
            weight = sum(item[1] for item in members)
            if weight > best_weight:
                best_weight, best_members = weight, members
        weights = np.asarray([item[1] for item in best_members])
        values = np.asarray([item[0] for item in best_members])
        return float(np.average(values, weights=weights)), float(weights.sum())

    short_min, long_at_short_min = supported_offset(
        long_axis_2d, short_axis_2d, short_min
    )
    short_max, long_at_short_max = supported_offset(
        long_axis_2d, short_axis_2d, short_max
    )
    long_min, short_at_long_min = supported_offset(
        short_axis_2d, long_axis_2d, long_min
    )
    long_max, short_at_long_max = supported_offset(
        short_axis_2d, long_axis_2d, long_max
    )
    dbg["四边支持mm"] = (
        f"长边@短向{short_min*1000:.0f}: {long_at_short_min*1000:.0f}"
        f"｜长边@短向{short_max*1000:.0f}: {long_at_short_max*1000:.0f}"
        f"｜短边@长向{long_min*1000:.0f}: {short_at_long_min*1000:.0f}"
        f"｜短边@长向{long_max*1000:.0f}: {short_at_long_max*1000:.0f}"
        "（各取两侧较大者，需 ≥12mm）"
    )
    if max(long_at_short_min, long_at_short_max) < 0.012:
        fail("没有找到受点云支持的完整长边")
    if max(short_at_long_min, short_at_long_max) < 0.012:
        fail("没有找到受点云支持的完整短边")
    selected_short = (
        short_min if long_at_short_min >= long_at_short_max else short_max
    )
    selected_long = (
        long_min if short_at_long_min >= short_at_long_max else long_max
    )
    opposite_long = long_max if selected_long == long_min else long_min
    opposite_short = short_max if selected_short == short_min else short_min

    def camera_point(long_position: float, short_position: float) -> np.ndarray:
        planar_position = (
            long_position * long_axis_2d
            + short_position * short_axis_2d
        )
        return center + planar_basis @ planar_position

    corner = camera_point(selected_long, selected_short)
    long_end = camera_point(opposite_long, selected_short)
    short_end = camera_point(selected_long, opposite_short)
    rectangle_corners = [
        camera_point(long_min, short_min),
        camera_point(long_max, short_min),
        camera_point(long_max, short_max),
        camera_point(long_min, short_max),
    ]
    rectangle_center = np.mean(rectangle_corners, axis=0)
    long_axis_camera = planar_basis @ long_axis_2d
    short_axis_camera = planar_basis @ short_axis_2d
    long_axis_camera /= np.linalg.norm(long_axis_camera)
    short_axis_camera /= np.linalg.norm(short_axis_camera)
    long_length = float(abs(opposite_long - selected_long))
    short_length = float(abs(opposite_short - selected_short))
    logger.info(
        "拟合成功 %.0f×%.0fmm 长边支持 %.0fmm 短边支持 %.0fmm 定向 %s 集中度 %.2f",
        long_length * 1000,
        short_length * 1000,
        max(long_at_short_min, long_at_short_max) * 1000,
        max(short_at_long_min, short_at_long_max) * 1000,
        orientation_source,
        orientation_concentration,
    )
    return {
        "available": True,
        "fit_method": "local-ransac-supported-orthogonal-rectangle",
        "point_count": int(points.shape[0]),
        "component_point_count": int(component_planar.shape[0]),
        "inlier_count": int(inliers.shape[0]),
        "inlier_ratio": float(inliers.shape[0] / points.shape[0]),
        "excluded_point_count": int(points.shape[0] - inliers.shape[0]),
        "camera_side_protrusion_point_count": int(
            (final_distances < -threshold_m).sum()
        ),
        "threshold_m": float(threshold_m),
        "rms_m": float(np.sqrt(np.mean(residuals**2))),
        "center_camera_m": center.tolist(),
        "normal_camera": normal.tolist(),
        "rectangle_corners_camera_m": [
            corner_point.tolist() for corner_point in rectangle_corners
        ],
        "rectangle_center_camera_m": rectangle_center.tolist(),
        "long_axis_camera": long_axis_camera.tolist(),
        "short_axis_camera": short_axis_camera.tolist(),
        "long_length_m": long_length,
        "short_length_m": short_length,
        "axis_aspect_ratio": float(
            long_length / max(short_length, 1e-9)
        ),
        "orientation_support": orientation_concentration,
        "orientation_source": orientation_source,
        "edges": [
            {
                "role": "long",
                "start_camera_m": corner.tolist(),
                "end_camera_m": long_end.tolist(),
                "length_m": long_length,
                "support_length_m": float(
                    max(long_at_short_min, long_at_short_max)
                ),
            },
            {
                "role": "short",
                "start_camera_m": corner.tolist(),
                "end_camera_m": short_end.tolist(),
                "length_m": short_length,
                "support_length_m": float(
                    max(short_at_long_min, short_at_long_max)
                ),
            },
        ],
    }


def _bimodal_brightness_threshold(
    brightness: np.ndarray,
    *,
    dark_fraction: float = 0.30,
    min_separability: float = 0.5,
) -> dict[str, float] | None:
    """Split panel brightness from dark knobs, gaps, and shadows."""
    counts, bin_edges = np.histogram(brightness, bins=64, range=(0, 255))
    total = float(counts.sum())
    if total < 1:
        return None
    centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    total_sum = float((counts * centers).sum())
    best_means: tuple[float, float] | None = None
    best_between_var = -1.0
    weight_dark = 0.0
    sum_dark = 0.0
    for index in range(1, 64):
        weight_dark += float(counts[index - 1])
        sum_dark += float(counts[index - 1]) * float(centers[index - 1])
        weight_bright = total - weight_dark
        if weight_dark == 0.0 or weight_bright == 0.0:
            continue
        mean_dark = sum_dark / weight_dark
        mean_bright = (total_sum - sum_dark) / weight_bright
        between_var = (
            weight_dark * weight_bright * (mean_dark - mean_bright) ** 2
        )
        if between_var > best_between_var:
            best_between_var = between_var
            best_means = (mean_dark, mean_bright)
    if best_means is None:
        return None
    variance = float(np.var(brightness))
    separability = best_between_var / (total * total * variance + 1e-9)
    if separability < min_separability:
        return None
    mean_dark, mean_bright = best_means
    return {
        "threshold": mean_dark + dark_fraction * (mean_bright - mean_dark),
        "separability": float(separability),
        "dark_cluster_mean": float(mean_dark),
        "bright_cluster_mean": float(mean_bright),
    }


def analyze_yolo_mask_panel(
    pointcloud: PointCloud,
    boxes: list[dict[str, Any]],
    image_shape: tuple[int, int] | list[int],
    *,
    threshold_m: float = 0.004,
    min_points: int = 100,
    wall_plane: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit the highest-confidence YOLO panel from an existing point cloud."""
    if not boxes:
        return {"available": False, "reason": "当前帧没有 YOLO 实例"}
    valid_boxes: list[tuple[int, dict[str, Any]]] = []
    for index, box in enumerate(boxes):
        try:
            confidence = float(box.get("conf", 0.0))
        except (TypeError, ValueError):
            continue
        if np.isfinite(confidence):
            valid_boxes.append((index, box))
    if not valid_boxes:
        return {"available": False, "reason": "YOLO 实例置信度无效"}
    box_index, box = max(
        valid_boxes, key=lambda item: float(item[1].get("conf", 0.0))
    )
    if len(image_shape) != 2:
        raise ValueError("图像尺寸必须是 (height, width)")
    shape = (int(image_shape[0]), int(image_shape[1]))
    if shape[0] <= 0 or shape[1] <= 0:
        raise ValueError("图像尺寸必须为正数")
    points = np.asarray(pointcloud.positions, dtype=np.float64)
    pixels = np.asarray(pointcloud.pixels, dtype=np.float64)
    rgb = np.asarray(pointcloud.rgb)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("PointCloud positions 必须是 N×3")
    if pixels.shape != (points.shape[0], 2):
        raise ValueError("像素坐标必须与点云一一对应")
    if rgb.shape != (points.shape[0], 3):
        raise ValueError("PointCloud rgb 必须与点云一一对应")
    inside = detection_pixel_mask(
        pixels[:, 0], pixels[:, 1], box, image_shape=shape
    )
    mask_points = points[inside]
    color_filter: dict[str, Any] = {
        "enabled": False,
        "removed_point_count": 0,
    }
    removed_points = np.empty((0, 3), dtype=np.float64)
    brightness = rgb[inside, :3].astype(np.float64).mean(axis=1)
    if brightness.size:
        split = _bimodal_brightness_threshold(brightness)
        if split is not None:
            keep = brightness >= split["threshold"]
            # Never remove nearly the whole mask.
            if float(keep.mean()) >= 0.25:
                color_filter = {
                    "enabled": True,
                    "removed_point_count": int((~keep).sum()),
                    "brightness_threshold": float(split["threshold"]),
                    "separability": split["separability"],
                    "dark_cluster_mean": split["dark_cluster_mean"],
                    "bright_cluster_mean": split["bright_cluster_mean"],
                }
                removed_points = mask_points[~keep]
                mask_points = mask_points[keep]

    def preview_points(
        preview_source: np.ndarray, limit: int = 4_000
    ) -> list[list[float]]:
        source = preview_source[
            np.isfinite(preview_source).all(axis=1)
        ]
        if source.shape[0] > limit:
            indices = np.linspace(
                0, source.shape[0] - 1, limit, dtype=np.int64
            )
            source = source[indices]
        return np.round(source, 5).tolist()

    classification_preview = {
        "kept_point_count": int(mask_points.shape[0]),
        "removed_point_count": int(removed_points.shape[0]),
        "kept_camera_m": preview_points(mask_points),
        "removed_camera_m": preview_points(removed_points),
    }
    detection = {
        "box_index": int(box_index),
        "cls": int(box.get("cls", -1)),
        "name": str(box.get("name", box.get("cls", "unknown"))),
        "conf": float(box.get("conf", 0.0)),
        "xyxy": box.get("xyxy"),
        "used_polygon_mask": bool(box.get("polygon") is not None),
    }
    candidate_summary = "；".join(
        f"[{index}] {candidate.get('name')} conf={float(candidate.get('conf', 0)):.2f}"
        f" 框{float(candidate['xyxy'][2]) - float(candidate['xyxy'][0]):.0f}"
        f"×{float(candidate['xyxy'][3]) - float(candidate['xyxy'][1]):.0f}px"
        f" poly{len(candidate.get('polygon') or [])}点"
        for index, candidate in valid_boxes
    )
    logger.info(
        "候选 %d 个：%s ｜ 选中[%d]（置信度最高） mask内点 %d → 参与拟合 %d（亮度过滤=%s）",
        len(valid_boxes),
        candidate_summary,
        box_index,
        int(mask_points.shape[0]) + int(removed_points.shape[0]),
        int(mask_points.shape[0]),
        (
            f"剔除暗点 {color_filter['removed_point_count']}"
            f"，亮度阈值 {color_filter.get('brightness_threshold', 0):.0f}"
            if color_filter["enabled"]
            else "未启用"
        ),
    )
    preferred_axes: tuple[Any, Any] | None = None
    if wall_plane is not None:
        wall_x = wall_plane.get("x_axis_camera")
        wall_z = wall_plane.get("z_axis_camera")
        if wall_x is not None and wall_z is not None:
            preferred_axes = (wall_x, wall_z)
    try:
        fitted = fit_yolo_panel_rectangle(
            mask_points,
            threshold_m=threshold_m,
            min_points=min_points,
            seed=20_000 + int(box_index),
            preferred_axes_camera=preferred_axes,
        )
    except ValueError as exc:
        return {
            "available": False,
            "reason": str(exc),
            "debug": getattr(exc, "debug", None),
            "mask_point_count": int(mask_points.shape[0]),
            "color_filter": color_filter,
            "classification_preview": classification_preview,
            "detection": detection,
        }
    fitted["mask_point_count"] = int(mask_points.shape[0])
    fitted["color_filter"] = color_filter
    fitted["classification_preview"] = classification_preview
    fitted["detection"] = detection
    return fitted


__all__ = [
    "analyze_yolo_mask_panel",
    "fit_yolo_panel_rectangle",
]
