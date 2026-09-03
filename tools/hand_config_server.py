"""18003 灵巧手配置页：连接 / 调姿 / 命名保存姿态到 IK 项目。

- 启动拜访 18000：按激活组合（臂 + 手型号）取 18089 设备绑定
  （hand_web_device_id）与 design_side；没配好拒绝启动（与 17001/18001
  同一策略，改了 18000 配置后重启本服务生效）。
- 基本操作对齐 18089 hand_web：连接 / 断开 / 下发归一化 positions
  （6 个 0~1，0=张开）。本页的增量是「姿态库」——命名保存到
  data/hand_poses/，18001 的起手点测试可以选择这些手位执行。
- 手臂运动期间也可在本页调手：手走 18089 HTTP，与手臂 DDS 通道互不
  阻塞；被视觉控制等占用时 18089 返回 409，本服务只透传、绝不抢占。

API：
- GET  /                      配置页面
- GET  /api/hand/info         激活组合 + 设备绑定 + 18089 地址
- GET  /api/hand/state        18089 实时状态（透传 /api/status）
- POST /api/hand/connect      让 18089 连接激活组合的设备
- POST /api/hand/disconnect   断开 18089 当前设备
- POST /api/hand/command      {positions[6], duration_ms}
- GET  /api/hand/poses        姿态库列表
- POST /api/hand/poses        {name, positions[6]} 保存
- POST /api/hand/poses/delete {file} 删除
"""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from core import hand_poses  # noqa: E402
from core.hand_runtime import (  # noqa: E402
    _DEFAULT_PREVIEWS,
    _default_fetch_json,
    _default_post_json,
)

PAGE_PATH = ROOT / "web" / "hand-config.html"
# 18089 hand_web 同款 URDF/STL（brainco_hand / inspire_hand 子目录）
DEFAULT_HAND_ASSETS_ROOT = Path("/home/robot/eai-teleop-studio/assets")

app = FastAPI(title="灵巧手配置（18003）")
# 三维视图要用 three.js（/web/vendor）；/assets 在 main() 里按参数挂载
app.mount("/web", StaticFiles(directory=ROOT / "web"), name="web")

# main() 启动时按 18000 激活组合填好
CONTEXT: dict[str, Any] = {}


def _service_url(path: str) -> str:
    return urljoin(str(CONTEXT["service_url"]).rstrip("/") + "/",
                   path.lstrip("/"))


