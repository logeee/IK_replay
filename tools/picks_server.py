#!/usr/bin/env python3
"""选点记录（data/pick_history）独立可视化服务。

只读、零依赖于相机/YOLO/18001，机器人离线也能看历史记录：

    python tools/picks_server.py --port 7010

- GET /api/picks?limit=N          记录列表（含 meta.json 全量数值）
- GET /api/picks/{name}/{file}    单条记录的 snapshot.jpg / cloud.ply / meta.json
- GET /api/executions             执行记录摘要（18001 落的 JSONL，按 capture_id 关联选点）
- GET /api/executions/{exec_id}   单条执行完整记录（含 5Hz 躯干漂移时间线）
- /                                托管 web-picks/dist 构建产物（若已构建）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pick_execution_archive import (
    EXECUTIONS_FILENAME,
    load_embedded_executions,
)

PICK_HISTORY_DIR = ROOT / "data" / "pick_history"
REACH_LOG_DIR = ROOT / "logs" / "reach"
DIST_DIR = ROOT / "web-picks" / "dist"
RECORD_FILES = ("snapshot.jpg", "cloud.ply", "meta.json",
                "flip_before.jpg", "flip_after.jpg",
                "flip_before_wrist.jpg", "flip_result.json",
                EXECUTIONS_FILENAME)
_RECORD_NAME_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")
_MEDIA_TYPES = {
    "snapshot.jpg": "image/jpeg",
    "cloud.ply": "application/octet-stream",
    "meta.json": "application/json",
    "flip_before.jpg": "image/jpeg",
    "flip_after.jpg": "image/jpeg",
    "flip_before_wrist.jpg": "image/jpeg",
    "flip_result.json": "application/json",
    EXECUTIONS_FILENAME: "application/x-ndjson",
}

app = FastAPI(title="pick-history-viewer")
# 开发时 Vite (5173) 直接跨域访问本服务，省掉代理配置的坑
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _list_records(limit: int) -> list[dict[str, Any]]:
    if not PICK_HISTORY_DIR.is_dir():
        return []
    names = sorted(
        (p.name for p in PICK_HISTORY_DIR.iterdir()
         if p.is_dir() and _RECORD_NAME_RE.match(p.name)),
        reverse=True,
    )[:limit]
    records = []
    for name in names:
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(
                (PICK_HISTORY_DIR / name / "meta.json")
                .read_text(encoding="utf-8")
            )
        except Exception:
            pass
        try:
            cloud_bytes = (PICK_HISTORY_DIR / name / "cloud.ply").stat().st_size
        except OSError:
            cloud_bytes = 0
        # 拨动前后证据（流程写入，手动选点的记录没有）
        flip: dict[str, Any] | None = None
        flip_path = PICK_HISTORY_DIR / name / "flip_result.json"
        if flip_path.is_file():
            try:
                flip = json.loads(flip_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        records.append({"name": name, "cloud_bytes": cloud_bytes,
                        "meta": meta, "flip": flip})
    return records


@app.get("/api/picks")
def picks_list(limit: int = 500):
    return {"ok": True, "records": _list_records(max(1, min(500, limit)))}


# --------- 执行记录：18001 每段真机动作落的 JSONL（reach_YYYYMMDD.jsonl）---------
# 摘要给列表用；完整记录（含 5Hz torso_trace）按 id 单独取。
# 文件按 (mtime, size) 缓存，机器人写新日志后下次请求自动重读。
_exec_cache: dict[str, tuple[tuple[float, int], list[dict[str, Any]]]] = {}


def _exec_summary(rec: dict[str, Any]) -> dict[str, Any]:
    pick = rec.get("pick_context") or {}
    tcp = rec.get("tcp") or {}
    drift = rec.get("torso_drift") or {}
    return {
        "id": rec.get("id"),
        "ts": rec.get("ts"),
        "segment": rec.get("segment"),
        "result": rec.get("result"),
        "capture_id": pick.get("capture_id"),
        "selection_source": pick.get("selection_source"),
        "target_point_slot": pick.get("target_point_slot"),
        "matched_detection_name": pick.get("matched_detection_name"),
        "duration_s": (rec.get("params") or {}).get("duration_s"),
        "tcp_mm": {k: tcp.get(k) for k in
                   ("ik_mm", "track_mm", "total_mm", "total_vs_drifted_mm")},
        "torso_rotation_deg": drift.get("torso_rotation_deg"),
        "target_shift_mm": drift.get("target_shift_mm"),
        "waist_delta_deg": drift.get("waist_delta_deg"),
        "imu_rpy_delta_deg": drift.get("imu_rpy_delta_deg"),
        "trace_len": len(rec.get("torso_trace") or []),
    }


def _load_exec_records() -> list[dict[str, Any]]:
    # New records carry their own diagnostics, so copying one pick-history
    # directory is sufficient. The central reach logs remain a fallback for
    # records created before the self-contained format was introduced.
    records = load_embedded_executions(PICK_HISTORY_DIR)
    if REACH_LOG_DIR.is_dir():
        for path in sorted(REACH_LOG_DIR.glob("reach_*.jsonl")):
            try:
                stat = path.stat()
                key = (stat.st_mtime, stat.st_size)
            except OSError:
                continue
            cached = _exec_cache.get(path.name)
            if cached and cached[0] == key:
                records.extend(cached[1])
                continue
            file_records: list[dict[str, Any]] = []
            try:
                with path.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            file_records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
            _exec_cache[path.name] = (key, file_records)
            records.extend(file_records)

    # The same execution is normally present in both locations on the robot.
    # Keep only one copy while preserving records without an id.
    deduplicated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for record in records:
        execution_id = str(record.get("id") or "")
        if execution_id:
            if execution_id in seen_ids:
                continue
            seen_ids.add(execution_id)
        deduplicated.append(record)
    return deduplicated


@app.get("/api/executions")
def executions_list(capture_id: str | None = None):
    records = _load_exec_records()
    if capture_id:
        records = [r for r in records
                   if (r.get("pick_context") or {}).get("capture_id")
                   == capture_id]
    summaries = [_exec_summary(r) for r in records]
    summaries.sort(key=lambda s: s.get("ts") or "", reverse=True)
    return {"ok": True, "records": summaries}


@app.get("/api/executions/{exec_id}")
def execution_detail(exec_id: str):
    for rec in _load_exec_records():
        if rec.get("id") == exec_id:
            return {"ok": True, "record": rec}
    return JSONResponse({"ok": False, "error": "执行记录不存在"},
                        status_code=404)


@app.get("/api/picks/{name}/{filename}")
def picks_file(name: str, filename: str):
    if not _RECORD_NAME_RE.match(name) or filename not in RECORD_FILES:
        return JSONResponse({"ok": False, "error": "非法记录名或文件名"},
                            status_code=400)
    path = PICK_HISTORY_DIR / name / filename
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "记录不存在"},
                            status_code=404)
    return FileResponse(path, media_type=_MEDIA_TYPES[filename],
                        headers={"Cache-Control": "public, max-age=86400"})


if DIST_DIR.is_dir():
    app.mount("/", StaticFiles(directory=DIST_DIR, html=True), name="dist")
else:
    @app.get("/")
    def index_placeholder():
        return JSONResponse({
            "ok": True,
            "hint": "前端还没构建：cd web-picks && npm install && npm run "
                    "build；开发模式则 npm run dev 后访问 Vite 地址",
        })


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="选点记录可视化服务（只读）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7010)
    parser.add_argument("--reach-log-dir", default=None,
                        help=f"18001 执行日志目录（默认 {REACH_LOG_DIR}）")
    args = parser.parse_args()
    if args.reach_log_dir:
        REACH_LOG_DIR = Path(args.reach_log_dir).expanduser().resolve()
    print(f"[picks] 记录目录: {PICK_HISTORY_DIR}")
    print(f"[picks] 执行日志: {REACH_LOG_DIR}"
          + ("" if REACH_LOG_DIR.is_dir() else "（目录不存在，执行记录为空）"))
    print(f"[picks] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
