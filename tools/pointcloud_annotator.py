#!/usr/bin/env python3
"""18006 点云点选页面：orbbec_rgbd_collector 的界面 + IK_replay 的柜面坐标系。

复用 Mac 端 orbbec_rgbd_collector（代码副本在 ../orbbec_rgbd_collector，不装
相机 SDK）的 17002 点云页面，直接读 7003「RGB-D 标定」落盘的
``data/calibration_datasets/``；但把页面里的柜面坐标系换成 IK_replay
``api.cabinet_frame.build_cabinet_frame`` 的结果——**默认按 18000 当前配置**
（方法 + 参数，与 7005 运行时一致），也可用 ``--cabinet-frame-method`` 或
运行时 ``POST /api/cabinet-frame {"method": ...}`` 切换（null = 回到 18000）。

原工具自己那套多平面分析仍会跑（结果放在 analysis.mac_plane 供对照），
页面展示、点选投影、annotations.jsonl 里记录的 plane 都用 IK_replay 的。

启动：``tools/run_pointcloud_annotator.sh``（或直接运行本脚本）。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

import numpy as np  # noqa: E402

import core.capability_registry as reg  # noqa: E402
from api.cabinet_frame import build_cabinet_frame  # noqa: E402
from api.cabinet_panel_anchor import (  # noqa: E402
    fit_panel_reference,
    panel_anchor_is_calibrated,
    predict_target_panel_anchor,
    select_knob_detection,
)
from api.pointcloud_core import build_pointcloud  # noqa: E402
from api.rgbd_dataset import DEFAULT_DATASET_ROOT, ExportSession  # noqa: E402
from core.cabinet_frame_methods import (  # noqa: E402
    CABINET_FRAME_METHOD_LABELS,
    CABINET_FRAME_METHODS,
    validate_cabinet_frame_config,
)
from core.target_models import PANEL_ANCHOR, default_target_model_params  # noqa: E402

DEFAULT_COLLECTOR = ROOT.parent / "orbbec_rgbd_collector"
Z_MIN_M, Z_MAX_M = 0.15, 3.0
FINDER_LIVE = "panel_anchor@18000"
FINDER_DRAFT = "panel_anchor@标定草稿"


class CabinetFrameProvider:
    """当前生效的柜面坐标系配置：命令行/运行时覆盖 > 18000 在线 > 注册表文件。"""

    def __init__(self, capability_url: str, registry_path: Path,
                 override_method: str | None) -> None:
        self.capability_url = capability_url
        self.registry_path = registry_path
        self.override_method = override_method
        self._lock = threading.Lock()

    def current(self) -> tuple[dict[str, Any], str]:
        if self.override_method:
            return (validate_cabinet_frame_config({"method": self.override_method}),
                    "手动指定（该方法默认参数）")
        registry, source = reg.load_registry_live(self.capability_url, self.registry_path)
        return reg.cabinet_frame_config(registry), f"18000 配置（{source}）"

    def set_override(self, method: str | None) -> None:
        if method is not None and method not in CABINET_FRAME_METHODS:
            raise ValueError(f"未知方法 {method!r}，可选 {' / '.join(CABINET_FRAME_METHODS)}")
        with self._lock:
            self.override_method = method

    def panel_anchor_params(self) -> dict[str, Any]:
        """panel_anchor 的运行时参数（18000 已配则用它，否则默认）；关掉尺寸守卫。"""
        registry, _ = reg.load_registry_live(self.capability_url, self.registry_path)
        model = reg.target_model_config(registry)
        params = (dict(model["params"]) if model["method"] == PANEL_ANCHOR
                  else default_target_model_params(PANEL_ANCHOR))
        params["panel_size_mm"] = [0.0, 0.0]
        params["max_size_deviation_ratio"] = 0.0
        return params

    def compute(self, data_dir: Path, session_id: str, frame_id: str,
                fallback_boxes: list[dict] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回 (柜面坐标系 plane, panel_anchor 参考点拟合)。后者失败时带 error。"""
        config, source = self.current()
        frame = ExportSession.open(data_dir / session_id).load_frame(frame_id)
        boxes = frame["boxes"] if frame["boxes"] is not None else (fallback_boxes or [])
        depth = frame["depth_mm"]
        cloud = build_pointcloud(
            depth, frame["bgr"], frame["intrinsics"], [],
            stride=int(config["params"].get("stride", 3)),
            z_min_m=Z_MIN_M, z_max_m=Z_MAX_M, max_points=350_000,
            dense_box_sampling=False, distortion=frame["distortion"],
        )
        plane = build_cabinet_frame(
            config, cloud.positions, cloud.pixels, depth.shape,
            depth_mm=depth, intrinsics=frame["intrinsics"], boxes=boxes,
        )
        # panel_anchor 的参考点：与 7005 / 标定脚本同一条路径（框内密采样点云）
        try:
            dense = build_pointcloud(
                depth, frame["bgr"], frame["intrinsics"], boxes,
                stride=4, z_min_m=Z_MIN_M, z_max_m=Z_MAX_M, max_points=2_000_000,
                dense_box_sampling=True, box_padding_ratio=0.1,
                distortion=frame["distortion"],
            )
            reference = fit_panel_reference(dense, boxes, depth.shape, plane,
                                            self.panel_anchor_params())
            origin = np.asarray(plane["origin_camera_m"])
            axes = np.asarray([plane["x_axis_camera"], plane["y_axis_camera"],
                               plane["z_axis_camera"]])
            center = np.asarray(reference["rectangle_center_camera_m"])
            reference["rectangle_center_wall_m"] = ((center - origin) @ axes.T).tolist()
        except Exception as exc:
            reference = {"available": False, "error": str(exc)}
        method = config["method"]
        label = CABINET_FRAME_METHOD_LABELS.get(method, method)
        # 页面按这几个字段决定文案/按钮：标成「已确认的自动结果」，让原工具的
        # 手动标定入口失效（坐标系由 IK_replay 决定，不在这里改）
        plane.update({
            "calibrated": True,
            "calibration_method": "accepted-automatic",
            "accepted_axis_estimation": (
                f"IK_replay {label} · {plane.get('axis_estimation', '?')} · {source}"),
            "plane_analysis_skipped": False,
            "cabinet_frame": {"method": method, "params": config["params"],
                              "source": source, "label": label},
        })
        plane.setdefault("rms_m", 0.0)
        plane.setdefault("sample_count", int(cloud.positions.shape[0]))
        plane.setdefault("inlier_ratio", 0.0)
        return plane, reference


