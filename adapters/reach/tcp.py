"""TCP 工作点选择：按激活手过滤列表，选中即热替换规划用的 p_tool。

- 点池 = 18003 手上取的自定义点（手系，经 T_wrist2hand 转腕系）
  + 手眼标定的指尖特征点（本来就是腕系）。
- 服务端按激活组合的 hand_id 过滤——激活因时看不到强脑的点。
- 选中立即生效：state.p_tool 是每次规划现读的，下一次 IK 就用新点；
  不选/还原时回启动值（标定 p_tool + tool_out_mm，与老行为一致）。
- 「设为默认」落盘 data/tcp_points/_default.json，18001 重启自动应用。
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from core import tcp_points as tcp_store

from .state import router, state


def _custom_points(hand_id: str) -> list[dict[str, Any]]:
    items = tcp_store.list_points(hand_id)
    if state.T_wrist2hand:
        for item in items:
            item["xyz_wrist"] = tcp_store.hand_to_wrist(
                state.T_wrist2hand, item["xyz_hand"])
    return items


def _calib_points() -> list[dict[str, Any]]:
    items = []
    for point in state.calib_tcp_points:
        entry = {"id": point["id"], "label": point["label"],
                 "xyz_wrist": point["p_wrist_m"]}
        if state.T_wrist2hand:
            entry["xyz_hand"] = tcp_store.wrist_to_hand(
                state.T_wrist2hand, point["p_wrist_m"])
        items.append(entry)
    return items


def apply_selection(kind: str | None, key: str | None) -> dict[str, Any]:
    """把选择落到 state.p_tool。kind=None 还原启动默认。

    抛 ValueError（业务错误）/ FileNotFoundError（点文件没了）。
    """
    if not state.handeye_ready or state.p_tool_startup is None:
        raise ValueError("当前是相机预览/无标定模式，没有 TCP 可换")
    if kind is None:
        state.p_tool = list(state.p_tool_startup)
        state.tcp_selection = None
        return {"selection": None, "p_tool": state.p_tool}

    hand_id = str((state.active_combo or {}).get("hand_id") or "")
    if kind == "custom":
        item = tcp_store.load_point(str(key))
        if item.get("hand_id") != hand_id:
            raise ValueError(
                f"TCP 点属于 {item.get('hand_id')}，不是激活的 {hand_id}")
        if not state.T_wrist2hand:
            raise ValueError("标定文件缺 T_wrist2hand，无法把手系点换算到腕系")
        xyz_wrist = tcp_store.hand_to_wrist(
            state.T_wrist2hand, item["xyz_hand"])
        label = str(item.get("name") or key)
    elif kind == "calib":
        point = next(
            (p for p in state.calib_tcp_points if p["id"] == key), None)
        if point is None:
            raise FileNotFoundError(f"标定点不存在: {key}")
        xyz_wrist = list(point["p_wrist_m"])
        label = str(point["label"])
    else:
        raise ValueError(f"kind 只能是 custom/calib，收到 {kind!r}")

    state.p_tool = [float(v) for v in xyz_wrist]
    state.tcp_selection = {
        "kind": kind, "key": str(key), "label": label,
        "xyz_wrist": state.p_tool,
    }
    return {"selection": state.tcp_selection, "p_tool": state.p_tool}


def apply_startup_default() -> str | None:
    """18001 启动时应用该手的默认 TCP 点（没有就保持标定默认）。"""
    hand_id = str((state.active_combo or {}).get("hand_id") or "")
    if not hand_id or not state.handeye_ready:
        return None
    default = tcp_store.get_default(hand_id)
    if not default:
        return None
    try:
        info = apply_selection(default["kind"], default["key"])
    except (ValueError, FileNotFoundError) as exc:
        return f"默认 TCP 点应用失败（改用标定默认）: {exc}"
    sel = info["selection"]
    xyz = ", ".join(f"{v:.4f}" for v in sel["xyz_wrist"])
    return f"默认 TCP 点已应用: {sel['label']}（腕系 [{xyz}]）"


@router.get("/tcp/points")
def tcp_points_list() -> dict:
    hand_id = str((state.active_combo or {}).get("hand_id") or "")
    if not hand_id or not state.handeye_ready:
        return {"ok": True, "enabled": False, "custom": [], "calib": [],
                "default": None, "selection": None, "p_tool": state.p_tool}
    return {
        "ok": True,
        "enabled": True,
        "hand_id": hand_id,
        "custom": _custom_points(hand_id),
        "calib": _calib_points(),
        "default": tcp_store.get_default(hand_id),
        "selection": state.tcp_selection,
        "p_tool": state.p_tool,
        "p_tool_startup": state.p_tool_startup,
        "has_wrist_transform": bool(state.T_wrist2hand),
    }


@router.post("/tcp/select")
def tcp_select(body: dict):
    kind = body.get("kind") or None
    key = body.get("key") or None
    try:
        info = apply_selection(kind, key)
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)},
                            status_code=404)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)},
                            status_code=422)
    if body.get("set_default"):
        hand_id = str((state.active_combo or {}).get("hand_id") or "")
        if kind is None:
            tcp_store.clear_default(hand_id)
        else:
            tcp_store.set_default(hand_id, str(kind), str(key))
    return {"ok": True, **info}
