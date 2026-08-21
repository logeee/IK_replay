"""Pure in-memory cabinet/wall coordinate-frame estimation.

Migrated from ``rgbd_collector.analysis``.  This module deliberately contains
no dataset lookup, calibration persistence, or file-system API.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


def fit_dominant_plane(
    points_xyz: np.ndarray,
    *,
    threshold_m: float = 0.008,
    iterations: int = 240,
    min_inlier_ratio: float = 0.20,
    seed: int = 0,
) -> dict[str, Any]:
    points = np.asarray(points_xyz, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("点云必须是 N×3 数组")
    points = points[np.isfinite(points).all(axis=1)]
    if points.shape[0] < 100:
        raise ValueError("有效点太少，无法拟合柜面")
    if not 0.001 <= threshold_m <= 0.05:
        raise ValueError("平面阈值必须在 1–50 mm 之间")

    rng = np.random.default_rng(seed)
    fitting_points = (
        points[rng.choice(points.shape[0], 60_000, replace=False)]
        if points.shape[0] > 60_000
        else points
    )
    best_mask: np.ndarray | None = None
    best_count = 0
    for _ in range(iterations):
        triangle = fitting_points[
            rng.choice(fitting_points.shape[0], 3, replace=False)
        ]
        normal = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
        length = np.linalg.norm(normal)
        if length < 1e-8:
            continue
        normal /= length
        mask = np.abs((fitting_points - triangle[0]) @ normal) <= threshold_m
        count = int(mask.sum())
        if count > best_count:
            best_count, best_mask = count, mask
    if best_mask is None:
        raise ValueError("未找到稳定平面")
    inlier_ratio = best_count / fitting_points.shape[0]
    if inlier_ratio < min_inlier_ratio:
        raise ValueError(
            f"最大平面内点比例仅 {inlier_ratio:.1%}，请调整视角或阈值"
        )

    inliers = fitting_points[best_mask]
    origin = inliers.mean(axis=0)
    _, _, vh = np.linalg.svd(inliers - origin, full_matrices=False)
    normal = vh[-1]
    normal /= np.linalg.norm(normal)
    if np.dot(normal, origin) < 0:
        normal = -normal
    refined_mask = (
        np.abs((fitting_points - origin) @ normal) <= threshold_m
    )
    inliers = fitting_points[refined_mask]
    origin = inliers.mean(axis=0)
    _, _, vh = np.linalg.svd(inliers - origin, full_matrices=False)
    normal = vh[-1]
    normal /= np.linalg.norm(normal)
    if np.dot(normal, origin) < 0:
        normal = -normal

    wall_y = normal
    wall_center = origin.copy()
    camera_up = np.array([0.0, -1.0, 0.0])
    wall_z = camera_up - np.dot(camera_up, wall_y) * wall_y
    wall_z_length = np.linalg.norm(wall_z)
    if wall_z_length < 1e-6:
        raise ValueError("墙面法向与相机向上方向平行，无法确定墙面 Z 轴")
    wall_z /= wall_z_length
    wall_x = np.cross(wall_y, wall_z)
    wall_x /= np.linalg.norm(wall_x)
    wall_z = np.cross(wall_x, wall_y)
    wall_z /= np.linalg.norm(wall_z)

    # Camera-origin projection is stable when the visible wall area changes.
    origin = np.dot(origin, wall_y) * wall_y
    residuals = (inliers - origin) @ wall_y
    return {
        "origin_camera_m": origin.tolist(),
        "center_camera_m": wall_center.tolist(),
        "normal_camera": wall_y.tolist(),
        "x_axis_camera": wall_x.tolist(),
        "y_axis_camera": wall_y.tolist(),
        "z_axis_camera": wall_z.tolist(),
        "coordinate_system": "wall-right-handed-x-right-y-inward-z-up",
        "origin_definition": "camera-origin-projection-on-wall",
        "axis_estimation": "camera-up-projection",
        "threshold_m": threshold_m,
        "inlier_count": int(inliers.shape[0]),
        "sample_count": int(fitting_points.shape[0]),
        "inlier_ratio": float(inliers.shape[0] / fitting_points.shape[0]),
        "rms_m": float(np.sqrt(np.mean(residuals**2))),
    }


def segment_dominant_planes(
    points_xyz: np.ndarray,
    *,
    threshold_m: float = 0.008,
    iterations: int = 160,
    max_planes: int = 6,
    min_inlier_ratio: float = 0.03,
    min_inlier_count: int = 800,
    sample_limit: int = 60_000,
    seed: int = 0,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    points = np.asarray(points_xyz, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("点云必须是 N×3 数组")
    if not 0.001 <= threshold_m <= 0.05:
        raise ValueError("平面阈值必须在 1–50 mm 之间")
    if not 1 <= max_planes <= 12:
        raise ValueError("最大平面数量必须在 1–12 之间")

    valid = np.isfinite(points).all(axis=1)
    labels = np.full(points.shape[0], -1, dtype=np.int32)
    valid_count = int(valid.sum())
    required_count = max(
        min_inlier_count, int(np.ceil(valid_count * min_inlier_ratio))
    )
    if valid_count < max(3, required_count):
        return labels, []
    rng = np.random.default_rng(seed)
    planes: list[dict[str, Any]] = []
    remaining = valid.copy()
    for plane_index in range(max_planes):
        remaining_indices = np.flatnonzero(remaining)
        if remaining_indices.size < required_count:
            break
        sample_indices = (
            rng.choice(remaining_indices, sample_limit, replace=False)
            if remaining_indices.size > sample_limit
            else remaining_indices
        )
        sample = points[sample_indices]
        best_mask: np.ndarray | None = None
        best_count = 0
        for _ in range(iterations):
            triangle = sample[rng.choice(sample.shape[0], 3, replace=False)]
            normal = np.cross(
                triangle[1] - triangle[0], triangle[2] - triangle[0]
            )
            length = np.linalg.norm(normal)
            if length < 1e-8:
                continue
            normal /= length
            mask = np.abs((sample - triangle[0]) @ normal) <= threshold_m
            count = int(mask.sum())
            if count > best_count:
                best_count, best_mask = count, mask
        if best_mask is None or best_count < 3:
            break
        fitting_inliers = sample[best_mask]
        origin = fitting_inliers.mean(axis=0)
        _, _, vh = np.linalg.svd(
            fitting_inliers - origin, full_matrices=False
        )
        normal = vh[-1] / np.linalg.norm(vh[-1])
        candidate_points = points[remaining_indices]
        candidate_mask = (
            np.abs((candidate_points - origin) @ normal) <= threshold_m
        )
        if int(candidate_mask.sum()) < required_count:
            break
        full_inliers = candidate_points[candidate_mask]
        refinement = (
            full_inliers[
                rng.choice(full_inliers.shape[0], sample_limit, replace=False)
            ]
            if full_inliers.shape[0] > sample_limit
            else full_inliers
        )
        origin = refinement.mean(axis=0)
        _, _, vh = np.linalg.svd(refinement - origin, full_matrices=False)
        normal = vh[-1] / np.linalg.norm(vh[-1])
        if np.dot(normal, origin) < 0:
            normal = -normal
        distances = np.abs((candidate_points - origin) @ normal)
        candidate_mask = distances <= threshold_m
        inlier_count = int(candidate_mask.sum())
        if inlier_count < required_count:
            break
        selected_indices = remaining_indices[candidate_mask]
        labels[selected_indices] = plane_index
        remaining[selected_indices] = False
        planes.append(
            {
                "index": plane_index,
                "origin_camera_m": origin.tolist(),
                "normal_camera": normal.tolist(),
                "inlier_count": inlier_count,
                "inlier_ratio": float(inlier_count / valid_count),
                "rms_m": float(
                    np.sqrt(np.mean(distances[candidate_mask] ** 2))
                ),
            }
        )
    return labels, planes


def split_plane_labels_by_connectivity(
    points_xyz: np.ndarray,
    plane_labels: np.ndarray,
    segmented_planes: list[dict[str, Any]],
    pixel_coordinates: np.ndarray,
    image_shape: tuple[int, int] | list[int],
    *,
    stride: int,
    source_point_count: int,
    min_component_count: int = 300,
    min_component_ratio: float = 0.002,
    max_patches: int = 12,
    preserve_farthest_plane: bool = False,
    max_planar_point_distance_from_farthest_plane_m: float | None = None,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    points = np.asarray(points_xyz, dtype=np.float64)
    labels = np.asarray(plane_labels, dtype=np.int32)
    pixels = np.asarray(pixel_coordinates, dtype=np.int32)
    if labels.shape != (points.shape[0],) or pixels.shape != (
        points.shape[0],
        2,
    ):
        raise ValueError("平面标签和像素坐标必须与点云数量一致")
    if (
        max_planar_point_distance_from_farthest_plane_m is not None
        and max_planar_point_distance_from_farthest_plane_m <= 0
    ):
        raise ValueError("P0 平面内点距离阈值必须大于零")
    height, width = int(image_shape[0]), int(image_shape[1])
    sampling_ratio = max(source_point_count / max(points.shape[0], 1), 1.0)
    sample_factor = (
        1 if sampling_ratio < 1.5 else int(np.ceil(np.sqrt(sampling_ratio)))
    )
    cell_size = max(1, stride * sample_factor)
    grid_width = int(np.ceil(width / cell_size))
    grid_height = int(np.ceil(height / cell_size))
    grid_u = np.clip(pixels[:, 0] // cell_size, 0, grid_width - 1)
    grid_v = np.clip(pixels[:, 1] // cell_size, 0, grid_height - 1)
    required_count = max(
        min_component_count,
        int(np.ceil(points.shape[0] * min_component_ratio)),
    )

    farthest_parent_index: int | None = None
    if preserve_farthest_plane and segmented_planes:
        farthest_parent_index = max(
            (int(plane["index"]) for plane in segmented_planes),
            key=lambda index: float(np.median(points[labels == index, 2])),
        )
    p0_origin: np.ndarray | None = None
    p0_planar_axes: np.ndarray | None = None
    p0_spatial_buckets: dict[tuple[int, int], np.ndarray] = {}
    if farthest_parent_index is not None:
        p0_plane = next(
            plane
            for plane in segmented_planes
            if int(plane["index"]) == farthest_parent_index
        )
        p0_origin = np.asarray(
            p0_plane.get(
                "origin_camera_m",
                points[labels == farthest_parent_index].mean(axis=0),
            ),
            dtype=np.float64,
        )
        p0_normal = np.asarray(p0_plane["normal_camera"], dtype=np.float64)
        p0_normal /= np.linalg.norm(p0_normal)
        reference = np.array([1.0, 0.0, 0.0])
        if abs(float(reference @ p0_normal)) > 0.9:
            reference = np.array([0.0, 1.0, 0.0])
        planar_x = reference - (reference @ p0_normal) * p0_normal
        planar_x /= np.linalg.norm(planar_x)
        planar_z = np.cross(p0_normal, planar_x)
        planar_z /= np.linalg.norm(planar_z)
        p0_planar_axes = np.column_stack((planar_x, planar_z))
        if max_planar_point_distance_from_farthest_plane_m is not None:
            radius = max_planar_point_distance_from_farthest_plane_m
            p0_coordinates = (
                points[labels == farthest_parent_index] - p0_origin
            ) @ p0_planar_axes
            buckets: dict[tuple[int, int], list[np.ndarray]] = {}
            cells = np.floor(p0_coordinates / radius).astype(np.int64)
            for coordinate, cell in zip(p0_coordinates, cells):
                buckets.setdefault((int(cell[0]), int(cell[1])), []).append(
                    coordinate
                )
            p0_spatial_buckets = {
                key: np.asarray(coordinates, dtype=np.float64)
                for key, coordinates in buckets.items()
            }

    planar_distance_cache: dict[int, float] = {}

    def nearest_planar_distance_to_p0(indices: np.ndarray) -> float:
        cache_key = id(indices)
        cached = planar_distance_cache.get(cache_key)
        if cached is not None:
            return cached
        if (
            p0_origin is None
            or p0_planar_axes is None
            or max_planar_point_distance_from_farthest_plane_m is None
        ):
            return 0.0
        if np.all(labels[indices] == farthest_parent_index):
            planar_distance_cache[cache_key] = 0.0
            return 0.0
        radius = max_planar_point_distance_from_farthest_plane_m
        coordinates = (points[indices] - p0_origin) @ p0_planar_axes
        cells = np.floor(coordinates / radius).astype(np.int64)
        nearest = float("inf")
        for coordinate, cell in zip(coordinates, cells):
            for offset_x in (-1, 0, 1):
                for offset_z in (-1, 0, 1):
                    bucket = p0_spatial_buckets.get(
                        (
                            int(cell[0]) + offset_x,
                            int(cell[1]) + offset_z,
                        )
                    )
                    if bucket is not None:
                        nearest = min(
                            nearest,
                            float(
                                np.min(
                                    np.linalg.norm(
                                        bucket - coordinate, axis=1
                                    )
                                )
                            ),
                        )
        planar_distance_cache[cache_key] = nearest
        return nearest

    preserved_candidate: tuple[np.ndarray, int] | None = None
    component_candidates: list[tuple[np.ndarray, int]] = []
    for plane in segmented_planes:
        parent_index = int(plane["index"])
        parent_indices = np.flatnonzero(labels == parent_index)
        if parent_indices.size < required_count:
            continue
        if parent_index == farthest_parent_index:
            preserved_candidate = (parent_indices, parent_index)
            continue
        mask = np.zeros((grid_height, grid_width), dtype=np.uint8)
        mask[grid_v[parent_indices], grid_u[parent_indices]] = 1
        _, components = cv2.connectedComponents(mask, connectivity=8)
        component_ids = components[
            grid_v[parent_indices], grid_u[parent_indices]
        ]
        counts = np.bincount(component_ids)
        accepted = [
            int(component_id)
            for component_id in np.argsort(counts[1:])[::-1] + 1
            if counts[component_id] >= required_count
        ]
        for component_id in accepted:
            component_candidates.append(
                (
                    parent_indices[component_ids == component_id],
                    parent_index,
                )
            )
    if max_planar_point_distance_from_farthest_plane_m is not None:
        component_candidates = [
            candidate
            for candidate in component_candidates
            if nearest_planar_distance_to_p0(candidate[0])
            <= max_planar_point_distance_from_farthest_plane_m
        ]
    component_candidates.sort(key=lambda item: item[0].size, reverse=True)
    if preserved_candidate is not None:
        component_candidates.insert(0, preserved_candidate)
    patch_labels = np.full(points.shape[0], -1, dtype=np.int32)
    patches: list[dict[str, Any]] = []
    valid_count = max(int(np.isfinite(points).all(axis=1).sum()), 1)
    for patch_index, (indices, parent_index) in enumerate(
        component_candidates[:max_patches]
    ):
        patch_points = points[indices]
        origin = patch_points.mean(axis=0)
        _, _, vh = np.linalg.svd(patch_points - origin, full_matrices=False)
        normal = vh[-1] / np.linalg.norm(vh[-1])
        if np.dot(normal, origin) < 0:
            normal = -normal
        residuals = (patch_points - origin) @ normal
        patch_labels[indices] = patch_index
        patches.append(
            {
                "index": patch_index,
                "parent_plane_index": parent_index,
                "is_farthest_plane": parent_index == farthest_parent_index,
                "median_depth_m": float(np.median(patch_points[:, 2])),
                "nearest_p0_xz_distance_m": nearest_planar_distance_to_p0(
                    indices
                ),
                "origin_camera_m": origin.tolist(),
                "normal_camera": normal.tolist(),
                "inlier_count": int(indices.size),
                "inlier_ratio": float(indices.size / valid_count),
                "rms_m": float(np.sqrt(np.mean(residuals**2))),
                "connectivity_cell_px": cell_size,
            }
        )
    return patch_labels, patches


def describe_p0_boundary_lines(
    points_xyz: np.ndarray,
    plane_labels: np.ndarray,
    segmented_planes: list[dict[str, Any]],
    *,
    max_point_distance_m: float = 0.010,
    max_lines_per_plane: int = 3,
    line_threshold_m: float = 0.003,
    min_line_points: int = 30,
    ransac_trials: int = 250,
) -> list[dict[str, Any]]:
    points = np.asarray(points_xyz, dtype=np.float64)
    labels = np.asarray(plane_labels, dtype=np.int32)
    if not segmented_planes:
        return []
    p0 = next(
        (
            plane
            for plane in segmented_planes
            if bool(plane.get("is_farthest_plane"))
        ),
        segmented_planes[0],
    )
    p0_index = int(p0["index"])
    p0_points = points[labels == p0_index]
    if p0_points.shape[0] < 2:
        return [{**plane, "boundary_lines": []} for plane in segmented_planes]
    origin = np.asarray(p0["origin_camera_m"], dtype=np.float64)
    normal = np.asarray(p0["normal_camera"], dtype=np.float64)
    normal /= np.linalg.norm(normal)
    reference = np.array([1.0, 0.0, 0.0])
    if abs(float(reference @ normal)) > 0.9:
        reference = np.array([0.0, 1.0, 0.0])
    planar_x = reference - (reference @ normal) * normal
    planar_x /= np.linalg.norm(planar_x)
    planar_z = np.cross(normal, planar_x)
    planar_z /= np.linalg.norm(planar_z)
    planar_axes = np.column_stack((planar_x, planar_z))
    p0_coordinates = (p0_points - origin) @ planar_axes

    described: list[dict[str, Any]] = []
    for plane in segmented_planes:
        plane_index = int(plane["index"])
        if plane_index == p0_index:
            described.append({**plane, "boundary_lines": []})
            continue
        plane_points = points[labels == plane_index]
        plane_coordinates = (plane_points - origin) @ planar_axes
        cells = np.floor(plane_coordinates / max_point_distance_m).astype(
            np.int64
        )
        buckets: dict[tuple[int, int], list[int]] = {}
        for local_index, cell in enumerate(cells):
            buckets.setdefault((int(cell[0]), int(cell[1])), []).append(
                local_index
            )
        nearest_local_indices: set[int] = set()
        p0_cells = np.floor(p0_coordinates / max_point_distance_m).astype(
            np.int64
        )
        for p0_coordinate, cell in zip(p0_coordinates, p0_cells):
            nearest_index: int | None = None
            nearest_distance = float("inf")
            for offset_x in (-1, 0, 1):
                for offset_z in (-1, 0, 1):
                    candidates = buckets.get(
                        (
                            int(cell[0]) + offset_x,
                            int(cell[1]) + offset_z,
                        )
                    )
                    if not candidates:
                        continue
                    candidate_indices = np.asarray(candidates, dtype=np.int64)
                    distances = np.linalg.norm(
                        plane_coordinates[candidate_indices] - p0_coordinate,
                        axis=1,
                    )
                    local_best = int(np.argmin(distances))
                    distance = float(distances[local_best])
                    if distance < nearest_distance:
                        nearest_distance = distance
                        nearest_index = int(candidate_indices[local_best])
            if (
                nearest_index is not None
                and nearest_distance <= max_point_distance_m
            ):
                nearest_local_indices.add(nearest_index)
        boundary_local = np.asarray(
            sorted(nearest_local_indices), dtype=np.int64
        )
        boundary_coordinates = plane_coordinates[boundary_local]
        boundary_points = plane_points[boundary_local]
        lines: list[dict[str, Any]] = []
        remaining = np.arange(boundary_coordinates.shape[0], dtype=np.int64)
        rng = np.random.default_rng(10_000 + plane_index)
        for line_index in range(max_lines_per_plane):
            if remaining.size < min_line_points:
                break
            best_inliers: np.ndarray | None = None
            best_score = 0.0
            for _ in range(ransac_trials):
                sample = rng.choice(remaining, size=2, replace=False)
                start, end = boundary_coordinates[sample]
                direction = end - start
                length = float(np.linalg.norm(direction))
                if length < 1e-6:
                    continue
                direction /= length
                relative = boundary_coordinates[remaining] - start
                distances = np.abs(
                    relative[:, 0] * direction[1]
                    - relative[:, 1] * direction[0]
                )
                inliers = remaining[distances <= line_threshold_m]
                if inliers.size < min_line_points:
                    continue
                center_2d = boundary_coordinates[inliers].mean(axis=0)
                _, _, vh = np.linalg.svd(
                    boundary_coordinates[inliers] - center_2d,
                    full_matrices=False,
                )
                positions = (
                    boundary_coordinates[inliers] - center_2d
                ) @ vh[0]
                low, high = np.quantile(positions, [0.02, 0.98])
                span = float(high - low)
                score = span * float(inliers.size)
                if span >= 0.02 and score > best_score:
                    best_score, best_inliers = score, inliers
            if best_inliers is None:
                break
            center_2d = boundary_coordinates[best_inliers].mean(axis=0)
            _, _, vh = np.linalg.svd(
                boundary_coordinates[best_inliers] - center_2d,
                full_matrices=False,
            )
            direction_2d = vh[0]
            direction_3d = planar_axes @ direction_2d
            direction_3d /= np.linalg.norm(direction_3d)
            inlier_points = boundary_points[best_inliers]
            center_3d = inlier_points.mean(axis=0)
            positions = (inlier_points - center_3d) @ direction_3d
            low, high = np.quantile(positions, [0.02, 0.98])
            lines.append(
                {
                    "index": line_index,
                    "start_camera_m": (
                        center_3d + low * direction_3d
                    ).tolist(),
                    "end_camera_m": (
                        center_3d + high * direction_3d
                    ).tolist(),
                    "direction_camera": direction_3d.tolist(),
                    "length_m": float(high - low),
                    "inlier_count": int(best_inliers.size),
                }
            )
            selected = boundary_coordinates[best_inliers]
            line_origin = selected.mean(axis=0)
            relative = boundary_coordinates[remaining] - line_origin
            distances = np.abs(
                relative[:, 0] * direction_2d[1]
                - relative[:, 1] * direction_2d[0]
            )
            remaining = remaining[distances > line_threshold_m]
        # Preserve the source fallback when RANSAC cannot isolate a line.
        if not lines and boundary_points.shape[0] >= 2:
            center_2d = boundary_coordinates.mean(axis=0)
            _, _, vh = np.linalg.svd(
                boundary_coordinates - center_2d, full_matrices=False
            )
            direction_3d = planar_axes @ vh[0]
            direction_3d /= np.linalg.norm(direction_3d)
            center_3d = boundary_points.mean(axis=0)
            positions = (boundary_points - center_3d) @ direction_3d
            low, high = np.quantile(positions, [0.02, 0.98])
            if high - low >= 0.02:
                lines.append(
                    {
                        "index": 0,
                        "start_camera_m": (
                            center_3d + low * direction_3d
                        ).tolist(),
                        "end_camera_m": (
                            center_3d + high * direction_3d
                        ).tolist(),
                        "direction_camera": direction_3d.tolist(),
                        "length_m": float(high - low),
                        "inlier_count": int(boundary_points.shape[0]),
                        "fit_method": "principal-axis-fallback",
                    }
                )
        lines.sort(key=lambda line: float(line["length_m"]), reverse=True)
        for index, line in enumerate(lines):
            line["index"] = index
            line.setdefault("fit_method", "ransac")
        described.append(
            {
                **plane,
                "p0_near_boundary_point_count": int(boundary_local.size),
                "boundary_lines": lines,
            }
        )
    return described


def estimate_wall_x_from_p0_boundary_lines(
    fitted_plane: dict[str, Any],
    segmented_planes: list[dict[str, Any]],
    *,
    max_direction_angle_deg: float = 1.0,
    min_group_line_length_m: float = 0.10,
    min_group_relative_length: float = 0.25,
    min_distinct_planes: int = 2,
    min_camera_up_line_angle_deg: float = 45.0,
) -> dict[str, Any]:
    result = dict(fitted_plane)
    wall_y = np.asarray(result["y_axis_camera"], dtype=np.float64)
    wall_y /= np.linalg.norm(wall_y)
    camera_up = np.array([0.0, -1.0, 0.0])
    candidates: list[
        tuple[float, np.ndarray, dict[str, Any], dict[str, Any]]
    ] = []
    for segment in segmented_planes:
        for line in segment.get("boundary_lines", []):
            line["selected_for_x"] = False
            line["accepted_for_x_group"] = False
            line["passes_camera_up_angle"] = False
            length = float(line.get("length_m", 0.0))
            direction = np.asarray(
                line.get("direction_camera"), dtype=np.float64
            )
            if direction.shape != (3,) or not np.isfinite(direction).all():
                continue
            direction -= np.dot(direction, wall_y) * wall_y
            direction_length = np.linalg.norm(direction)
            if direction_length >= 1e-6:
                direction /= direction_length
                camera_up_angle_deg = float(
                    np.degrees(
                        np.arccos(
                            np.clip(
                                abs(float(direction @ camera_up)), 0.0, 1.0
                            )
                        )
                    )
                )
                line["camera_up_line_angle_deg"] = camera_up_angle_deg
                line["passes_camera_up_angle"] = (
                    camera_up_angle_deg >= min_camera_up_line_angle_deg
                )
            if (
                length >= min_group_line_length_m
                and direction_length >= 1e-6
                and line["passes_camera_up_angle"]
            ):
                candidates.append((length, direction, segment, line))
    if not candidates:
        return result
    cosine_threshold = float(np.cos(np.radians(max_direction_angle_deg)))
    groups: list[tuple[tuple[int, float, float], list[Any]]] = []
    for reference in candidates:
        nearby = sorted(
            [
                candidate
                for candidate in candidates
                if abs(float(reference[1] @ candidate[1]))
                >= cosine_threshold
            ],
            key=lambda candidate: candidate[0],
            reverse=True,
        )
        members: list[Any] = []
        member_planes: set[int] = set()
        for candidate in nearby:
            plane_index = int(candidate[2]["index"])
            if plane_index in member_planes:
                continue
            if any(
                abs(float(candidate[1] @ member[1])) < cosine_threshold
                for member in members
            ):
                continue
            members.append(candidate)
            member_planes.add(plane_index)
        if members:
            relative_cutoff = (
                max(member[0] for member in members)
                * min_group_relative_length
            )
            members = [
                member for member in members if member[0] >= relative_cutoff
            ]
            member_planes = {
                int(member[2]["index"]) for member in members
            }
        if len(member_planes) < min_distinct_planes:
            continue
        groups.append(
            (
                (
                    len(member_planes),
                    float(sum(member[0] for member in members)),
                    max(member[0] for member in members),
                ),
                members,
            )
        )
    if not groups:
        return result
    _, selected_group = max(groups, key=lambda group: group[0])
    length, _, segment, selected_line = max(
        selected_group, key=lambda candidate: candidate[0]
    )
    for _, _, _, line in selected_group:
        line["accepted_for_x_group"] = True
    line_start = np.asarray(selected_line["start_camera_m"], dtype=np.float64)
    line_end = np.asarray(selected_line["end_camera_m"], dtype=np.float64)
    wall_x = line_end - line_start
    wall_x -= np.dot(wall_x, wall_y) * wall_y
    wall_x_length = np.linalg.norm(wall_x)
    if wall_x_length < 1e-6:
        return result
    wall_x /= wall_x_length
    wall_z = np.cross(wall_x, wall_y)
    wall_z /= np.linalg.norm(wall_z)
    if np.dot(wall_z, camera_up) < 0:
        wall_x, wall_z = -wall_x, -wall_z
        line_start, line_end = line_end, line_start
    selected_line["selected_for_x"] = True
    result.update(
        {
            "x_axis_camera": wall_x.tolist(),
            "z_axis_camera": wall_z.tolist(),
            "axis_estimation": "p0-nearest-boundary-line",
            "axis_reference_plane_index": int(segment["index"]),
            "axis_reference_boundary_index": int(selected_line["index"]),
            "axis_reference_line_length_m": length,
            "axis_reference_line_fit_method": selected_line.get(
                "fit_method", "ransac"
            ),
            "axis_reference_group_size": len(
                {int(member[2]["index"]) for member in selected_group}
            ),
            "axis_reference_group_line_count": len(selected_group),
            "axis_reference_group_total_length_m": float(
                sum(member[0] for member in selected_group)
            ),
            "axis_reference_group_angle_tolerance_deg": (
                max_direction_angle_deg
            ),
            "axis_reference_camera_up_angle_deg": float(
                selected_line["camera_up_line_angle_deg"]
            ),
            "axis_reference_min_camera_up_angle_deg": (
                min_camera_up_line_angle_deg
            ),
            "automatic_x_line_start_camera_m": line_start.tolist(),
            "automatic_x_line_end_camera_m": line_end.tolist(),
        }
    )
    return result


def estimate_wall_x_from_plane_intersections(
    fitted_plane: dict[str, Any],
    segmented_planes: list[dict[str, Any]],
    *,
    min_angle_deg: float = 2.0,
    max_angle_deg: float = 25.0,
    min_camera_horizontal_alignment: float = 0.65,
) -> dict[str, Any]:
    result = dict(fitted_plane)
    wall_y = np.asarray(result["y_axis_camera"], dtype=np.float64)
    wall_y /= np.linalg.norm(wall_y)
    camera_right = np.array([1.0, 0.0, 0.0])
    camera_right -= np.dot(camera_right, wall_y) * wall_y
    right_length = np.linalg.norm(camera_right)
    if right_length < 1e-6:
        return result
    camera_right /= right_length
    best: tuple[float, np.ndarray, dict[str, Any], float] | None = None
    for candidate in segmented_planes:
        normal = np.asarray(candidate.get("normal_camera"), dtype=np.float64)
        if normal.shape != (3,) or not np.isfinite(normal).all():
            continue
        normal_length = np.linalg.norm(normal)
        if normal_length < 1e-8:
            continue
        normal /= normal_length
        cosine = float(np.clip(abs(wall_y @ normal), 0.0, 1.0))
        angle_deg = float(np.degrees(np.arccos(cosine)))
        if not min_angle_deg <= angle_deg <= max_angle_deg:
            continue
        direction = np.cross(wall_y, normal)
        length = np.linalg.norm(direction)
        if length < 1e-6:
            continue
        direction /= length
        alignment = float(abs(direction @ camera_right))
        if alignment < min_camera_horizontal_alignment:
            continue
        score = float(candidate.get("inlier_ratio", 0.0)) * alignment
        if best is None or score > best[0]:
            best = (score, direction, candidate, angle_deg)
    if best is None:
        return result
    _, wall_x, reference, angle_deg = best
    wall_z = np.cross(wall_x, wall_y)
    wall_z /= np.linalg.norm(wall_z)
    camera_up = np.array([0.0, -1.0, 0.0])
    if np.dot(wall_z, camera_up) < 0:
        wall_x, wall_z = -wall_x, -wall_z
    result.update(
        {
            "x_axis_camera": wall_x.tolist(),
            "z_axis_camera": wall_z.tolist(),
            "axis_estimation": "multi-plane-intersection",
            "axis_reference_plane_index": int(reference["index"]),
            "axis_reference_angle_deg": angle_deg,
        }
    )
    return result


def estimate_wall_x_from_secondary_plane_shape(
    fitted_plane: dict[str, Any],
    points_xyz: np.ndarray,
    plane_labels: np.ndarray,
    segmented_planes: list[dict[str, Any]],
    *,
    min_normal_angle_deg: float = 0.8,
    max_normal_angle_deg: float = 25.0,
    min_axis_alignment: float = 0.65,
    min_shape_anisotropy: float = 1.7,
) -> dict[str, Any]:
    result = dict(fitted_plane)
    points = np.asarray(points_xyz, dtype=np.float64)
    labels = np.asarray(plane_labels, dtype=np.int32)
    wall_y = np.asarray(result["y_axis_camera"], dtype=np.float64)
    wall_y /= np.linalg.norm(wall_y)
    camera_right = np.array([1.0, 0.0, 0.0])
    camera_right -= np.dot(camera_right, wall_y) * wall_y
    camera_right /= np.linalg.norm(camera_right)
    best: tuple[
        float, np.ndarray, np.ndarray, np.ndarray, dict[str, Any]
    ] | None = None
    for candidate in segmented_planes:
        index = int(candidate["index"])
        if index == 0:
            continue
        candidate_points = points[labels == index]
        if candidate_points.shape[0] < 100:
            continue
        normal = np.asarray(candidate["normal_camera"], dtype=np.float64)
        normal_length = np.linalg.norm(normal)
        if normal.shape != (3,) or normal_length < 1e-8:
            continue
        normal /= normal_length
        angle_deg = float(
            np.degrees(
                np.arccos(
                    np.clip(abs(float(wall_y @ normal)), 0.0, 1.0)
                )
            )
        )
        if not min_normal_angle_deg <= angle_deg <= max_normal_angle_deg:
            continue
        center = candidate_points.mean(axis=0)
        centered = candidate_points - center
        eigenvalues, eigenvectors = np.linalg.eigh(
            centered.T @ centered / candidate_points.shape[0]
        )
        order = np.argsort(eigenvalues)[::-1]
        if eigenvalues[order[1]] <= 0:
            continue
        anisotropy = float(
            np.sqrt(eigenvalues[order[0]] / eigenvalues[order[1]])
        )
        if anisotropy < min_shape_anisotropy:
            continue
        wall_x = eigenvectors[:, order[0]]
        wall_x -= np.dot(wall_x, wall_y) * wall_y
        wall_x_length = np.linalg.norm(wall_x)
        if wall_x_length < 1e-6:
            continue
        wall_x /= wall_x_length
        alignment = float(abs(wall_x @ camera_right))
        if alignment < min_axis_alignment:
            continue
        if best is None or anisotropy > best[0]:
            positions = centered @ wall_x
            axis_start, axis_end = np.quantile(positions, [0.02, 0.98])
            best = (
                anisotropy,
                wall_x,
                center + axis_start * wall_x,
                center + axis_end * wall_x,
                {**candidate, "normal_angle_deg": angle_deg},
            )
    if best is None:
        return result
    anisotropy, wall_x, line_start, line_end, reference = best
    wall_z = np.cross(wall_x, wall_y)
    wall_z /= np.linalg.norm(wall_z)
    camera_up = np.array([0.0, -1.0, 0.0])
    if np.dot(wall_z, camera_up) < 0:
        wall_x, wall_z = -wall_x, -wall_z
        line_start, line_end = line_end, line_start
    result.update(
        {
            "x_axis_camera": wall_x.tolist(),
            "z_axis_camera": wall_z.tolist(),
            "axis_estimation": "secondary-plane-principal-axis",
            "axis_reference_plane_index": int(reference["index"]),
            "axis_reference_angle_deg": float(reference["normal_angle_deg"]),
            "axis_shape_anisotropy": anisotropy,
            "automatic_x_line_start_camera_m": line_start.tolist(),
            "automatic_x_line_end_camera_m": line_end.tolist(),
        }
    )
    return result


def build_wall_coordinate_frame(
    points_xyz: np.ndarray,
    pixel_coordinates: np.ndarray,
    image_shape: tuple[int, int] | list[int],
    *,
    plane_threshold_m: float = 0.008,
    stride: int = 3,
    min_plane_points: int = 300,
    plane_analysis_max_points: int = 200_000,
) -> dict[str, Any]:
    """Build a calibrated wall frame from arrays already held in memory."""
    points = np.asarray(points_xyz, dtype=np.float64)
    pixels = np.asarray(pixel_coordinates)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("点云必须是 N×3 数组")
    if pixels.shape != (points.shape[0], 2):
        raise ValueError("像素坐标必须与点云一一对应")
    if len(image_shape) != 2:
        raise ValueError("图像尺寸必须是 (height, width)")
    height, width = int(image_shape[0]), int(image_shape[1])
    if height <= 0 or width <= 0:
        raise ValueError("图像尺寸必须为正数")
    if stride < 1:
        raise ValueError("stride 必须大于零")
    if min_plane_points < 3:
        raise ValueError("最少平面点数不能小于 3")
    if not 1_000 <= plane_analysis_max_points <= 1_000_000:
        raise ValueError("X 轴分析最大点数必须在 1000~1000000")

    valid = np.isfinite(points).all(axis=1)
    valid &= np.isfinite(pixels).all(axis=1)
    points, pixels = points[valid], pixels[valid]
    if points.shape[0] < 100:
        raise ValueError("有效点太少，无法拟合柜面")
    source_point_count = int(points.shape[0])
    if points.shape[0] > plane_analysis_max_points:
        indices = np.linspace(
            0,
            points.shape[0] - 1,
            plane_analysis_max_points,
            dtype=np.int64,
        )
        plane_xyz, plane_pixels = points[indices], pixels[indices]
    else:
        plane_xyz, plane_pixels = points, pixels
    sampling_ratio = plane_xyz.shape[0] / max(source_point_count, 1)
    analysis_min_plane_points = max(
        3, int(np.ceil(min_plane_points * sampling_ratio))
    )
    plane = fit_dominant_plane(
        plane_xyz, threshold_m=plane_threshold_m
    )
    plane_labels, segmented_planes = segment_dominant_planes(
        plane_xyz, threshold_m=plane_threshold_m
    )
    plane_labels, segmented_planes = split_plane_labels_by_connectivity(
        plane_xyz,
        plane_labels,
        segmented_planes,
        plane_pixels,
        (height, width),
        stride=stride,
        source_point_count=source_point_count,
        min_component_count=analysis_min_plane_points,
        min_component_ratio=0.0,
        preserve_farthest_plane=True,
        max_planar_point_distance_from_farthest_plane_m=0.010,
    )
    plane_segments = describe_p0_boundary_lines(
        plane_xyz, plane_labels, segmented_planes
    )
    plane = estimate_wall_x_from_p0_boundary_lines(plane, plane_segments)
    if plane["axis_estimation"] == "camera-up-projection":
        plane = estimate_wall_x_from_secondary_plane_shape(
            plane, plane_xyz, plane_labels, segmented_planes
        )
        if plane["axis_estimation"] == "camera-up-projection":
            plane = estimate_wall_x_from_plane_intersections(
                plane, segmented_planes
            )
    plane.update(
        {
            "source": "automatic-in-memory-plane-analysis",
            "calibrated": True,
            "automatic_segmented_plane_count": len(segmented_planes),
            "plane_analysis_point_count": int(plane_xyz.shape[0]),
            "plane_analysis_source_point_count": source_point_count,
            "plane_analysis_downsampled": bool(
                plane_xyz.shape[0] < source_point_count
            ),
            "plane_analysis_skipped": False,
        }
    )
    return plane


__all__ = [
    "build_wall_coordinate_frame",
    "fit_dominant_plane",
]