def _take_route(app, path: str, method: str):
    for route in list(app.router.routes):
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or ()):
            app.router.routes.remove(route)
            return route.endpoint
    raise RuntimeError(f"原应用没有路由 {method} {path}")


_FRAME_LIST_ORIGINAL = """      const data = await json(`/api/sessions/${encodeURIComponent(session)}/frames`);
      $("frame").innerHTML = (data.frames || []).map(item =>
        `<option value="${escapeHtml(item.id)}">#${item.sequence} · ${escapeHtml(item.saved_at || "")} · 有效 ${((item.valid_ratio || 0) * 100).toFixed(1)}%</option>`
      ).join("");
"""

# 帧下拉里标出每帧已保存的点位（✅ 点1·点3 / 🟡 点1 / ⬜ 未标），并给出会话进度
_FRAME_LIST_PATCHED = """      const data = await json(`/api/sessions/${encodeURIComponent(session)}/frames`);
      {
        const sel = $("targetFinderVersion"), keep = sel.value;
        loadTargetFinderModels().then(() => {
          if (keep && [...sel.options].some(o => o.value === keep)) sel.value = keep;
        }).catch(() => {});
      }
      window.__ikAnnotated = {};
      try {
        const ann = await json(`/api/sessions/${encodeURIComponent(session)}/annotations`);
        for (const rec of (ann.annotations || [])) {
          const pts = rec.points && typeof rec.points === "object" ? rec.points : {"1": rec};
          window.__ikAnnotated[rec.frame_id] = Object.keys(pts).filter(k => Array.isArray(pts[k]?.target_camera_m)).sort();
        }
      } catch (e) {}
      const markOf = id => {
        const slots = window.__ikAnnotated[id];
        if (!slots || !slots.length) return "⬜ 未标";
        const full = slots.includes("1") && slots.includes("3");
        return (full ? "✅ " : "🟡 ") + slots.map(s => "点" + s).join("·");
      };
      $("frame").innerHTML = (data.frames || []).map(item =>
        `<option value="${escapeHtml(item.id)}">#${item.sequence} · ${markOf(item.id)} · ${escapeHtml((item.saved_at || "").slice(11, 19))} · 有效 ${((item.valid_ratio || 0) * 100).toFixed(1)}%</option>`
      ).join("");
      {
        const total = (data.frames || []).length;
        const done = (data.frames || []).filter(f => (window.__ikAnnotated[f.id] || []).length).length;
        const n1 = Object.values(window.__ikAnnotated).filter(s => s.includes("1")).length;
        const n3 = Object.values(window.__ikAnnotated).filter(s => s.includes("3")).length;
        const nRef = Object.values(window.__ikAnnotated).filter(s => s.some(k => Number(k) >= 4)).length;
        const label = $("frame").parentElement;
        if (label && label.tagName === "LABEL")
          label.firstChild.textContent = `帧（已标 ${done}/${total}：点1×${n1} 点3×${n3} 参考点×${nRef}）`;
      }
"""

