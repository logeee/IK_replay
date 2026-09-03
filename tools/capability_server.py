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
- POST /api/capability/sequence-claims     整组保存某能力条目的认领
                                           （起手式动作名 + 手选位点名）
- POST /api/capability/sequence-claims/add 录制上报（臂+手+动作名）：按各
                                           条目起手式正则自动路由认领，幂等
- /                                        托管 web-capability/dist 构建产物（若已构建）

公共动作池 = data/sequences，位点池 = data/waypoints（都由 18001 录制落
盘）；认领挂在能力条目上——拨和扭是不同条目，各认各的互不影响。终点位
点不落库：认领了起手式即自动推导其配套终点；其余位点手选。GET registry
的 payload 附 sequence_pool / waypoint_pool（按名聚合，含最近录制时间）。
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
        "sequence_pool": reg.sequence_pool(ROOT),
        "waypoint_pool": reg.waypoint_pool(ROOT),
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
        # 认领挂在能力条目上；手被条目引用时上面已拒绝，无需清理认领
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
        registry["sequence_claims"] = [c for c in registry["sequence_claims"]
                                       if c["capability_id"] != cap_id]
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


@app.post("/api/capability/sequence-claims")
async def sequence_claims_set(request: Request):
    """整组保存某能力条目的认领。

    body: {capability_id, names: [...], waypoint_names: [...]}。
    names=起手式动作名；waypoint_names=手选的非终点位点（终点位点不落库，
    由已认领起手式自动推导）。缺省字段保留原值。
    """
    body = await _json_body(request)
    capability_id = str(body.get("capability_id") or "").strip()
    with _lock:
        registry = reg.load_registry(REGISTRY_PATH)
        if not any(c["id"] == capability_id
                   for c in registry["capabilities"]):
            return _error(f"能力条目「{capability_id}」不存在", 404)
        previous = next((c for c in registry["sequence_claims"]
                         if c["capability_id"] == capability_id), None)
        entry = {
            "capability_id": capability_id,
            "names": (body["names"] if "names" in body
                      else (previous or {}).get("names") or []),
            "waypoint_names": (
                body["waypoint_names"] if "waypoint_names" in body
                else (previous or {}).get("waypoint_names") or []),
        }
        others = [c for c in registry["sequence_claims"]
                  if c["capability_id"] != capability_id]
        registry["sequence_claims"] = others + [entry]
        try:
            registry = reg.save_registry(registry, REGISTRY_PATH)
        except ValueError as exc:
            return _error(str(exc))
    return _registry_payload(registry)


@app.post("/api/capability/sequence-claims/add")
async def sequence_claims_add(request: Request):
    """录制上报（18001 保存新序列后自动调用，幂等）。

    body: {arm, hand_id, name}。动作名按该组合各条目的起手式正则路由：
    命中谁认领给谁（拨/扭的命名不同，天然互不污染）；谁都不命中就留池
    待手动认领。返回精简结果（18001 只关心路由到了哪些条目）。
    """
    body = await _json_body(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return _error("缺少动作名 name")
    with _lock:
        registry = reg.load_registry(REGISTRY_PATH)
        try:
            arm = reg._clean_arm(body.get("arm"), "arm")
        except ValueError as exc:
            return _error(str(exc))
        hand_id = str(body.get("hand_id") or "").strip()
        if not reg.find_hand(registry, hand_id):
            return _error(f"手型号「{hand_id}」不存在", 404)
        matched = reg.route_sequence_claim(registry, arm, hand_id, name)
        claimed_to: list[dict] = []
        changed = False
        for cap_id in matched:
            entry = next((c for c in registry["sequence_claims"]
                          if c["capability_id"] == cap_id), None)
            already = entry is not None and name in entry["names"]
            if entry is None:
                registry["sequence_claims"] = registry["sequence_claims"] + [
                    {"capability_id": cap_id, "names": [name]}]
            elif not already:
                entry["names"] = entry["names"] + [name]
            changed = changed or not already
            cap = next(c for c in registry["capabilities"]
                       if c["id"] == cap_id)
            claimed_to.append({
                "capability_id": cap_id,
                "label": f"{cap['task']['name']}·{cap['method']}",
                "already_claimed": already,
            })
        if changed:
            try:
                registry = reg.save_registry(registry, REGISTRY_PATH)
            except ValueError as exc:
                return _error(str(exc))
    return {"ok": True, "arm": arm, "hand_id": hand_id, "name": name,
            "claimed_to": claimed_to}


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
        # 一次性迁移到「能力条目级」认领：无键/组合级旧格式 → 按各条目
        # 起手式正则拆分（存量默认归 右臂+因时-右-1 的条目）
        if reg.migrate_sequence_claims(REGISTRY_PATH, ROOT):
            migrated = reg.load_registry(REGISTRY_PATH)
            parts = [f"{c['capability_id']}={len(c['names'])}个"
                     for c in migrated["sequence_claims"]]
            print(f"[capability] 迁移：起手式认领已拆到能力条目 → "
                  f"{'、'.join(parts) or '（无匹配，全部留池）'}")
        registry = reg.ensure_registry(REGISTRY_PATH, ROOT)
    active = registry.get("active") or {}
    print(f"[capability] 注册表: {REGISTRY_PATH}")
    print(f"[capability] 激活组合: "
          f"{reg.ARM_LABELS.get(active.get('arm'), '未设置')}"
          f" + {active.get('hand_id', '-')}")
    print(f"[capability] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
