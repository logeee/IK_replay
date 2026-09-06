"""兼容转发：柜面坐标系方法一已迁到 ``api/cabinet_frame/method1_plane_analysis.py``。

新代码请用 ``api.cabinet_frame.build_cabinet_frame``（按 18000 配置分发）；
这里只保留旧的导入路径（``fit_dominant_plane`` 被 api/cabinet_panel_fit.py
复用，``build_wall_coordinate_frame`` 供直接调用方法一）。
"""
from __future__ import annotations

from .cabinet_frame.method1_plane_analysis import (  # noqa: F401
    build_wall_coordinate_frame,
    describe_p0_boundary_lines,
    estimate_wall_x_from_p0_boundary_lines,
    estimate_wall_x_from_plane_intersections,
    estimate_wall_x_from_secondary_plane_shape,
    fit_dominant_plane,
    segment_dominant_planes,
    split_plane_labels_by_connectivity,
)

__all__ = [
    "build_wall_coordinate_frame",
    "fit_dominant_plane",
]