_FINDER_MODELS_ORIGINAL = """      const data = await json("/api/target-finder/models");"""
_FINDER_MODELS_PATCHED = """      const data = await json("/api/target-finder/models?session_id=" + encodeURIComponent($("session").value || ""));"""

_SAVE_STATUS_ORIGINAL = """      setStatus(
        `点位 ${activePointSlot} 已保存`,"""
_SAVE_STATUS_PATCHED = """      refreshFrames(true).catch(() => {});
      setStatus(
        `点位 ${activePointSlot} 已保存`,"""


_PANEL_CSS_ORIGINAL = """    .yolo-panel-overlay .panel-plane {
      fill: rgba(94, 234, 212, .14); stroke: rgba(94, 234, 212, .85);
      stroke-width: 2; stroke-dasharray: 7 5;
    }
    .yolo-panel-overlay .panel-center {
      fill: #ff6bd6; stroke: rgba(255, 255, 255, .9); stroke-width: 2;
    }"""
_PANEL_CSS_PATCHED = """    .yolo-panel-overlay .panel-plane {
      fill: rgba(170, 90, 255, .12); stroke: rgba(190, 120, 255, .9);
      stroke-width: 2; stroke-dasharray: 7 5;
    }
    .yolo-panel-overlay .panel-center {
      fill: #a855f7; stroke: rgba(255, 255, 255, .95); stroke-width: 2;
    }"""

_PANEL_CHECK_ORIGINAL = """        <input id="yoloPanelFit" type="checkbox">
        YOLO Mask 内面板拟合 + 长短边（试验）"""
_PANEL_CHECK_PATCHED = """        <input id="yoloPanelFit" type="checkbox" checked>
        面板中心（panel_anchor 参考点，紫色）+ 矩形"""

# 分析面板里加一行：面板中心在墙面系的坐标（看逐帧稳定性就靠这个）
_PANEL_INFO_ORIGINAL = """      if (currentAnalysis?.yolo) {
        const instances = currentAnalysis.yolo.boxes;"""
_PANEL_INFO_PATCHED = """      if (currentAnalysis?.yolo_panel_fit) {
        const f = currentAnalysis.yolo_panel_fit;
        if (f.available && Array.isArray(f.rectangle_center_wall_m))
          lines.push(
            `面板中心（墙面系 mm）: ${f.rectangle_center_wall_m.map(v => (v * 1000).toFixed(1)).join(", ")}` +
            ` · 矩形 ${(f.long_length_m * 1000).toFixed(0)}×${(f.short_length_m * 1000).toFixed(0)} mm` +
            ` · 内点 ${(f.inlier_ratio * 100).toFixed(0)}%`);
        else if (!f.available)
          lines.push(`面板中心: 拟合失败（${f.error || "未知"}）`);
      }
      if (currentAnalysis?.yolo) {
        const instances = currentAnalysis.yolo.boxes;"""


def _patch_page(html: str) -> str:
    """页面小改：坐标系来源文案、帧列表标注状态、保存后刷新列表、面板中心叠加层。"""
    for original, patched in ((_FRAME_LIST_ORIGINAL, _FRAME_LIST_PATCHED),
                              (_SAVE_STATUS_ORIGINAL, _SAVE_STATUS_PATCHED),
                              (_FINDER_MODELS_ORIGINAL, _FINDER_MODELS_PATCHED),
                              (_PANEL_CSS_ORIGINAL, _PANEL_CSS_PATCHED),
                              (_PANEL_CHECK_ORIGINAL, _PANEL_CHECK_PATCHED),
                              (_PANEL_INFO_ORIGINAL, _PANEL_INFO_PATCHED)):
        if original not in html:
            print(f"[18006] ⚠ 页面代码与预期不一致，跳过一处补丁: {patched.strip()[:40]}…")
        html = html.replace(original, patched)
    return (html
            .replace("`坐标系来源: 已确认保存的自动结果（原来源 ` +", "`坐标系来源: ` +")
            .replace('`${p.accepted_axis_estimation || "未知"}）`', '`${p.accepted_axis_estimation || "未知"}`')
            # panel_anchor 的拟合结果没有原工具的颜色过滤/语义点数字段，缺省按 0 显示
            .replace("panel.excluded_point_count.toLocaleString()",
                     "(panel.excluded_point_count ?? 0).toLocaleString()")
            .replace("targetFinderPrediction.semantic_point_count\n                .toLocaleString()",
                     "(targetFinderPrediction.semantic_point_count ?? 0).toLocaleString()")
            .replace("<title>", "<title>IK_replay 18006 · "))


