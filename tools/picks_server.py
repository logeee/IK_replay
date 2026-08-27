#!/usr/bin/env python3
"""选点记录（data/pick_history）独立可视化服务。

只读、零依赖于相机/YOLO/18001，机器人离线也能看历史记录：

    python tools/picks_server.py --port 7010

- GET /api/picks?limit=N          记录列表（含 meta.json 全量数值）
- GET /api/picks/{name}/{file}    单条记录的 snapshot.jpg / cloud.ply / meta.json
- /                                托管 web-picks/dist 构建产物（若已构建）
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
PICK_HISTORY_DIR = ROOT / "data" / "pick_history"
DIST_DIR = ROOT / "web-picks" / "dist"
RECORD_FILES = ("snapshot.jpg", "cloud.ply", "meta.json")
_RECORD_NAME_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")
_MEDIA_TYPES = {
    "snapshot.jpg": "image/jpeg",
    "cloud.ply": "application/octet-stream",
    "meta.json": "application/json",
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
        records.append({"name": name, "cloud_bytes": cloud_bytes, "meta": meta})
    return records


@app.get("/api/picks")
def picks_list(limit: int = 500):
    return {"ok": True, "records": _list_records(max(1, min(500, limit)))}


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
    args = parser.parse_args()
    print(f"[picks] 记录目录: {PICK_HISTORY_DIR}")
    print(f"[picks] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
