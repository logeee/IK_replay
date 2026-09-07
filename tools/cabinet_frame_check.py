"""柜面坐标系构建结果核验（独立工具，不接入任何服务）。

从 18001 抓一帧头部 RGB-D，本地跑一次 YOLO，然后把**每一种**已登记的
柜面坐标系构建方法都算一遍，输出到 ``data/cabinet_frame_check/<时间戳>/``：

* ``index.html``   离线可打开的三维查看器（three.js 已内嵌）：彩色点云 +
                   各方法 X(红)/Y(绿)/Z(蓝) 三轴 + 数值表，可逐方法显隐
* ``overlay.jpg``  在 RGB 图上投影三轴与面板矩形
* ``cloud.ply``    彩色点云；``frame_<方法>.ply`` 各方法三轴（CloudCompare
                   直接拖入即可，多文件叠加）
* ``result.json``  各方法的完整返回值 / 错误信息与两两夹角

用法::

    python tools/cabinet_frame_check.py                # 抓帧并核验
    python tools/cabinet_frame_check.py --stride 2     # 更密的点云
    python tools/cabinet_frame_check.py --npz saved.npz   # 复用已保存的帧

每次运行同时把原始帧保存为 ``frame.npz``，便于换参数复算而不必重抓。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.cabinet_frame import build_cabinet_frame  # noqa: E402
from api.pointcloud_core import build_pointcloud  # noqa: E402
from core.cabinet_frame_methods import (  # noqa: E402
    CABINET_FRAME_METHOD_LABELS,
    CABINET_FRAME_METHODS,
    default_cabinet_frame_params,
)

VENDOR = ROOT / "web" / "vendor"
OUT_ROOT = ROOT / "data" / "cabinet_frame_check"
CAMERA_RIGHT = np.array([1.0, 0.0, 0.0])
CAMERA_UP = np.array([0.0, -1.0, 0.0])
AXIS_LEN_M = 0.15


# ----------------------------------------------------------------- 取帧
def fetch_snapshot(reach_base: str) -> dict[str, Any]:
    import requests

    response = requests.get(f"{reach_base}/api/reach/rgbd_snapshot",
                            timeout=(3.0, 20.0))
    response.raise_for_status()
    return load_npz(response.content)


def load_npz(payload: bytes) -> dict[str, Any]:
    with np.load(io.BytesIO(payload), allow_pickle=False) as archive:
        snapshot = {
            "jpeg": archive["jpeg"].astype(np.uint8).tobytes(),
            "depth_mm": archive["depth_mm"].astype(np.float32),
            "intrinsics": archive["intrinsics"].astype(np.float64),
            "distortion": (archive["distortion"].astype(np.float64)
                           if "distortion" in archive.files
                           else np.empty(0)),
            "T_cam2root": (archive["T_cam2root"].astype(np.float64)
                           if "T_cam2root" in archive.files else None),
            "metadata": json.loads(
                archive["metadata_json"].astype(np.uint8).tobytes()
                .decode("utf-8")),
        }
    return snapshot


def save_npz(path: Path, snapshot: dict[str, Any]) -> None:
    arrays = {
        "jpeg": np.frombuffer(snapshot["jpeg"], dtype=np.uint8),
        "depth_mm": snapshot["depth_mm"],
        "intrinsics": snapshot["intrinsics"],
        "distortion": snapshot["distortion"],
        "metadata_json": np.frombuffer(
            json.dumps(snapshot["metadata"]).encode("utf-8"), dtype=np.uint8),
    }
    if snapshot.get("T_cam2root") is not None:
        arrays["T_cam2root"] = snapshot["T_cam2root"]
    np.savez(path, **arrays)


def run_yolo(bgr: np.ndarray, model_path: Path, conf: float) -> list[dict]:
    from ultralytics import YOLO

    model = YOLO(str(model_path))
    names = model.names
    boxes: list[dict[str, Any]] = []
    for result in model.predict(bgr, conf=conf, verbose=False):
        if result.boxes is None:
            continue
        masks = getattr(getattr(result, "masks", None), "xy", None) or []
        for index, box in enumerate(result.boxes):
            cls = int(box.cls[0])
            detection: dict[str, Any] = {
                "cls": cls,
                "name": str(names.get(cls, cls)),
                "conf": round(float(box.conf[0]), 4),
                "xyxy": [round(float(v), 2) for v in box.xyxy[0].tolist()],
            }
            if index < len(masks):
                polygon = np.asarray(masks[index], dtype=np.float32)
                if polygon.ndim == 2 and polygon.shape[0] >= 3:
                    detection["polygon"] = polygon.round(2).tolist()
            boxes.append(detection)
    return boxes


# ----------------------------------------------------------------- 计算
def angle_deg(a, b) -> float:
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    cos = float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12)
    return float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))


def frame_axes(frame: dict[str, Any]) -> np.ndarray:
    return np.asarray([frame["x_axis_camera"], frame["y_axis_camera"],
                       frame["z_axis_camera"]], dtype=np.float64)


def summarize(frame: dict[str, Any], T_cam2root: np.ndarray | None) -> dict:
    axes = frame_axes(frame)
    summary = {
        "x_vs_camera_right_deg": angle_deg(axes[0], CAMERA_RIGHT),
        "z_vs_camera_up_deg": angle_deg(axes[2], CAMERA_UP),
        "y_vs_optical_axis_deg": angle_deg(axes[1], [0, 0, 1]),
        "orthogonality_max_abs": float(np.max(np.abs(axes @ axes.T - np.eye(3)))),
        "right_handed_det": float(np.linalg.det(axes)),
    }
    if T_cam2root is not None and T_cam2root.shape == (4, 4):
        R = T_cam2root[:3, :3]
        root_axes = (R @ axes.T).T
        summary["root_frame"] = {
            "x_axis_root": root_axes[0].round(5).tolist(),
            "y_axis_root": root_axes[1].round(5).tolist(),
            "z_axis_root": root_axes[2].round(5).tolist(),
            "z_vs_root_up_deg": angle_deg(root_axes[2], [0, 0, 1]),
            "x_vs_root_horizontal_deg": float(np.degrees(np.arcsin(
                np.clip(abs(root_axes[0][2]), 0, 1)))),
        }
    return summary


def compare(frames: dict[str, dict]) -> list[dict]:
    ids = [m for m in frames if frames[m].get("ok")]
    rows = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            aa = frame_axes(frames[a]["frame"])
            bb = frame_axes(frames[b]["frame"])
            rows.append({
                "a": a, "b": b,
                "x_deg": angle_deg(aa[0], bb[0]),
                "y_deg": angle_deg(aa[1], bb[1]),
                "z_deg": angle_deg(aa[2], bb[2]),
            })
    return rows


# ----------------------------------------------------------------- 输出
def project(points: np.ndarray, intrinsics, distortion) -> np.ndarray:
    fx, fy, cx, cy = [float(v) for v in intrinsics]
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], float)
    dist = np.asarray(distortion, float).reshape(-1)
    if dist.size not in {4, 5, 8, 12, 14}:
        dist = np.zeros(5)
    pts, _ = cv2.projectPoints(np.asarray(points, float).reshape(-1, 1, 3),
                               np.zeros(3), np.zeros(3), K, dist)
    return pts.reshape(-1, 2)


def anchor_point(frames: dict[str, dict]) -> np.ndarray:
    """三轴的公共画法锚点：优先面板中心（方法二），否则任一方法的 center。"""
    for method in ("method2_panel_edges", *frames):
        entry = frames.get(method)
        if entry and entry.get("ok"):
            return np.asarray(entry["frame"]["center_camera_m"], float)
    raise ValueError("没有任何方法成功")


def draw_overlay(bgr, boxes, frames, anchor, intrinsics, distortion) -> np.ndarray:
    image = bgr.copy()
    for box in boxes:
        x1, y1, x2, y2 = [int(round(v)) for v in box["xyxy"]]
        color = (0, 200, 255) if box.get("name") == "面板" else (140, 140, 140)
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
        cv2.putText(image, f"{_ascii_name(box.get('name'))} {box.get('conf', 0):.2f}",
                    (x1, max(20, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    color, 2)
        if box.get("polygon"):
            pts = np.asarray(box["polygon"], np.int32).reshape(-1, 1, 2)
            cv2.polylines(image, [pts], True, color, 1)
    styles = {}
    for index, method in enumerate(frames):
        styles[method] = index  # 0 实线，1 虚线，2 点线…
    legend_y = 40
    for method, entry in frames.items():
        if not entry.get("ok"):
            cv2.putText(image, f"{method}: FAIL {entry['error'][:60]}",
                        (20, legend_y), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (0, 0, 255), 2)
            legend_y += 30
            continue
        frame = entry["frame"]
        axes = frame_axes(frame)
        tips = project(anchor + AXIS_LEN_M * axes, intrinsics, distortion)
        base = project(anchor[None, :], intrinsics, distortion)[0]
        base_px = tuple(int(round(v)) for v in base)
        for tip, color in zip(tips, [(0, 0, 255), (0, 200, 0), (255, 80, 0)]):
            tip_px = tuple(int(round(v)) for v in tip)
            if styles[method] == 0:
                cv2.arrowedLine(image, base_px, tip_px, color, 3, tipLength=0.15)
            else:
                _dashed_line(image, base_px, tip_px, color, 3, styles[method])
        rect = (frame.get("panel_rectangle") or {}).get("corners_camera_m")
        if rect:
            corners = project(np.asarray(rect), intrinsics, distortion)
            cv2.polylines(image, [corners.astype(np.int32).reshape(-1, 1, 2)],
                          True, (60, 220, 60), 2)
        style_name = ["solid", "dashed", "dotted"][min(styles[method], 2)]
        cv2.putText(image, f"{method} ({style_name})", (20, legend_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        legend_y += 30
    cv2.putText(image, "X red  Y green  Z blue", (20, legend_y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return image


def _ascii_name(name) -> str:
    """cv2.putText 不支持中文，类别名换成 ASCII 显示。"""
    return {"面板": "panel", "旋钮左": "knob_L", "旋钮右": "knob_R",
            "远方就地左": "knob_L(old)", "远方就地右": "knob_R(old)"
            }.get(str(name), str(name))


def _dashed_line(image, p1, p2, color, thickness, style) -> None:
    p1 = np.asarray(p1, float)
    p2 = np.asarray(p2, float)
    length = float(np.linalg.norm(p2 - p1))
    dash = 18.0 if style == 1 else 6.0
    count = max(1, int(length / dash))
    for k in range(0, count, 2):
        a = p1 + (p2 - p1) * (k / count)
        b = p1 + (p2 - p1) * (min(k + 1, count) / count)
        cv2.line(image, tuple(a.round().astype(int)),
                 tuple(b.round().astype(int)), color, thickness)
    cv2.circle(image, tuple(p2.round().astype(int)), 6, color, -1)


def write_ply(path: Path, positions: np.ndarray, colors: np.ndarray) -> None:
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {positions.shape[0]}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    )
    record = np.zeros(positions.shape[0], dtype=[
        ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
        ("r", "u1"), ("g", "u1"), ("b", "u1")])
    record["x"], record["y"], record["z"] = positions.T.astype("<f4")
    record["r"], record["g"], record["b"] = colors.T.astype("u1")
    with path.open("wb") as handle:
        handle.write(header.encode("ascii"))
        handle.write(record.tobytes())


def axis_points(anchor: np.ndarray, axes: np.ndarray, *, step_m: float = 0.001,
                radius_m: float = 0.002) -> tuple[np.ndarray, np.ndarray]:
    """三轴画成细圆柱状的点，X红 Y绿 Z蓝，CloudCompare 里一眼可辨。"""
    rng = np.random.default_rng(0)
    pos, col = [], []
    for axis, color in zip(axes, [(255, 0, 0), (0, 200, 0), (40, 90, 255)]):
        t = np.arange(0.0, AXIS_LEN_M, step_m)
        core = anchor + t[:, None] * axis
        jitter = rng.normal(size=(t.size, 3)) * radius_m
        jitter -= (jitter @ axis)[:, None] * axis
        pos.append(core + jitter)
        col.append(np.tile(color, (t.size, 1)))
    return np.vstack(pos), np.vstack(col)


def write_html(path: Path, cloud_positions, cloud_rgb, frames, anchor,
               summary: dict, image_jpeg: bytes) -> None:
    three_src = (VENDOR / "three.module.js").read_text(encoding="utf-8")
    orbit_src = (VENDOR / "OrbitControls.js").read_text(encoding="utf-8")
    # 相机系 (x右,y下,z前) → three (x右,y上,z朝观察者)：(x, -y, -z)
    display = cloud_positions * np.array([1.0, -1.0, -1.0])
    pos_b64 = base64.b64encode(display.astype("<f4").tobytes()).decode()
    rgb_b64 = base64.b64encode(cloud_rgb.astype(np.uint8).tobytes()).decode()
    frame_payload = {}
    for method, entry in frames.items():
        if entry.get("ok"):
            f = entry["frame"]
            frame_payload[method] = {
                "ok": True,
                "label": CABINET_FRAME_METHOD_LABELS.get(method, method),
                "axes": frame_axes(f).tolist(),
                "origin": f["origin_camera_m"],
                "center": f["center_camera_m"],
                "rect": (f.get("panel_rectangle") or {}).get("corners_camera_m"),
                "axis_estimation": f.get("axis_estimation"),
                "summary": summary["methods"][method],
            }
        else:
            frame_payload[method] = {
                "ok": False,
                "label": CABINET_FRAME_METHOD_LABELS.get(method, method),
                "error": entry["error"],
            }
    data = {
        "frames": frame_payload,
        "anchor": anchor.tolist(),
        "pairs": summary["pairs"],
        "point_count": int(cloud_positions.shape[0]),
        "captured_at": summary["captured_at"],
        "image": "data:image/jpeg;base64,"
                 + base64.b64encode(image_jpeg).decode(),
    }
    html = HTML_TEMPLATE.replace("__THREE_SRC__", _escape_script(three_src))
    html = html.replace("__ORBIT_SRC__", _escape_script(orbit_src))
    html = html.replace("__DATA_JSON__", _escape_script(
        json.dumps(data, ensure_ascii=False)))
    html = html.replace("__POS_B64__", pos_b64).replace("__RGB_B64__", rgb_b64)
    path.write_text(html, encoding="utf-8")


def _escape_script(text: str) -> str:
    return text.replace("</script", "<\\/script")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>柜面坐标系核验</title>
<style>
 body{margin:0;background:#111;color:#ddd;font:13px/1.5 system-ui,sans-serif;display:flex;height:100vh}
 #view{flex:1;position:relative}
 canvas{display:block}
 #side{width:420px;overflow:auto;padding:12px;background:#1b1b1b;border-left:1px solid #333}
 h2{font-size:15px;margin:10px 0 6px}
 table{border-collapse:collapse;width:100%;font-size:12px}
 td,th{border:1px solid #333;padding:3px 6px;text-align:right;white-space:nowrap}
 th{text-align:left;color:#9cf}
 .fail{color:#f66}
 .legend span{display:inline-block;width:14px;height:3px;margin:0 4px}
 label{display:block;margin:4px 0}
 img{width:100%;border:1px solid #333;margin-top:6px}
 #hint{position:absolute;left:10px;bottom:8px;color:#888;font-size:12px}
</style></head><body>
<div id="view"><div id="hint">左键旋转 · 右键平移 · 滚轮缩放 · 视角：相机系 X右 / Y下 / Z前（显示时翻成 Y上 Z朝屏外）</div></div>
<div id="side">
 <h2>柜面坐标系核验 <small id="ts"></small></h2>
 <div class="legend"><span style="background:#f33"></span>X <span style="background:#3d3"></span>Y <span style="background:#48f"></span>Z ｜ 实线=第1个方法，虚线=第2个</div>
 <div id="toggles"></div>
 <label><input type="checkbox" id="showCloud" checked> 显示点云 (<span id="pc"></span> 点)</label>
 <label>点大小 <input type="range" id="psize" min="1" max="8" value="2"></label>
 <div id="tables"></div>
 <h2>RGB 叠加图</h2><img id="overlay">
</div>
<script type="text/plain" id="three-src">__THREE_SRC__</script>
<script type="text/plain" id="orbit-src">__ORBIT_SRC__</script>
<script type="text/plain" id="data-json">__DATA_JSON__</script>
<script type="text/plain" id="pos-b64">__POS_B64__</script>
<script type="text/plain" id="rgb-b64">__RGB_B64__</script>
<script>
window.addEventListener('error', (e) => { document.getElementById('tables').insertAdjacentHTML('afterbegin', `<div class="fail">脚本错误：${e.message}</div>`); });
window.addEventListener('unhandledrejection', (e) => { document.getElementById('tables').insertAdjacentHTML('afterbegin', `<div class="fail">加载错误：${e.reason && (e.reason.stack || e.reason)}</div>`); });
</script>
<script type="module">
const blob = (t) => URL.createObjectURL(new Blob([t], {type: 'text/javascript'}));
const threeUrl = blob(document.getElementById('three-src').textContent);
const orbitSrc = document.getElementById('orbit-src').textContent.replace(/from\s+['"]three['"]/g, `from '${threeUrl}'`);
const THREE = await import(threeUrl);
const { OrbitControls } = await import(blob(orbitSrc));
const DATA = JSON.parse(document.getElementById('data-json').textContent);
const b64 = (id) => { const s = atob(document.getElementById(id).textContent); const a = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) a[i] = s.charCodeAt(i); return a; };
const posBytes = b64('pos-b64'); const rgbBytes = b64('rgb-b64');
const positions = new Float32Array(posBytes.buffer, posBytes.byteOffset, posBytes.byteLength / 4);
const colors = new Float32Array(rgbBytes.length); for (let i = 0; i < rgbBytes.length; i++) colors[i] = rgbBytes[i] / 255;
const toDisp = (v) => new THREE.Vector3(v[0], -v[1], -v[2]);

const view = document.getElementById('view');
const scene = new THREE.Scene(); scene.background = new THREE.Color(0x111111);
const camera = new THREE.PerspectiveCamera(55, 1, 0.01, 50);
const anchor = toDisp(DATA.anchor);
camera.position.set(0, 0, 0); camera.lookAt(anchor);

const geom = new THREE.BufferGeometry();
geom.setAttribute('position', new THREE.BufferAttribute(positions, 3));
geom.setAttribute('color', new THREE.BufferAttribute(colors, 3));
const pmat = new THREE.PointsMaterial({size: 0.002, vertexColors: true, sizeAttenuation: true});
const cloud = new THREE.Points(geom, pmat); scene.add(cloud);
document.getElementById('pc').textContent = DATA.point_count.toLocaleString();
document.getElementById('ts').textContent = DATA.captured_at;
document.getElementById('overlay').src = DATA.image;
document.getElementById('showCloud').onchange = (e) => cloud.visible = e.target.checked;
document.getElementById('psize').oninput = (e) => pmat.size = 0.001 * Number(e.target.value);

const AXIS_COLORS = [0xff3333, 0x33dd33, 0x4488ff];
const groups = {};
const toggles = document.getElementById('toggles');
const tables = document.getElementById('tables');
let idx = 0;
for (const [method, f] of Object.entries(DATA.frames)) {
  const group = new THREE.Group(); groups[method] = group; scene.add(group);
  const label = document.createElement('label');
  if (!f.ok) {
    label.innerHTML = `<input type="checkbox" disabled> <span class="fail">${f.label}：失败</span>`;
    toggles.appendChild(label);
    tables.insertAdjacentHTML('beforeend', `<h2 class="fail">${f.label}</h2><div class="fail">${f.error}</div>`);
    idx++; continue;
  }
  const dashed = idx % 2 === 1;
  for (let a = 0; a < 3; a++) {
    const end = anchor.clone().add(toDisp(f.axes[a]).multiplyScalar(0.15));
    const g = new THREE.BufferGeometry().setFromPoints([anchor, end]);
    const m = dashed
      ? new THREE.LineDashedMaterial({color: AXIS_COLORS[a], dashSize: 0.008, gapSize: 0.005})
      : new THREE.LineBasicMaterial({color: AXIS_COLORS[a]});
    const line = new THREE.Line(g, m); if (dashed) line.computeLineDistances();
    group.add(line);
    const tip = new THREE.Mesh(new THREE.SphereGeometry(dashed ? 0.004 : 0.006, 12, 12), new THREE.MeshBasicMaterial({color: AXIS_COLORS[a]}));
    tip.position.copy(end); group.add(tip);
  }
  const origin = new THREE.Mesh(new THREE.SphereGeometry(0.005, 12, 12), new THREE.MeshBasicMaterial({color: dashed ? 0xffff00 : 0xff00ff}));
  origin.position.copy(toDisp(f.origin)); group.add(origin);
  if (f.rect) {
    const pts = f.rect.map(toDisp); pts.push(pts[0]);
    group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({color: 0x66ff66})));
  }
  label.innerHTML = `<input type="checkbox" checked> ${f.label} <small>(${dashed ? '虚线' : '实线'}，${f.axis_estimation}；原点球 ${dashed ? '黄' : '紫'})</small>`;
  label.querySelector('input').onchange = (e) => group.visible = e.target.checked;
  toggles.appendChild(label);
  const s = f.summary; const fmt = (v) => Number(v).toFixed(2);
  let rows = `<tr><th>X 轴 (相机系)</th><td>${f.axes[0].map(fmt).join(', ')}</td></tr>
  <tr><th>Y 轴 (相机系)</th><td>${f.axes[1].map(fmt).join(', ')}</td></tr>
  <tr><th>Z 轴 (相机系)</th><td>${f.axes[2].map(fmt).join(', ')}</td></tr>
  <tr><th>X 与画面水平夹角</th><td>${fmt(s.x_vs_camera_right_deg)}°</td></tr>
  <tr><th>Z 与画面竖直夹角</th><td>${fmt(s.z_vs_camera_up_deg)}°</td></tr>
  <tr><th>Y 与光轴夹角</th><td>${fmt(s.y_vs_optical_axis_deg)}°</td></tr>
  <tr><th>正交误差 / 行列式</th><td>${Number(s.orthogonality_max_abs).toExponential(1)} / ${fmt(s.right_handed_det)}</td></tr>`;
  if (s.root_frame) {
    rows += `<tr><th>Z 与躯干竖直夹角</th><td>${fmt(s.root_frame.z_vs_root_up_deg)}°</td></tr>
    <tr><th>X 相对躯干水平面倾角</th><td>${fmt(s.root_frame.x_vs_root_horizontal_deg)}°</td></tr>
    <tr><th>Z 轴 (躯干系)</th><td>${s.root_frame.z_axis_root.map(fmt).join(', ')}</td></tr>`;
  }
  tables.insertAdjacentHTML('beforeend', `<h2>${f.label}</h2><table>${rows}</table>`);
  idx++;
}
if (DATA.pairs.length) {
  let rows = '';
  for (const p of DATA.pairs) rows += `<tr><th style="white-space:normal">${p.a}<br>↔ ${p.b}</th><td>X ${p.x_deg.toFixed(2)}°<br>Y ${p.y_deg.toFixed(2)}°<br>Z ${p.z_deg.toFixed(2)}°</td></tr>`;
  tables.insertAdjacentHTML('beforeend', `<h2>方法间夹角</h2><table style="white-space:normal">${rows}</table>`);
}
// 相机本体三轴（细，灰白），便于对照
const camAxes = new THREE.AxesHelper(0.05); scene.add(camAxes);

// 表格填完再起 WebGL：没有 WebGL 的环境仍能看数值和叠加图
let renderer;
try { renderer = new THREE.WebGLRenderer({antialias: true}); }
catch (err) { view.insertAdjacentHTML('afterbegin', `<div class="fail" style="padding:20px">WebGL 不可用：${err.message}<br>右侧数值与叠加图仍有效；三维查看请换支持 WebGL 的浏览器。</div>`); }
if (renderer) {
  renderer.setPixelRatio(window.devicePixelRatio); view.appendChild(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement); controls.target.copy(anchor); controls.update();
  const resize = () => { const w = view.clientWidth, h = view.clientHeight; renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix(); };
  window.addEventListener('resize', resize); resize();
  (function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
}
</script></body></html>
"""