def create_app(collector: Path, data_dir: Path, model: Path | None, conf: float,
               provider: CabinetFrameProvider):
    sys.path.insert(0, str(collector / "src"))
    from rgbd_collector.offline_yolo import OfflineYolo
    from rgbd_collector.pointcloud_app import create_pointcloud_app

    web_root = collector / "web"
    app = create_pointcloud_app(
        data_dir, web_root=web_root,
        yolo=OfflineYolo(model, confidence=conf, device=None),
    )

    original_analyze = _take_route(app, "/api/analyze/{session_id}/{frame_id}", "POST")

    @app.post("/api/analyze/{session_id}/{frame_id}")
    def analyze(session_id: str, frame_id: str, body: dict | None = None):
        result = original_analyze(session_id, frame_id, body)
        analysis = result["analysis"]
        fallback_boxes = ((analysis.get("yolo") or {}).get("boxes")
                          if isinstance(analysis.get("yolo"), dict) else None)
        try:
            plane, reference = provider.compute(data_dir, session_id, frame_id,
                                                fallback_boxes)
        except Exception as exc:
            analysis["cabinet_frame_error"] = str(exc)
            mac = analysis.get("plane") or {}
            mac["accepted_axis_estimation"] = (
                f"IK_replay 柜面坐标系失败（{exc}），暂用原工具结果")
            mac["calibrated"] = True
            mac["calibration_method"] = "accepted-automatic"
            return result
        analysis["mac_plane"] = copy.deepcopy(analysis.get("plane"))
        analysis["plane"] = plane
        # 页面的「面板拟合」叠加层（矩形 + 中心点）改为显示 panel_anchor 参考点
        analysis["mac_yolo_panel_fit"] = analysis.get("yolo_panel_fit")
        analysis["yolo_panel_fit"] = reference
        return result

    # 「算法找点（Tab）」改用 IK_replay 的 panel_anchor：原工具的 0.x 版本按 Mac 端的
    # 类别名（远方/就地）与偏移写死，对 IK_replay 的旋钮左/右类别和坐标系不适用
    _take_route(app, "/api/target-finder/models", "GET")
    _take_route(app, "/api/target-finder/{session_id}/{frame_id}", "POST")

    def finder_models(session_id: str | None = None) -> list[dict[str, Any]]:
        models = []
        registry, source = reg.load_registry_live(provider.capability_url, provider.registry_path)
        live = reg.target_model_config(registry)
        live_ok = live["method"] == PANEL_ANCHOR and panel_anchor_is_calibrated(live["params"])
        models.append({
            "version": FINDER_LIVE, "params": live["params"] if live_ok else None,
            "name": ("18000 当前参数" if live_ok
                     else f"18000 当前不是已标定的 panel_anchor（{live['method']}）"),
            "source": source, "available": live_ok,
        })
        draft_path = (data_dir / session_id / "panel_anchor_calibration.json"
                      if session_id else None)
        draft = None
        if draft_path and draft_path.is_file():
            try:
                draft = json.loads(draft_path.read_text(encoding="utf-8"))["target_model"]["params"]
            except Exception:
                draft = None
        models.append({
            "version": FINDER_DRAFT, "params": draft, "available": draft is not None,
            "name": ("本会话标定草稿 panel_anchor_calibration.json" if draft
                     else "本会话还没有标定报告（先跑 tools/calibrate_panel_anchor.py）"),
        })
        for item in models:
            item.update({
                "algorithm": "ik_replay-panel-anchor",
                "reference_source": "yolo-panel-rectangle-center",
                "training_frame_count": "–",
                "allowed_requested_point_slots": [1, 3],
                "requires_saved_wall_coordinate": False,
                "target_point_slot": "detection-dependent（旋钮右→1，旋钮左→3）",
            })
        return models

    @app.get("/api/target-finder/models")
    def target_finder_model_list(session_id: str | None = None):
        models = finder_models(session_id)
        default = next((m["version"] for m in models if m["available"]), FINDER_LIVE)
        return {"ok": True, "models": models, "default_version": default}

    @app.post("/api/target-finder/{session_id}/{frame_id}")
    def find_target_one(session_id: str, frame_id: str, body: dict | None = None):
        options = body or {}
        version = str(options.get("version", FINDER_LIVE))
        requested = int(options.get("requested_point_slot", 1))
        try:
            model = next((m for m in finder_models(session_id) if m["version"] == version), None)
            if model is None:
                raise ValueError(f"未知找点算法版本: {version}")
            if not model["available"]:
                raise ValueError(model["name"])
            if requested not in (1, 3):
                raise ValueError(f"{version} 只响应点位槽 1、3（当前槽 {requested}）")
            session = ExportSession.open(data_dir / session_id)
            frame = session.load_frame(frame_id)
            plane, reference = provider.compute(data_dir, session_id, frame_id, None)
            if not reference.get("available"):
                raise ValueError(f"当前帧面板中心不可用：{reference.get('error', '?')}")
            _, knob = select_knob_detection(frame["boxes"])
            prediction = predict_target_panel_anchor(reference, str(knob.get("name", "")),
                                                     plane, model["params"])
            prediction.update({
                "model": {"version": version, "name": model["name"],
                          "training_frame_count": "–",
                          "params": model["params"]},
                "requested_point_slot": requested,
                "reference_source": "yolo-panel-rectangle-center",
                "panel_center_camera_m": reference["rectangle_center_camera_m"],
                "panel_center_wall_m": reference.get("rectangle_center_wall_m"),
            })
            slot = int(prediction["target_point_slot"])
            existing = None
            hit = (session.load_annotations().get(frame_id) or {}).get("points", {}).get(str(slot))
            if isinstance(hit, dict) and isinstance(hit.get("target_camera_m"), list):
                existing = np.asarray(hit["target_camera_m"], dtype=np.float64)
            if existing is not None:
                origin = np.asarray(plane["origin_camera_m"])
                axes = np.asarray([plane["x_axis_camera"], plane["y_axis_camera"],
                                   plane["z_axis_camera"]])
                ref_wall = (existing - origin) @ axes.T
                err = np.asarray(prediction["target_wall_m"]) - ref_wall
                prediction["validation"] = {
                    "reference_point_slot": slot,
                    "reference_target_camera_m": existing.tolist(),
                    "reference_target_wall_m": ref_wall.tolist(),
                    "prediction_minus_reference_wall_m": err.tolist(),
                    "error_distance_m": float(np.linalg.norm(err)),
                }
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (TypeError, ValueError, RuntimeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "prediction": prediction, "panel_fit": reference}

    _take_route(app, "/", "GET")
    page_html = _patch_page((web_root / "pointcloud.html").read_text(encoding="utf-8"))

    @app.get("/")
    def index():
        return HTMLResponse(page_html)

    @app.get("/api/cabinet-frame")
    def cabinet_frame_get():
        config, source = provider.current()
        return {"ok": True, "config": config, "source": source,
                "override_method": provider.override_method,
                "methods": list(CABINET_FRAME_METHODS),
                "labels": CABINET_FRAME_METHOD_LABELS}

    @app.post("/api/cabinet-frame")
    def cabinet_frame_set(body: dict):
        try:
            provider.set_override(body.get("method") or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return cabinet_frame_get()

    return app


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18006)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--collector", type=Path, default=DEFAULT_COLLECTOR,
                        help="orbbec_rgbd_collector 代码副本目录")
    parser.add_argument("--model", type=Path, default=ROOT / "models" / "Xuanniu_hhy.pt")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--capability-url", default="http://127.0.0.1:18000")
    parser.add_argument("--registry", type=Path, default=reg.DEFAULT_REGISTRY_PATH,
                        help="18000 不可达时兜底读取的注册表文件")
    parser.add_argument("--cabinet-frame-method", default=None,
                        choices=list(CABINET_FRAME_METHODS),
                        help="固定柜面坐标系方法（默认跟随 18000 配置）")
    args = parser.parse_args()

    if not (args.collector / "src" / "rgbd_collector").is_dir():
        print(f"缺少 {args.collector}/src/rgbd_collector；先从 macos-yx rsync 代码副本"
              "（见 tools/run_pointcloud_annotator.sh）", file=sys.stderr)
        return 1
    provider = CabinetFrameProvider(args.capability_url, args.registry,
                                    args.cabinet_frame_method)
    config, source = provider.current()
    model = args.model if args.model and args.model.is_file() else None
    app = create_app(args.collector, args.data_dir.expanduser().resolve(), model,
                     args.conf, provider)
    print(f"[18006] 数据目录: {args.data_dir.expanduser().resolve()}")
    print(f"[18006] 柜面坐标系: {config['method']} ← {source}")
    print(f"[18006] YOLO: {model or '未启用'}")
    print(f"[18006] 前端: http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