def _proxy_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, HTTPError):
        try:
            import json as _json
            detail = _json.loads(exc.read().decode("utf-8", errors="replace"))
            return {"ok": False,
                    "error": f"18089: {detail.get('error') or exc.code}"}
        except (ValueError, OSError):
            return {"ok": False, "error": f"18089: HTTP {exc.code}"}
    return {"ok": False, "error": f"18089 不可达: {exc}"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(PAGE_PATH, headers={"Cache-Control": "no-cache"})


def _model_info() -> dict[str, Any] | None:
    """三维视图的模型描述：URDF 地址 + 归一化关节映射（同 18001）。"""
    preview = _DEFAULT_PREVIEWS.get(str(CONTEXT.get("device_id") or ""))
    if not preview:
        return None
    preview = deepcopy(preview)
    model_root = str(preview.get("model_root") or "").rstrip("/")
    urdf = str(preview.get("urdf") or "").format(side=CONTEXT["side"])
    if not model_root or not urdf:
        return None
    return {
        "urdf_url": f"{model_root}/{urdf}",
        "mesh_base_url": f"{model_root}/",
        "preview": preview,
    }


@app.get("/api/hand/info")
def hand_info() -> dict:
    return {
        "ok": True,
        "combo": CONTEXT["combo"],
        "hand_name": CONTEXT["hand_name"],
        "device_id": CONTEXT["device_id"],
        "side": CONTEXT["side"],
        "service_url": CONTEXT["service_url"],
        "model": _model_info(),
    }


@app.get("/api/hand/state")
def hand_state() -> dict:
    try:
        body = _default_fetch_json(_service_url("api/status"), 2.0, False)
    except Exception as exc:  # noqa: BLE001 —— 状态轮询,一律转文字
        return _proxy_error(exc)
    if not isinstance(body, dict):
        return {"ok": False, "error": "18089 返回格式异常"}
    return {"ok": True, **body}


@app.post("/api/hand/connect")
def hand_connect_route() -> dict:
    try:
        body = _default_post_json(
            _service_url("api/connect"),
            {"device_id": CONTEXT["device_id"]}, 8.0, False)
    except Exception as exc:  # noqa: BLE001
        return _proxy_error(exc)
    if not isinstance(body, dict) or body.get("ok") is False:
        return {"ok": False,
                "error": str((body or {}).get("error") or "18089 拒绝连接")}
    return {"ok": True, **body}


@app.post("/api/hand/disconnect")
def hand_disconnect_route() -> dict:
    try:
        body = _default_post_json(
            _service_url("api/disconnect"), {}, 5.0, False)
    except Exception as exc:  # noqa: BLE001
        return _proxy_error(exc)
    if not isinstance(body, dict) or body.get("ok") is False:
        return {"ok": False,
                "error": str((body or {}).get("error") or "18089 拒绝断开")}
    return {"ok": True, **body}


@app.post("/api/hand/command")
def hand_command_route(body: dict) -> Any:
    try:
        positions = hand_poses.validate_positions(body.get("positions"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    duration_ms = int(body.get("duration_ms") or 500)
    payload = {
        "side": CONTEXT["side"],
        "positions": positions,
        "duration_ms": max(50, min(duration_ms, 5000)),
    }
    # 拖动实时发送时前端会带 continuous，透传给 18089 降低日志噪声
    if body.get("continuous") is True:
        payload["continuous"] = True
    try:
        result = _default_post_json(
            _service_url("api/command"), payload, 5.0, False)
    except Exception as exc:  # noqa: BLE001
        return _proxy_error(exc)
    if not isinstance(result, dict) or result.get("ok") is False:
        return {"ok": False,
                "error": str((result or {}).get("error") or "18089 拒绝指令")}
    return {"ok": True}


@app.get("/api/hand/poses")
def poses_list() -> dict:
    return {"ok": True, "poses": hand_poses.list_poses(),
            "device_id": CONTEXT["device_id"], "side": CONTEXT["side"]}


@app.post("/api/hand/poses")
def poses_save(body: dict) -> Any:
    try:
        item = hand_poses.save_pose(
            body.get("name"),
            body.get("positions"),
            device_id=CONTEXT["device_id"],
            side=CONTEXT["side"],
            combo=CONTEXT["combo"],
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    return {"ok": True, "pose": item}


@app.post("/api/hand/poses/delete")
def poses_delete(body: dict) -> Any:
    filename = str(body.get("file") or "")
    if not hand_poses.delete_pose(filename):
        return JSONResponse(
            {"ok": False, "error": f"姿态文件不存在: {filename}"},
            status_code=404)
    return {"ok": True}


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


def main() -> int:
    import uvicorn

    from core.capability_client import (
        DEFAULT_CAPABILITY_URL,
        CapabilityUnavailable,
        describe_active,
        fetch_snapshot,
    )

    parser = argparse.ArgumentParser(description="灵巧手配置页（18003）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18003)
    parser.add_argument("--capability-url", default=None,
                        help=f"18000 地址（默认 {DEFAULT_CAPABILITY_URL}）")
    parser.add_argument("--hand-service-url",
                        default="https://127.0.0.1:18089",
                        help="18089 hand_web 地址")
    parser.add_argument("--hand-assets-root", type=Path,
                        default=DEFAULT_HAND_ASSETS_ROOT,
                        help="URDF/STL 资产目录（三维视图用）")
    args = parser.parse_args()

    if args.hand_assets_root.is_dir():
        app.mount("/assets", StaticFiles(directory=args.hand_assets_root),
                  name="assets")
    else:
        print(f"[手配置] 资产目录不存在，三维视图不可用: {args.hand_assets_root}")

    # 启动拜访 18000：激活组合决定设备与侧，改配置后重启本服务生效
    try:
        snapshot = fetch_snapshot(args.capability_url)
    except CapabilityUnavailable as exc:
        print(f"[手配置] 启动拜访 18000 失败：{exc}")
        return 1
    registry = snapshot["registry"]
    print(f"[手配置] 18000 {describe_active(snapshot)}")
    active = registry.get("active") or {}
    hand = next(
        (item for item in registry.get("hands", [])
         if item.get("id") == active.get("hand_id")),
        None,
    )
    if not active or hand is None:
        print("[手配置] 18000 尚未配置有效的 active.arm + active.hand_id")
        return 1
    device_id = str(hand.get("hand_web_device_id") or "").strip()
    if not device_id:
        print(f"[手配置] 手型号 {hand.get('name') or hand.get('id')} 未绑定 "
              "18089 设备（在 18000 配置 hand_web_device_id 后重启）")
        return 1

    CONTEXT.update(
        combo={"arm": active.get("arm"), "hand_id": active.get("hand_id")},
        hand_name=str(hand.get("name") or hand.get("id")),
        device_id=device_id,
        side=str(hand.get("design_side") or "right"),
        service_url=args.hand_service_url,
    )
    print(f"[手配置] 激活组合: {CONTEXT['hand_name']}"
          f"（{device_id} · {CONTEXT['side']}）")
    print(f"[手配置] 姿态库: {hand_poses.POSES_DIR}")
    print(f"[手配置] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
