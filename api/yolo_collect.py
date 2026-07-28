"""7003 YOLO 取点样本采集台。

目的：学习「YOLO 框 → 真实取点」的映射。正视时点在框内的相对位置固定，
斜视时会随距离/偏航角系统性漂移——采样本把这个规律拟合出来。

两种采集节奏（页面上都有）：
    · 拍并点：冻结一帧 → 点目标 → 保存。上下文最准，适合零散补样。
    · 只拍不点：按一下立即落盘（clicks 留空），几秒一张快速过完所有
      距离×角度组合，机器人尽早收工；之后进「补标注」模式逐张点击补全。

每个样本（一张图）记录：
    · 相机帧（jpg，存 yolo_samples/images/）
    · 采集时刻的 distance_m / yaw_err_deg / pitch_err_deg（问 reach_server
      的 /perpendicular，和对中用的是同一套测量）
    · YOLO 框（给了 --model 且装了 ultralytics 时实时推理；没给也能采，
      图像都在，之后可离线补跑推理补框）
    · 点击的真实目标点（可多个；保存时自动关联包含它的框）

启动：
    /home/robot/miniconda3/envs/yolo/bin/python -m api.yolo_collect \
        --model skip_yolo_file/Xuanniu.pt --conf 0.25

网页：http://<机器人IP>:7003/

落盘格式（yolo_samples/samples_<日期>.jsonl，每行一个样本）：
    {"id": "...", "ts": "...", "image": "images/xxx.jpg", "w": 1280, "h": 720,
     "distance_m": 0.54, "yaw_err_deg": -3.2, "pitch_err_deg": 1.0,
     "dmin": 0.4, "dmax": 1.0,
     "boxes": [{"cls": 0, "name": "switch", "conf": 0.91,
                "xyxy": [x1, y1, x2, y2]}, ...],
     "clicks": [{"u": 1206, "v": 638, "box_index": 0,
                 "au": 0.48, "av": 0.71}, ...],   # au/av = 点在框内的归一化位置
     "model": "Xuanniu.pt"}
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

app = FastAPI(title="yolo-collect")

_reach_base = "http://127.0.0.1:8001"
_samples_dir = Path(__file__).resolve().parent.parent / "yolo_samples"
_model = None          # ultralytics.YOLO 实例（可选）
_model_name = ""
_model_conf = 0.25

_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}   # id → {"jpeg": bytes, ...}（拍并点模式的暂存）


# --------------- 相机 ---------------


@app.get("/cam")
def cam():
    """直播：服务器端代理 reach_server 的 MJPEG 流（同确认台）。"""
    try:
        upstream = requests.get(f"{_reach_base}/api/reach/stream",
                                stream=True, timeout=(3.0, None))
        upstream.raise_for_status()
    except requests.RequestException as exc:
        return Response(f"相机流不可达（{_reach_base}）: {exc}",
                        status_code=502, media_type="text/plain")

    def gen():
        try:
            yield from upstream.iter_content(chunk_size=65536)
        finally:
            upstream.close()

    return StreamingResponse(
        gen(),
        media_type=upstream.headers.get(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame"),
        headers={"Cache-Control": "no-cache"})


def _grab_jpeg(timeout_s: float = 5.0) -> bytes:
    """从 MJPEG 流里抓一帧完整 JPEG（SOI 0xFFD8 … EOI 0xFFD9）。"""
    r = requests.get(f"{_reach_base}/api/reach/stream", stream=True,
                     timeout=(3.0, timeout_s))
    try:
        r.raise_for_status()
        buf = b""
        deadline = time.monotonic() + timeout_s
        for chunk in r.iter_content(chunk_size=16384):
            buf += chunk
            start = buf.find(b"\xff\xd8")
            if start >= 0:
                end = buf.find(b"\xff\xd9", start + 2)
                if end >= 0:
                    return buf[start:end + 2]
            if time.monotonic() > deadline:
                break
            if len(buf) > 8 * 1024 * 1024:      # 防呆：不该有这么大的帧
                buf = buf[-2 * 1024 * 1024:]
        raise RuntimeError(f"{timeout_s}s 内没抓到完整帧")
    finally:
        r.close()


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """纯 Python 读 JPEG 尺寸（SOF 段），不依赖 cv2。失败返回 (0, 0)。"""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        i += 2 + length
    return 0, 0


def _measure(dmin: float, dmax: float) -> dict:
    """采集时刻的距离/角度（reach_server /perpendicular，失败字段置 None）。"""
    out = {"distance_m": None, "yaw_err_deg": None,
           "pitch_err_deg": None, "tilt_deg": None}
    try:
        r = requests.get(f"{_reach_base}/api/reach/perpendicular",
                         params={"dmin": dmin, "dmax": dmax}, timeout=8.0)
        fit = r.json()
        if fit.get("ok"):
            for k in out:
                if fit.get(k) is not None:
                    out[k] = round(float(fit[k]), 3)
    except Exception:
        pass
    return out


def _infer(jpeg: bytes) -> list[dict]:
    """YOLO 推理（没配模型返回空列表）。"""
    if _model is None:
        return []
    import numpy as np

    import cv2
    img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    results = _model.predict(img, conf=_model_conf, verbose=False)
    boxes = []
    for r in results:
        names = r.names or {}
        for b in r.boxes:
            xyxy = [round(float(v), 1) for v in b.xyxy[0].tolist()]
            cls = int(b.cls[0])
            boxes.append({"cls": cls, "name": str(names.get(cls, cls)),
                          "conf": round(float(b.conf[0]), 3), "xyxy": xyxy})
    return boxes


def _capture(dmin: float, dmax: float) -> dict:
    """抓帧 + 测量 + 推理，拍并点/只拍两条路共用。"""
    jpeg = _grab_jpeg()
    meta = _measure(dmin, dmax)
    meta.update({"dmin": dmin, "dmax": dmax})
    try:
        boxes = _infer(jpeg)
    except Exception as exc:
        boxes = []
        meta["infer_error"] = str(exc)
    return {"jpeg": jpeg, "meta": meta, "boxes": boxes}


# --------------- 样本落盘 / 读写 ---------------


def _attach_box(click: dict, boxes: list[dict]) -> dict:
    """把点击关联到框：优先「包含它的最小框」，其次最近中心的框。"""
    u, v = click["u"], click["v"]
    best_i, best_area = None, None
    for i, b in enumerate(boxes):
        x1, y1, x2, y2 = b["xyxy"]
        if x1 <= u <= x2 and y1 <= v <= y2:
            area = (x2 - x1) * (y2 - y1)
            if best_area is None or area < best_area:
                best_i, best_area = i, area
    if best_i is None and boxes:
        best_i = min(range(len(boxes)), key=lambda i: (
            ((boxes[i]["xyxy"][0] + boxes[i]["xyxy"][2]) / 2 - u) ** 2
            + ((boxes[i]["xyxy"][1] + boxes[i]["xyxy"][3]) / 2 - v) ** 2))
    out = {"u": int(u), "v": int(v), "box_index": best_i}
    if best_i is not None:
        x1, y1, x2, y2 = boxes[best_i]["xyxy"]
        if x2 > x1 and y2 > y1:
            # 点在框内的归一化位置——拟合映射就用它当因变量
            out["au"] = round((u - x1) / (x2 - x1), 4)
            out["av"] = round((v - y1) / (y2 - y1), 4)
    return out


def _write_sample(jpeg: bytes, meta: dict, boxes: list[dict],
                  clicks: list[dict], w: int = 0, h: int = 0) -> dict:
    if not (w and h):
        w, h = _jpeg_size(jpeg)
    sid = uuid.uuid4().hex[:10]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    img_rel = f"images/{stamp}_{sid}.jpg"
    (_samples_dir / "images").mkdir(parents=True, exist_ok=True)
    (_samples_dir / img_rel).write_bytes(jpeg)
    record = {
        "id": sid,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "image": img_rel, "w": w, "h": h,
        **meta,
        "boxes": boxes,
        "clicks": [_attach_box({"u": int(c["u"]), "v": int(c["v"])}, boxes)
                   for c in clicks],
        "model": _model_name or None,
    }
    path = _samples_dir / f"samples_{datetime.now().strftime('%Y%m%d')}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _iter_samples() -> list[tuple[Path, list[dict]]]:
    """[(文件, [记录...]), ...]，按文件名（日期）排序。"""
    out = []
    for p in sorted(_samples_dir.glob("samples_*.jsonl")):
        records = []
        try:
            for line in p.open(encoding="utf-8"):
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            continue
        out.append((p, records))
    return out


def _counts() -> dict:
    total = unann = 0
    for _, records in _iter_samples():
        total += len(records)
        unann += sum(1 for r in records if not r.get("clicks"))
    return {"total": total, "unannotated": unann}


# --------------- 采集接口 ---------------


@app.post("/api/collect/snap")
def collect_snap(body: dict):
    """拍并点：帧暂存内存，等点击后 /save 才落盘。"""
    try:
        cap = _capture(float(body.get("dmin", 0.4)), float(body.get("dmax", 1.0)))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"抓帧失败: {exc}"},
                            status_code=502)
    sid = uuid.uuid4().hex[:10]
    with _lock:
        _pending[sid] = {**cap, "created": time.time()}
        # 只留最近几张没保存的，防止内存越积越多
        for old in sorted(_pending, key=lambda k: _pending[k]["created"])[:-5]:
            _pending.pop(old, None)
    return {"ok": True, "id": sid, "boxes": cap["boxes"], **cap["meta"],
            "model": _model_name or None}


@app.post("/api/collect/shoot")
def collect_shoot(body: dict):
    """只拍不点：立即落盘（clicks 留空），之后在补标注里点。"""
    try:
        cap = _capture(float(body.get("dmin", 0.4)), float(body.get("dmax", 1.0)))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"抓帧失败: {exc}"},
                            status_code=502)
    with _lock:
        record = _write_sample(cap["jpeg"], cap["meta"], cap["boxes"], clicks=[])
    return {"ok": True, "id": record["id"], "boxes": cap["boxes"],
            **cap["meta"], **_counts()}


@app.get("/api/collect/frame/{sid}.jpg")
def collect_frame(sid: str):
    with _lock:
        item = _pending.get(sid)
    if item is None:
        return Response("帧已过期", status_code=404, media_type="text/plain")
    return Response(item["jpeg"], media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.post("/api/collect/save")
def collect_save(body: dict):
    """拍并点模式的保存：内存帧 + 点击 → 落盘。"""
    sid = str(body.get("id") or "")
    clicks = body.get("clicks") or []
    if not clicks:
        return JSONResponse({"ok": False, "error": "至少点一个目标点再保存"},
                            status_code=400)
    with _lock:
        item = _pending.pop(sid, None)
        if item is None:
            return JSONResponse({"ok": False, "error": "帧已过期，请重新采集"},
                                status_code=404)
        record = _write_sample(item["jpeg"], item["meta"], item["boxes"],
                               clicks, int(body.get("w") or 0),
                               int(body.get("h") or 0))
    return {"ok": True, "saved": record["image"], **_counts()}


@app.post("/api/collect/discard")
def collect_discard(body: dict):
    with _lock:
        _pending.pop(str(body.get("id") or ""), None)
    return {"ok": True}


# --------------- 补标注接口 ---------------


@app.get("/api/collect/todo")
def collect_todo():
    """所有还没点过的样本（clicks 为空），按时间序。"""
    items = []
    with _lock:
        for _, records in _iter_samples():
            for r in records:
                if not r.get("clicks"):
                    items.append({k: r.get(k) for k in
                                  ("id", "image", "boxes", "distance_m",
                                   "yaw_err_deg", "pitch_err_deg", "ts")})
    return {"todo": items, **_counts()}


@app.get("/api/collect/image/{name}")
def collect_image(name: str):
    """按文件名取已落盘的样本图。"""
    if "/" in name or "\\" in name or ".." in name:
        return Response("非法文件名", status_code=400, media_type="text/plain")
    path = _samples_dir / "images" / name
    if not path.is_file():
        return Response("没有这张图", status_code=404, media_type="text/plain")
    return Response(path.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.post("/api/collect/annotate")
def collect_annotate(body: dict):
    """给已落盘的样本补点击：改写它所在 jsonl 里的那一行。"""
    sid = str(body.get("id") or "")
    clicks = body.get("clicks") or []
    if not clicks:
        return JSONResponse({"ok": False, "error": "至少点一个目标点"},
                            status_code=400)
    with _lock:
        for path, records in _iter_samples():
            hit = next((r for r in records if r.get("id") == sid), None)
            if hit is None:
                continue
            hit["clicks"] = [_attach_box({"u": int(c["u"]), "v": int(c["v"])},
                                         hit.get("boxes") or []) for c in clicks]
            if body.get("w"):
                hit["w"] = int(body["w"])
            if body.get("h"):
                hit["h"] = int(body["h"])
            tmp = path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            tmp.replace(path)
            return {"ok": True, **_counts()}
    return JSONResponse({"ok": False, "error": "样本不存在（可能文件被移动）"},
                        status_code=404)


@app.get("/api/collect/stats")
def collect_stats():
    return {**_counts(), "model": _model_name or None, "dir": str(_samples_dir)}


# --------------- 页面 ---------------

_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YOLO 取点样本采集 :7003</title>
<style>
  body { margin:0; background:#14171c; color:#dde3ea; font:14px/1.6 system-ui,"Noto Sans SC",sans-serif; }
  .wrap { max-width:1200px; margin:0 auto; padding:16px; }
  h1 { font-size:17px; margin:4px 0 12px; color:#8ecbff; }
  #imgbox { position:relative; display:inline-block; max-width:100%; }
  #view { display:block; width:100%; border:1px solid #333a44; border-radius:6px; }
  #imgbox.frozen #view { cursor:crosshair; border-color:#e0a838; }
  .box { position:absolute; border:2px solid #37d67a; pointer-events:none;
         font-size:11px; color:#37d67a; }
  .box span { background:rgba(20,23,28,.8); padding:0 4px; position:absolute;
              top:-18px; left:-2px; white-space:nowrap; }
  .dot { position:absolute; width:14px; height:14px; margin:-7px 0 0 -7px;
         border:2px solid #ff5c5c; border-radius:50%; background:rgba(255,92,92,.35);
         pointer-events:none; }
  button { background:#2a3340; color:#dde3ea; border:1px solid #3d4a5c;
           border-radius:6px; padding:8px 18px; margin:4px 8px 4px 0;
           font-size:14px; cursor:pointer; }
  button:hover { background:#354152; }
  button.primary { background:#2563eb; border-color:#2563eb; color:#fff; }
  button.green { background:#1d6b3c; border-color:#2c8a52; color:#fff; }
  button.warn { background:#5c4a2a; border-color:#7a663a; }
  input[type=number] { width:70px; background:#1c2129; color:#dde3ea;
           border:1px solid #3d4a5c; border-radius:4px; padding:4px 6px; }
  .muted { color:#8a93a0; font-size:13px; }
  #meta { color:#9fd6a0; font-size:14px; min-height:22px; }
  .bar { margin:8px 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>YOLO 取点样本采集 <span class="muted" id="stats"></span></h1>
  <div class="bar" id="liveBar">
    深度 dmin <input type="number" id="dmin" step="0.05" value="0.4">
    dmax <input type="number" id="dmax" step="0.05" value="1.0"> m
    <button class="primary" id="snapBtn" onclick="snap()">📷 拍并点</button>
    <button class="green" id="shootBtn" onclick="shoot()">📸 只拍不点（连拍）</button>
    <button id="annBtn" onclick="startAnn()">✏️ 补标注 (<span id="todoN">0</span>)</button>
  </div>
  <div class="bar" id="markBar" style="display:none">
    <button class="primary" onclick="save()">💾 保存</button>
    <button onclick="undo()">撤销上个点</button>
    <button class="warn" onclick="skip()" id="skipBtn">跳过这张</button>
    <button class="warn" onclick="quitMark()">退出</button>
    <span class="muted" id="annPos"></span>
  </div>
  <div id="meta"></div>
  <div id="imgbox">
    <img id="view" src="/cam" alt="相机画面加载中…">
  </div>
  <div class="muted" id="hint">直播中。「拍并点」冻结后点目标；「只拍不点」立即落盘，之后进「补标注」逐张点。</div>
</div>
<script>
const $ = id => document.getElementById(id);
// mode: live | frozen(拍并点) | ann(补标注)
let mode = 'live';
let cur = null;        // frozen: {id, boxes} / ann: 当前待标注记录
let clicks = [];
let todo = [], annIdx = 0;

['dmin','dmax'].forEach(k => {
  const v = localStorage.getItem('yc_'+k); if (v) $(k).value = v;
  $(k).addEventListener('change', () => localStorage.setItem('yc_'+k, $(k).value));
});

$('view').addEventListener('error', () => {
  if (mode === 'live') setTimeout(() => { $('view').src = '/cam?' + Date.now(); }, 2000);
});

async function refreshStats() {
  try {
    const s = await (await fetch('/api/collect/stats')).json();
    $('stats').textContent = `共 ${s.total} 个样本，待标注 ${s.unannotated}` +
      (s.model ? ` · 模型 ${s.model}` : ' · 未加载模型（只采图，之后补框）');
    $('todoN').textContent = s.unannotated;
  } catch (e) {}
}
refreshStats();

// ---------------- 拍并点 ----------------

async function snap() {
  $('snapBtn').disabled = true;
  try {
    const d = await postJson('/api/collect/snap',
      { dmin: +$('dmin').value, dmax: +$('dmax').value });
    if (!d.ok) { alert(d.error); return; }
    mode = 'frozen'; cur = d; clicks = [];
    showImage('/api/collect/frame/' + d.id + '.jpg', d.boxes);
    $('meta').textContent = metaText(d);
    setBars();
    $('hint').textContent = '画面已冻结：点击真实目标点（可多个），然后保存。';
  } finally { $('snapBtn').disabled = false; }
}

// ---------------- 只拍不点（连拍） ----------------

async function shoot() {
  $('shootBtn').disabled = true;
  try {
    const d = await postJson('/api/collect/shoot',
      { dmin: +$('dmin').value, dmax: +$('dmax').value });
    if (!d.ok) { alert(d.error); return; }
    $('meta').textContent = `✔ 已落盘：${metaText(d)} ｜ 共 ${d.total} 个，待标注 ${d.unannotated}`;
    refreshStats();
  } finally { $('shootBtn').disabled = false; }
}

// ---------------- 补标注 ----------------

async function startAnn() {
  const d = await (await fetch('/api/collect/todo')).json();
  todo = d.todo || [];
  if (!todo.length) { alert('没有待标注的样本'); return; }
  annIdx = 0;
  mode = 'ann';
  showAnn();
}

function showAnn() {
  if (annIdx >= todo.length) { quitMark('全部标注完成'); refreshStats(); return; }
  cur = todo[annIdx]; clicks = [];
  showImage('/api/collect/image/' + cur.image.replace('images/', ''), cur.boxes);
  $('meta').textContent = metaText(cur);
  $('annPos').textContent = `第 ${annIdx + 1}/${todo.length} 张`;
  setBars();
  $('hint').textContent = '补标注：点击真实目标点，保存后自动下一张。';
}

function skip() { annIdx += 1; showAnn(); }

// ---------------- 共用 ----------------

async function save() {
  if (!cur) return;
  if (!clicks.length) { alert('先在图上点目标点'); return; }
  const img = $('view');
  const body = { id: cur.id, clicks, w: img.naturalWidth, h: img.naturalHeight };
  const d = await postJson(mode === 'ann' ? '/api/collect/annotate'
                                          : '/api/collect/save', body);
  if (!d.ok) { alert(d.error); return; }
  refreshStats();
  if (mode === 'ann') { annIdx += 1; showAnn(); }
  else backLive(`已保存（共 ${d.total} 个，待标注 ${d.unannotated}）`);
}

async function discardFrozen() {
  if (cur) postJson('/api/collect/discard', { id: cur.id });
  backLive('已放弃');
}

function quitMark(msg) {
  if (mode === 'frozen') { discardFrozen(); return; }
  backLive(msg || '已退出补标注');
}

function backLive(msg) {
  mode = 'live'; cur = null; clicks = []; todo = [];
  clearOverlay('.box'); clearOverlay('.dot');
  $('imgbox').classList.remove('frozen');
  $('view').onload = null;
  $('view').src = '/cam?' + Date.now();
  $('meta').textContent = '';
  setBars();
  $('hint').textContent = (msg || '') + '。直播中，可继续采集。';
}

function setBars() {
  $('liveBar').style.display = mode === 'live' ? '' : 'none';
  $('markBar').style.display = mode === 'live' ? 'none' : '';
  $('skipBtn').style.display = mode === 'ann' ? '' : 'none';
  $('annPos').textContent = mode === 'ann' ? $('annPos').textContent : '';
}

function showImage(src, boxes) {
  clearOverlay('.box'); clearOverlay('.dot');
  $('imgbox').classList.add('frozen');
  const img = $('view');
  img.onload = () => drawBoxes(boxes || []);
  img.src = src;
}

function drawBoxes(boxes) {
  clearOverlay('.box');
  const img = $('view');
  if (!img.naturalWidth) return;
  for (const b of boxes) {
    const [x1, y1, x2, y2] = b.xyxy;
    const el = document.createElement('div');
    el.className = 'box';
    el.style.left = (x1 / img.naturalWidth * 100) + '%';
    el.style.top = (y1 / img.naturalHeight * 100) + '%';
    el.style.width = ((x2 - x1) / img.naturalWidth * 100) + '%';
    el.style.height = ((y2 - y1) / img.naturalHeight * 100) + '%';
    el.innerHTML = `<span>${b.name} ${(b.conf * 100).toFixed(0)}%</span>`;
    $('imgbox').appendChild(el);
  }
}

$('view').addEventListener('click', ev => {
  if (mode === 'live' || !cur) return;
  const img = $('view');
  const rect = img.getBoundingClientRect();
  const fx = (ev.clientX - rect.left) / rect.width;
  const fy = (ev.clientY - rect.top) / rect.height;
  clicks.push({ u: Math.round(fx * img.naturalWidth),
                v: Math.round(fy * img.naturalHeight) });
  const dot = document.createElement('div');
  dot.className = 'dot';
  dot.style.left = (fx * 100) + '%'; dot.style.top = (fy * 100) + '%';
  $('imgbox').appendChild(dot);
});

function undo() {
  clicks.pop();
  const dots = document.querySelectorAll('.dot');
  if (dots.length) dots[dots.length - 1].remove();
}

function metaText(d) {
  return `距离 ${d.distance_m ?? '?'} m · yaw ${d.yaw_err_deg ?? '?'}° · ` +
    `pitch ${d.pitch_err_deg ?? '?'}° · 框 ${(d.boxes || []).length} 个` +
    (d.infer_error ? `（推理失败: ${d.infer_error}）` : '');
}

async function postJson(url, body) {
  const r = await fetch(url, { method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body) });
  return r.json();
}

function clearOverlay(sel) {
  document.querySelectorAll(sel).forEach(e => e.remove());
}
</script>
</body>
</html>
"""


