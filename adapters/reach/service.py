"""服务状态与全身电机只读接口：/status、/motors。"""

from __future__ import annotations

import math

from fastapi.responses import JSONResponse

from .execution import _exec_status
from .state import router, state


@router.get("/status")
def reach_status():
    if not state.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "robot": state.robot_id,
        "chain_id": state.chain_id,
        "camera": (state.camera.info() if state.camera is not None
                   else {"source": "disabled", "mode": "robot_only"}),
        "calib": state.calib_meta,
        "handeye_ready": state.handeye_ready,
        "camera_only": state.camera_only,
        "robot_only": state.robot_only,
        "p_tool": state.p_tool,
        "p_tool_wrist_m_by_marker": state.p_tool_by_marker,
        "p_tool_reference_marker": state.tool_reference_marker,
        "wrist_link": state.wrist_link,
        "T_cam2root": (None if state.T_cam2root is None
                       else state.T_cam2root.tolist()),
        "arm_supported": state.arm_factory is not None,   # 有真机执行能力
        "armed": state.controller is not None,            # 前端已接管手臂
        "hand_move": bool(state.controller and state.controller.status()["float"]),
        "joints_available": (state.controller is not None
                             or state.provider_reader is not None),
        "exec": _exec_status(),
    }


# H2 全身电机名称表（rt/lowstate 的 motor_state 序号 → 名称）
H2_MOTOR_NAMES = [
    "左腿俯仰", "左腿横滚", "左腿偏航", "左膝俯仰", "左踝横滚", "左踝俯仰",
    "右腿俯仰", "右腿横滚", "右腿偏航", "右膝俯仰", "右踝横滚", "右踝俯仰",
    "腰横滚", "腰俯仰", "腰偏航",
    "左大臂俯仰", "左大臂横滚", "左大臂偏航", "左肘俯仰",
    "左小臂偏航", "左小臂俯仰", "左小臂横滚",
    "右大臂俯仰", "右大臂横滚", "右大臂偏航", "右肘俯仰",
    "右小臂偏航", "右小臂俯仰", "右小臂横滚",
    "脖子俯仰", "脖子偏航",
]

# perp 页底部默认展示：左右腿的俯仰/偏航 + 腰偏航
MOTOR_WATCH_DEFAULT = [0, 2, 6, 8, 14]


@router.get("/motors")
def reach_motors(ids: str = ""):
    """只读全身电机角度（来自 rt/lowstate 订阅，不发任何指令）。

    ids: 逗号分隔的电机序号，缺省 = 左右腿俯仰/偏航 + 腰偏航。
    """
    if state.motors_reader is None:
        return JSONResponse({"ok": False, "error": "无 DDS 连接（--no-robot 模式？）"},
                            status_code=503)
    try:
        indices = ([int(v) for v in ids.split(",") if v.strip()]
                   if ids.strip() else list(MOTOR_WATCH_DEFAULT))
    except ValueError:
        return JSONResponse({"ok": False, "error": f"ids 不合法: {ids!r}"},
                            status_code=422)
    if any(not 0 <= i < len(H2_MOTOR_NAMES) for i in indices):
        return JSONResponse({"ok": False, "error": f"电机序号超范围 0~{len(H2_MOTOR_NAMES)-1}"},
                            status_code=422)
    q = state.motors_reader(indices)
    if q is None:
        return JSONResponse({"ok": False, "error": "还没收到 rt/lowstate 帧"},
                            status_code=503)
    return {"ok": True, "motors": [
        {"index": i, "name": H2_MOTOR_NAMES[i],
         "q_rad": round(float(v), 5), "q_deg": round(math.degrees(float(v)), 2)}
        for i, v in zip(indices, q)]}
