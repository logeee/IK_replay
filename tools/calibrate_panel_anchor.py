#!/usr/bin/env python3
"""标定 ``panel_anchor`` 自动选点模型的偏移（面板中心 → 锚点 → 点 1/点 3）。

输入：``tools/export_rgbd_frames.py`` 导出的数据集目录 + Mac 端 17002 点选
后保存的 ``annotations.jsonl``（每帧点 1 / 点 3 的相机系坐标）。

每帧按 7005 运行时**同一套代码**重算：YOLO 框（优先复用帧目录里的
``yolo_boxes.json``）→ 柜面坐标系（按注册表 cabinet_frame 配置）→「面板」
矩形中心（``api.cabinet_panel_anchor.fit_panel_reference``）；再把人工点转
到墙面系，得到「面板中心 → 点 i」的偏移样本 T_i。

聚合（两级偏移的约定，见 core/target_models.py）::

    T1 = mean(点 1 样本)      T3 = mean(点 3 样本)
    anchor  = (T1 + T3) / 2   —— 旋钮轴心（从不被点击，由数据算出）
    point1  = T1 - anchor     point3 = T3 - anchor（恒等于 -point1）

自检：
* 各点位样本沿 x/y/z 的标准差与留一 RMSE（估计精度）；
* point1 的 y/z 分量：左右两点应等高等深，这两项应接近 0（几 mm 内）；
* 面板矩形长/短边尺寸的一致性（写入 panel_size_mm 供运行时出画守卫）。

用法::

    python tools/calibrate_panel_anchor.py --dataset data/calibration_datasets/2026..._panel_calib
    python tools/calibrate_panel_anchor.py --dataset <dir> --write     # 直接写入 18000
    python tools/calibrate_panel_anchor.py --dataset <dir> --annotations /path/annotations.jsonl

结果同时写到 ``<dataset>/panel_anchor_calibration.json``。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from export_rgbd_frames import run_yolo  # noqa: E402  (tools/ 同级脚本)
from api.cabinet_frame import build_cabinet_frame  # noqa: E402
from api.cabinet_panel_anchor import (  # noqa: E402
    fit_panel_reference,
    select_knob_detection,
)
from api.pointcloud_core import build_pointcloud  # noqa: E402
from api.switch_states import SCENE_LEFT, SCENE_RIGHT  # noqa: E402
from core import capability_registry as reg  # noqa: E402
from core.target_models import (  # noqa: E402
    PANEL_ANCHOR,
    default_target_model_params,
    validate_target_model_config,
)

SLOT_EXPECTED_KNOB = {1: SCENE_RIGHT, 3: SCENE_LEFT}
# 与 7005 capture 默认一致
CAPTURE_STRIDE = 4
Z_MIN_M, Z_MAX_M = 0.15, 3.0
BOX_PADDING_RATIO = 0.1


# ----------------------------------------------------------------- 读数据
def load_session(dataset: Path) -> dict[str, Any]:
    session_file = dataset / "session.json"
    if not session_file.is_file():
        raise FileNotFoundError(f"{dataset} 不是数据集目录（缺 session.json）")
    return json.loads(session_file.read_text(encoding="utf-8"))


def load_annotations(path: Path) -> dict[str, dict[int, np.ndarray]]:
    """frame_id → {slot: target_camera_m}。同帧多行取最后一行；兼容 v3
    ``points`` 结构与旧版单点（顶层 target_camera_m + point_slot）。"""
    if not path.is_file():
        raise FileNotFoundError(f"标注文件不存在: {path}")
    per_frame: dict[str, dict[int, np.ndarray]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        frame_id = record.get("frame_id")
        if not isinstance(frame_id, str):
            continue
        points: dict[int, np.ndarray] = {}
        if isinstance(record.get("points"), dict):
            for key, entry in record["points"].items():
                try:
                    slot = int(entry.get("point_slot", key))
                    target = np.asarray(entry["target_camera_m"], dtype=np.float64)
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                if target.shape == (3,) and np.isfinite(target).all():
                    points[slot] = target
        elif record.get("target_camera_m") is not None:
            try:
                slot = int(record.get("point_slot", 1))
                target = np.asarray(record["target_camera_m"], dtype=np.float64)
            except (TypeError, ValueError):
                continue
            if target.shape == (3,) and np.isfinite(target).all():
                points[slot] = target
        if points:
            per_frame[frame_id] = points   # 覆盖：同帧以最后一行为准
    return per_frame


def load_frame(dataset: Path, session: dict[str, Any], frame_id: str) -> dict[str, Any]:
    frame_dir = dataset / "frames" / frame_id
    frame_meta = json.loads((frame_dir / "frame.json").read_text(encoding="utf-8"))
    bgr = cv2.imread(str(frame_dir / "color.jpg"), cv2.IMREAD_COLOR)
    depth_raw = cv2.imread(str(frame_dir / "depth_aligned.png"), cv2.IMREAD_UNCHANGED)
    if bgr is None or depth_raw is None:
        raise FileNotFoundError(f"{frame_id}: color.jpg / depth_aligned.png 读取失败")
    if depth_raw.dtype != np.uint16 or depth_raw.shape != bgr.shape[:2]:
        raise ValueError(f"{frame_id}: 对齐深度格式异常 {depth_raw.dtype}/{depth_raw.shape}")
    scale = float(frame_meta.get("depth_scale", {}).get("value", 1.0))
    depth_mm = depth_raw.astype(np.float32) * np.float32(scale)
    color = session["camera"]["color"]
    intr = color["intrinsics"]
    intrinsics = (float(intr["fx"]), float(intr["fy"]), float(intr["cx"]), float(intr["cy"]))
    distortion = np.asarray(
        (color.get("distortion") or {}).get("coefficients") or [], dtype=np.float64)
    boxes = None
    boxes_file = frame_dir / "yolo_boxes.json"
    if boxes_file.is_file():
        boxes = json.loads(boxes_file.read_text(encoding="utf-8")).get("boxes")
    return {
        "frame_id": frame_id,
        "bgr": bgr,
        "depth_mm": depth_mm,
        "intrinsics": intrinsics,
        "distortion": distortion,
        "boxes": boxes,
    }


# ----------------------------------------------------------------- 单帧
def analyze_frame(frame: dict[str, Any], cabinet_config: dict[str, Any],
                  anchor_params: dict[str, Any], model, conf: float) -> dict[str, Any]:
    boxes = frame["boxes"]
    if boxes is None:
        if model is None:
            raise ValueError("帧目录没有 yolo_boxes.json，且未指定 --model")
        boxes = run_yolo(model, frame["bgr"], conf)
    depth, bgr = frame["depth_mm"], frame["bgr"]
    wall_cloud = build_pointcloud(
        depth, bgr, frame["intrinsics"], [],
        stride=int(cabinet_config["params"].get("stride", 3)),
        z_min_m=Z_MIN_M, z_max_m=Z_MAX_M, max_points=350_000,
        dense_box_sampling=False, distortion=frame["distortion"],
    )
    wall_plane = build_cabinet_frame(
        cabinet_config, wall_cloud.positions, wall_cloud.pixels, depth.shape,
        depth_mm=depth, intrinsics=frame["intrinsics"], boxes=boxes,
    )
    cloud = build_pointcloud(
        depth, bgr, frame["intrinsics"], boxes,
        stride=CAPTURE_STRIDE, z_min_m=Z_MIN_M, z_max_m=Z_MAX_M,
        max_points=2_000_000, dense_box_sampling=True,
        box_padding_ratio=BOX_PADDING_RATIO, distortion=frame["distortion"],
    )
    reference = fit_panel_reference(cloud, boxes, depth.shape, wall_plane,
                                    anchor_params)
    knob_name = None
    try:
        _, knob_box = select_knob_detection(boxes)
        knob_name = str(knob_box.get("name"))
    except ValueError:
        pass
    origin = np.asarray(wall_plane["origin_camera_m"], dtype=np.float64)
    axes = np.asarray([wall_plane["x_axis_camera"], wall_plane["y_axis_camera"],
                       wall_plane["z_axis_camera"]], dtype=np.float64)
    ref_cam = np.asarray(reference["rectangle_center_camera_m"], dtype=np.float64)
    ref_wall = (ref_cam - origin) @ axes.T
    return {
        "boxes": boxes,
        "wall_plane": wall_plane,
        "reference": reference,
        "knob_name": knob_name,
        "origin": origin,
        "axes": axes,
        "reference_wall": ref_wall,
    }


# ----------------------------------------------------------------- 聚合
def _stats(samples: np.ndarray) -> dict[str, Any]:
    n = int(samples.shape[0])
    mean = samples.mean(axis=0)
    result: dict[str, Any] = {
        "count": n,
        "mean_mm": (mean * 1000.0).round(2).tolist(),
        "std_mm": ((samples.std(axis=0, ddof=1) if n > 1 else np.zeros(3)) * 1000.0)
        .round(2).tolist(),
        "max_abs_residual_mm": float(np.abs(samples - mean).max() * 1000.0) if n else 0.0,
    }
    if n > 1:
        loo = []
        for i in range(n):
            others = np.delete(samples, i, axis=0).mean(axis=0)
            loo.append(np.linalg.norm(samples[i] - others))
        result["leave_one_out_rmse_mm"] = float(np.sqrt(np.mean(np.square(loo))) * 1000.0)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, required=True, help="数据集会话目录")
    parser.add_argument("--annotations", type=Path, default=None,
                        help="annotations.jsonl（默认 <dataset>/annotations.jsonl）")
    parser.add_argument("--model", default=None,
                        help="YOLO .pt；帧目录没有 yolo_boxes.json 时才需要")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--registry", type=Path, default=reg.DEFAULT_REGISTRY_PATH,
                        help="能力注册表（取 cabinet_frame 配置与现有 target_model 参数）")
    parser.add_argument("--cabinet-frame-method", default=None,
                        help="覆盖注册表的柜面坐标系方法（默认按注册表）")
    parser.add_argument("--border-margin-px", type=int, default=None,
                        help="标定阶段面板框离边守卫（默认取模型参数）")
    parser.add_argument("--allow-single-slot", action="store_true",
                        help="只标了一个点位时也输出（锚点=该点，另一点位为 0；仅应急）")
    parser.add_argument("--write", action="store_true",
                        help="通过 18000 写入注册表 target_model（并切换为 panel_anchor）")
    parser.add_argument("--capability-url", default="http://127.0.0.1:18000")
    args = parser.parse_args()

    dataset = args.dataset.expanduser().resolve()
    session = load_session(dataset)
    annotations = load_annotations(args.annotations or dataset / "annotations.jsonl")
    if not annotations:
        print("[calib] 标注为空", file=sys.stderr)
        return 1

    registry = reg.load_registry(args.registry)
    cabinet_config = reg.cabinet_frame_config(registry)
    if args.cabinet_frame_method:
        from core.cabinet_frame_methods import validate_cabinet_frame_config

        cabinet_config = validate_cabinet_frame_config(
            {"method": args.cabinet_frame_method})
    existing = reg.target_model_config(registry)
    anchor_params = (dict(existing["params"]) if existing["method"] == PANEL_ANCHOR
                     else default_target_model_params(PANEL_ANCHOR))
    # 运行时参数（写回注册表的）与标定阶段分析参数分开：标定阶段不用尺寸
    # 守卫（尺寸正是要标的），离边守卫可临时覆盖
    runtime_params = dict(anchor_params)
    anchor_params["panel_size_mm"] = [0.0, 0.0]
    anchor_params["max_size_deviation_ratio"] = 0.0
    if args.border_margin_px is not None:
        anchor_params["border_margin_px"] = int(args.border_margin_px)

    model = None
    if args.model:
        from ultralytics import YOLO

        model = YOLO(args.model)

    print(f"[calib] 数据集 {dataset.name}：{len(annotations)} 帧有标注")
    print(f"[calib] 柜面坐标系方法 {cabinet_config['method']}")

    samples: dict[int, list[np.ndarray]] = {1: [], 3: []}
    sizes: list[tuple[float, float]] = []
    per_frame_rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for frame_id in sorted(annotations):
        points = annotations[frame_id]
        try:
            frame = load_frame(dataset, session, frame_id)
            analysis = analyze_frame(frame, cabinet_config, anchor_params,
                                     model, args.conf)
        except Exception as exc:  # 单帧失败不阻断
            failures.append({"frame_id": frame_id, "error": str(exc)})
            print(f"[calib] ✗ {frame_id}: {exc}")
            continue
        ref = analysis["reference"]
        sizes.append((float(ref["long_length_m"]) * 1000.0,
                      float(ref["short_length_m"]) * 1000.0))
        row: dict[str, Any] = {
            "frame_id": frame_id,
            "knob_detection": analysis["knob_name"],
            "cabinet_axis_estimation": analysis["wall_plane"].get("axis_estimation"),
            "panel_size_mm": [round(sizes[-1][0], 1), round(sizes[-1][1], 1)],
            "panel_conf": ref["detection"]["conf"],
            "offsets_mm": {},
            "warnings": [],
        }
        for slot, target_cam in points.items():
            if slot not in samples:
                row["warnings"].append(f"点位 {slot} 不参与标定（只用 1/3）")
                continue
            target_wall = (target_cam - analysis["origin"]) @ analysis["axes"].T
            delta = target_wall - analysis["reference_wall"]
            samples[slot].append(delta)
            row["offsets_mm"][str(slot)] = (delta * 1000.0).round(2).tolist()
            expected = SLOT_EXPECTED_KNOB[slot]
            if analysis["knob_name"] and analysis["knob_name"] != expected:
                row["warnings"].append(
                    f"点位 {slot} 通常对应「{expected}」，本帧 YOLO 是「{analysis['knob_name']}」")
        per_frame_rows.append(row)
        offsets_text = "  ".join(
            f"点{slot}[{v[0]:+.1f},{v[1]:+.1f},{v[2]:+.1f}]"
            for slot, v in row["offsets_mm"].items())
        warn = f"  ⚠ {'；'.join(row['warnings'])}" if row["warnings"] else ""
        print(f"[calib] ✓ {frame_id}  面板 {row['panel_size_mm'][0]:.0f}×"
              f"{row['panel_size_mm'][1]:.0f}mm  旋钮「{analysis['knob_name']}」  "
              f"{offsets_text}{warn}")

    stats = {slot: _stats(np.asarray(v)) for slot, v in samples.items() if v}
    if not stats:
        print("[calib] 没有任何有效样本", file=sys.stderr)
        return 1

    if 1 in stats and 3 in stats:
        t1 = np.asarray(stats[1]["mean_mm"]) / 1000.0
        t3 = np.asarray(stats[3]["mean_mm"]) / 1000.0
        anchor = (t1 + t3) / 2.0
        point1 = t1 - anchor
        point3 = t3 - anchor
        mode = "two-slot-midpoint"
    elif args.allow_single_slot:
        slot = 1 if 1 in stats else 3
        # 另一点位未知：锚点暂取该点本身，两个点位偏移都为 0；补标后重跑
        anchor = np.asarray(stats[slot]["mean_mm"]) / 1000.0
        point1 = np.zeros(3)
        point3 = np.zeros(3)
        mode = f"single-slot-{slot}（锚点=该点，另一点位为 0，需补标）"
    else:
        print("[calib] 只标了一个点位，锚点无法由中点定义；补标另一点位，"
              "或加 --allow-single-slot 应急输出", file=sys.stderr)
        return 1

    sizes_arr = np.asarray(sizes)
    panel_size = sizes_arr.mean(axis=0)
    params = dict(runtime_params)
    params.update({
        "anchor_offset_wall_mm": (anchor * 1000.0).round(2).tolist(),
        "point1_offset_wall_mm": (point1 * 1000.0).round(2).tolist(),
        "point3_offset_wall_mm": (point3 * 1000.0).round(2).tolist(),
        "panel_size_mm": panel_size.round(1).tolist(),
    })
    config = validate_target_model_config({"method": PANEL_ANCHOR, "params": params})

    report = {
        "dataset": str(dataset),
        "cabinet_frame": cabinet_config,
        "mode": mode,
        "slot_stats": {str(k): v for k, v in stats.items()},
        "panel_size_mm": {
            "mean": panel_size.round(1).tolist(),
            "std": (sizes_arr.std(axis=0, ddof=1) if len(sizes) > 1
                    else np.zeros(2)).round(1).tolist(),
        },
        "symmetry_check_mm": {
            "point1_y_should_be_near_0": round(float(point1[1] * 1000.0), 2),
            "point1_z_should_be_near_0": round(float(point1[2] * 1000.0), 2),
            "knob_half_span_x": round(float(abs(point1[0]) * 1000.0), 2),
        },
        "target_model": config,
        "frames": per_frame_rows,
        "failures": failures,
    }
    out = dataset / "panel_anchor_calibration.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    print()
    print(f"[calib] 模式：{mode}   失败帧 {len(failures)}")
    for slot, st in sorted(stats.items()):
        loo = st.get("leave_one_out_rmse_mm")
        print(f"[calib] 点{slot}：{st['count']} 帧  面板中心→点 均值(mm) {st['mean_mm']}  "
              f"std {st['std_mm']}  LOO-RMSE {loo if loo is None else round(loo, 2)}")
    print(f"[calib] 面板尺寸 {panel_size[0]:.1f}×{panel_size[1]:.1f} mm "
          f"(std {report['panel_size_mm']['std']})")
    print(f"[calib] anchor(mm) {config['params']['anchor_offset_wall_mm']}  "
          f"点1 {config['params']['point1_offset_wall_mm']}  "
          f"点3 {config['params']['point3_offset_wall_mm']}")
    sym = report["symmetry_check_mm"]
    print(f"[calib] 对称性：点1 相对锚点 y {sym['point1_y_should_be_near_0']:+.1f} / "
          f"z {sym['point1_z_should_be_near_0']:+.1f} mm（应接近 0），"
          f"半跨距 {sym['knob_half_span_x']:.1f} mm")
    print(f"[calib] 报告已写 {out}")
    print("[calib] 18000 target_model 配置：")
    print(json.dumps(config, ensure_ascii=False, indent=2))

    if args.write:
        import requests

        response = requests.post(f"{args.capability_url}/api/capability/target-model",
                                 json=config, timeout=10)
        payload = response.json()
        if not response.ok or not payload.get("ok"):
            print(f"[calib] 写入 18000 失败: {payload.get('error') or response.status_code}",
                  file=sys.stderr)
            return 1
        print("[calib] 已写入 18000 并切换为 panel_anchor；重启 7005 生效")
    else:
        print("[calib] 未写入（加 --write 直接写入 18000；或在 18000 页面粘贴上面 JSON）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