@app.get("/")
def page():
    return HTMLResponse(_PAGE)


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> None:
    global _reach_base, _model, _model_name, _model_conf, _samples_dir
    import uvicorn

    parser = argparse.ArgumentParser(description="YOLO 取点样本采集台（7003）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7003)
    parser.add_argument("--reach-base", default="http://127.0.0.1:8001")
    parser.add_argument("--model", default=None,
                        help="YOLO .pt 模型路径（可选；不给则只采图不推理）")
    parser.add_argument("--conf", type=float, default=0.25, help="置信度阈值")
    parser.add_argument("--out", default=None,
                        help=f"样本目录（默认 {_samples_dir}）")
    args = parser.parse_args()
    _reach_base = args.reach_base.rstrip("/")
    _model_conf = args.conf
    if args.out:
        _samples_dir = Path(args.out)

    if args.model:
        try:
            from ultralytics import YOLO
            _model = YOLO(args.model)
            _model_name = Path(args.model).name
            print(f"[collect] 模型已加载: {args.model}")
        except Exception as exc:
            print(f"[collect] ⚠ 模型加载失败（继续以无模型模式采集）: {exc}")
    else:
        print("[collect] 未指定 --model：只采图+测量+点击，之后可离线补跑推理")

    print(f"[collect] 采集台已启动（进程常驻属正常）")
    print(f"[collect] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    print(f"[collect] 样本目录: {_samples_dir}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