# ----------------------------------------------------------------- 主流程
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reach-base", default="http://127.0.0.1:18001")
    parser.add_argument("--npz", type=Path, help="复用已保存的 frame.npz")
    parser.add_argument("--model", type=Path,
                        default=ROOT / "models" / "Xuanniu_hhy.pt")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--stride", type=int, default=3,
                        help="柜面拟合点云步长（与 7005 方法一默认一致）")
    parser.add_argument("--html-max-points", type=int, default=250_000)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = args.out or (OUT_ROOT / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.npz:
        snapshot = load_npz(args.npz.read_bytes())
        print(f"[check] 复用帧 {args.npz}")
    else:
        print(f"[check] 从 {args.reach_base} 抓取 RGB-D…")
        snapshot = fetch_snapshot(args.reach_base)
    save_npz(out_dir / "frame.npz", snapshot)

    bgr = cv2.imdecode(np.frombuffer(snapshot["jpeg"], np.uint8),
                       cv2.IMREAD_COLOR)
    if bgr is None:
        print("[check] JPEG 解码失败")
        return 1
    print(f"[check] YOLO {args.model.name} conf={args.conf} …")
    boxes = run_yolo(bgr, args.model, args.conf)
    print("[check] 检出:", ", ".join(
        f"{b['name']}({b['conf']:.2f}{',mask' if 'polygon' in b else ''})"
        for b in boxes) or "无")

    cloud = build_pointcloud(
        snapshot["depth_mm"], bgr, snapshot["intrinsics"], [],
        stride=args.stride, z_min_m=0.15, z_max_m=3.0, max_points=350_000,
        dense_box_sampling=False, distortion=snapshot["distortion"],
    )
    print(f"[check] 整帧点云 {cloud.count:,} 点（stride {args.stride}）")

    T_cam2root = snapshot.get("T_cam2root")
    frames: dict[str, dict[str, Any]] = {}
    for method in CABINET_FRAME_METHODS:
        params = default_cabinet_frame_params(method)
        if "stride" in params:
            params["stride"] = args.stride
        config = {"method": method, "params": params}
        started = time.perf_counter()
        try:
            frame = build_cabinet_frame(
                config, cloud.positions, cloud.pixels,
                snapshot["depth_mm"].shape, depth_mm=snapshot["depth_mm"],
                intrinsics=snapshot["intrinsics"], boxes=boxes,
            )
            frames[method] = {"ok": True, "frame": frame,
                              "elapsed_ms": (time.perf_counter() - started) * 1e3}
            print(f"[check] {method}: OK ({frames[method]['elapsed_ms']:.0f} ms) "
                  f"X={np.round(frame['x_axis_camera'], 3).tolist()} "
                  f"Z={np.round(frame['z_axis_camera'], 3).tolist()}")
        except (TypeError, ValueError, np.linalg.LinAlgError) as exc:
            frames[method] = {"ok": False, "error": str(exc),
                              "elapsed_ms": (time.perf_counter() - started) * 1e3}
            print(f"[check] {method}: 失败 — {exc}")

    summary = {
        "captured_at": stamp,
        "source": snapshot["metadata"],
        "boxes": boxes,
        "stride": args.stride,
        "T_cam2root": None if T_cam2root is None else T_cam2root.tolist(),
        "methods": {
            m: (summarize(e["frame"], T_cam2root) if e["ok"] else None)
            for m, e in frames.items()
        },
        "pairs": compare(frames),
        "results": {
            m: ({"ok": True, "elapsed_ms": e["elapsed_ms"], **e["frame"]}
                if e["ok"] else e)
            for m, e in frames.items()
        },
    }
    (out_dir / "result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8")
    for row in summary["pairs"]:
        print(f"[check] {row['a']} ↔ {row['b']}: X {row['x_deg']:.2f}°  "
              f"Y {row['y_deg']:.2f}°  Z {row['z_deg']:.2f}°")

    if not any(e["ok"] for e in frames.values()):
        print(f"[check] 所有方法都失败，只保存了 frame.npz / result.json → {out_dir}")
        return 2

    anchor = anchor_point(frames)
    overlay = draw_overlay(bgr, boxes, frames, anchor, snapshot["intrinsics"],
                           snapshot["distortion"])
    cv2.imwrite(str(out_dir / "overlay.jpg"), overlay,
                [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    _, overlay_jpeg = cv2.imencode(".jpg", overlay,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 75])

    write_ply(out_dir / "cloud.ply", cloud.positions, cloud.rgb)
    for method, entry in frames.items():
        if entry["ok"]:
            pos, col = axis_points(anchor, frame_axes(entry["frame"]))
            write_ply(out_dir / f"frame_{method}.ply", pos, col)

    positions, rgb = cloud.positions, cloud.rgb
    if positions.shape[0] > args.html_max_points:
        pick = np.random.default_rng(0).choice(
            positions.shape[0], args.html_max_points, replace=False)
        positions, rgb = positions[pick], rgb[pick]
    write_html(out_dir / "index.html", positions, rgb, frames, anchor,
               summary, overlay_jpeg.tobytes())
    print(f"[check] 输出目录: {out_dir}")
    print(f"[check]   浏览器打开: file://{out_dir / 'index.html'}")
    print(f"[check]   CloudCompare: cloud.ply + frame_*.ply")
    return 0


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    raise TypeError(str(type(value)))


if __name__ == "__main__":
    sys.exit(main())
