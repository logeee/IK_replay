#!/usr/bin/env python3
"""18000 能力配置中心：四级能力注册表 + 手眼标定归档的管理服务。

四级：臂侧 → 手型号 → 任务配置 → 实现方式。
数据落在 config/capability_registry.json；标定归档在
config/hand_eye/{arm}__{hand_id}/handeye3d_result.json。

    python tools/capability_server.py --port 18000

- GET  /api/capability/registry            注册表全量 + 各组合标定状态 + 枚举元数据
- POST /api/capability/hands               新增/编辑手型号（body 带 id 即编辑）
- POST /api/capability/hands/delete        删除手型号（被引用时拒绝）
- POST /api/capability/capabilities        新增/编辑能力条目
- POST /api/capability/capabilities/delete 删除能力条目
- POST /api/capability/active              切换激活组合（17001/18001 重启后生效）
- POST /api/capability/calibrations        登记标定（source_path 复制入库 或
                                           content 直接上传 JSON 内容）
- /                                        托管 web-capability/dist 构建产物（若已构建）
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import capability_registry as reg

DIST_DIR = ROOT / "web-capability" / "dist"
REGISTRY_PATH = reg.DEFAULT_REGISTRY_PATH

app = FastAPI(title="capability-config")
# 开发时 Vite (5173) 直接跨域访问本服务，省掉代理配置的坑
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
_lock = threading.Lock()


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _registry_payload(registry: dict[str, Any]) -> dict[str, Any]:
    calibrations = [
        reg.calibration_info(registry, c["arm"], c["hand_id"], ROOT)
        for c in registry["calibrations"]
    ]
    # 激活组合即使没登记标定也给出状态（前端徽标要显示"未登记"）
    active = registry.get("active")
    if active and not any(c["arm"] == active["arm"]
                          and c["hand_id"] == active["hand_id"]
                          for c in registry["calibrations"]):
        calibrations.append(reg.calibration_info(
            registry, active["arm"], active["hand_id"], ROOT))
    return {
        "ok": True,
        "registry": registry,
        "calibrations": calibrations,
        "meta": {
            "arms": list(reg.ARMS),
            "arm_labels": reg.ARM_LABELS,
            "design_sides": list(reg.DESIGN_SIDES),
            "sites": list(reg.SITES),
            "directions": list(reg.DIRECTIONS),
            "methods": list(reg.METHODS),
            "method_labels": reg.METHOD_LABELS,
            "implemented_methods": sorted(reg.IMPLEMENTED_METHODS),
            "method_param_specs": reg.METHOD_PARAM_SPECS,
        },
    }


@app.get("/api/capability/registry")
def registry_get():
    with _lock:
        registry = reg.load_registry(REGISTRY_PATH)
    return _registry_payload(registry)


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


@app.post("/api/capability/hands")
async def hands_upsert(request: Request):
    body = await _json_body(request)
    with _lock:
        registry = reg.load_registry(REGISTRY_PATH)
        hand_id = str(body.get("id") or "").strip()
        hands = registry["hands"]
        if hand_id and any(h["id"] == hand_id for h in hands):
            hands = [dict(body, id=hand_id) if h["id"] == hand_id else h
                     for h in hands]
        else:
            hands = hands + [body]
        registry["hands"] = hands
        try:
            registry = reg.save_registry(registry, REGISTRY_PATH)
        except ValueError as exc:
            return _error(str(exc))
    return _registry_payload(registry)


@app.post("/api/capability/hands/delete")
async def hands_delete(request: Request):
    body = await _json_body(request)
    hand_id = str(body.get("id") or "").strip()
    if not hand_id:
        return _error("缺少手型号 id")
    with _lock:
        registry = reg.load_registry(REGISTRY_PATH)
        if not any(h["id"] == hand_id for h in registry["hands"]):
            return _error(f"手型号「{hand_id}」不存在", 404)
        used_by = [c["id"] for c in registry["capabilities"]
                   if c["hand_id"] == hand_id]
        if used_by:
            return _error(f"手型号「{hand_id}」被能力 {used_by} 引用，先删除或改指向")
        active = registry.get("active")
        if active and active.get("hand_id") == hand_id:
            return _error(f"手型号「{hand_id}」是当前激活组合，先切换激活组合")
        registry["hands"] = [h for h in registry["hands"]
                             if h["id"] != hand_id]
        registry["calibrations"] = [c for c in registry["calibrations"]
                                    if c["hand_id"] != hand_id]
        try:
            registry = reg.save_registry(registry, REGISTRY_PATH)
        except ValueError as exc:
            return _error(str(exc))
    return _registry_payload(registry)


@app.post("/api/capability/capabilities")
async def capabilities_upsert(request: Request):
    body = await _json_body(request)
    with _lock:
        registry = reg.load_registry(REGISTRY_PATH)
        cap_id = str(body.get("id") or "").strip()
        caps = registry["capabilities"]
        if cap_id and any(c["id"] == cap_id for c in caps):
            caps = [dict(body, id=cap_id) if c["id"] == cap_id else c
                    for c in caps]
        else:
            caps = caps + [body]
        registry["capabilities"] = caps
        try:
            registry = reg.save_registry(registry, REGISTRY_PATH)
        except ValueError as exc:
            return _error(str(exc))
    return _registry_payload(registry)


@app.post("/api/capability/capabilities/delete")
async def capabilities_delete(request: Request):
    body = await _json_body(request)
    cap_id = str(body.get("id") or "").strip()
    if not cap_id:
        return _error("缺少能力 id")
    with _lock:
        registry = reg.load_registry(REGISTRY_PATH)
        if not any(c["id"] == cap_id for c in registry["capabilities"]):
            return _error(f"能力「{cap_id}」不存在", 404)
        registry["capabilities"] = [c for c in registry["capabilities"]
                                    if c["id"] != cap_id]
        try:
            registry = reg.save_registry(registry, REGISTRY_PATH)
        except ValueError as exc:
            return _error(str(exc))
    return _registry_payload(registry)


@app.post("/api/capability/active")
async def active_set(request: Request):
    body = await _json_body(request)
    with _lock:
        registry = reg.load_registry(REGISTRY_PATH)
        registry["active"] = {"arm": body.get("arm"),
                              "hand_id": body.get("hand_id")}
        try:
            registry = reg.save_registry(registry, REGISTRY_PATH)
        except ValueError as exc:
            return _error(str(exc))
    return _registry_payload(registry)


@app.post("/api/capability/calibrations")
async def calibrations_register(request: Request):
    """登记标定：content=前端上传的标定 JSON 内容；source_path=服务器
    本机路径（复制入库）。两者给其一即可；都不给则只登记占位（待补）。"""
    body = await _json_body(request)
    content = body.get("content")
    if content is not None and (not isinstance(content, dict)
                                or "T_cam2base" not in content):
        return _error("标定内容不合法：缺少 T_cam2base（请上传 "
                      "handeye3d_result.json）")
    with _lock:
        registry = reg.load_registry(REGISTRY_PATH)
        try:
            arm = reg._clean_arm(body.get("arm"), "arm")
        except ValueError as exc:
            return _error(str(exc))
        hand_id = str(body.get("hand_id") or "").strip()
        if not reg.find_hand(registry, hand_id):
            return _error(f"手型号「{hand_id}」不存在", 404)
        entry = {
            "arm": arm,
            "hand_id": hand_id,
            "source_path": str(body.get("source_path") or "").strip(),
            "registered_at": datetime.now().isoformat(timespec="seconds"),
        }
        others = [c for c in registry["calibrations"]
                  if not (c["arm"] == arm and c["hand_id"] == hand_id)]
        registry["calibrations"] = others + [entry]
        try:
            registry = reg.save_registry(registry, REGISTRY_PATH)
        except ValueError as exc:
            return _error(str(exc))
        target = reg.calib_abs_path(arm, hand_id, ROOT)
        if content is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(content, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        else:
            calib = next(c for c in registry["calibrations"]
                         if c["arm"] == arm and c["hand_id"] == hand_id)
            reg.try_import_calibration(calib, ROOT)
    return _registry_payload(registry)


if DIST_DIR.is_dir():
    app.mount("/", StaticFiles(directory=DIST_DIR, html=True), name="dist")
else:
    @app.get("/")
    def index_placeholder():
        return JSONResponse({
            "ok": True,
            "hint": "前端还没构建：cd web-capability && npm install && "
                    "npm run build；开发模式则 npm run dev 后访问 Vite 地址",
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

    parser = argparse.ArgumentParser(description="四级能力配置中心（18000）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18000)
    args = parser.parse_args()
    with _lock:
        registry = reg.ensure_registry(REGISTRY_PATH, ROOT)
    active = registry.get("active") or {}
    print(f"[capability] 注册表: {REGISTRY_PATH}")
    print(f"[capability] 激活组合: "
          f"{reg.ARM_LABELS.get(active.get('arm'), '未设置')}"
          f" + {active.get('hand_id', '-')}")
    print(f"[capability] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
