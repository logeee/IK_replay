#!/usr/bin/env python3
"""IK_replay + reach adapter 启动入口：点击相机取目标 → IK 预演 → 确认后真机执行。

不加任何参数时等价于原离线查看器（app.py），reach 面板不出现。
相机 / 手臂控制模块复用 calib/hand_eye_3D 项目。

是否接管手臂（真机执行）由【前端页面按钮】决定：
服务器启动时只做 rt/lowstate 只读订阅；页面上点「接管手臂」后才创建
控制器、发布 rt/arm_sdk；「释放手臂」权重渐出交还本体控制器。

示例
----
纯模拟联调（假相机，无机器人）:
    python reach_server.py --camera-source mock --no-robot

正常启动（真相机 + DDS 只读；执行与否由页面决定）:
    python reach_server.py --camera-serial CP0BB53000FS --network-interface enp86s0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
HAND_EYE_3D_ROOT = Path("/home/robot/yx/project/calib/hand_eye_3D")

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(HAND_EYE_3D_ROOT))

DEFAULT_CALIB = (HAND_EYE_3D_ROOT / "handeye3d_data" / "20260720_230131"
                 / "handeye3d_result.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="IK replay viewer + click-to-reach adapter")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)

    parser.add_argument("--robot", default="h2", help="使用的机器人配置（默认 h2）")
    parser.add_argument("--chain", default="right_arm", help="执行链（默认 right_arm）")
    parser.add_argument("--calib", type=Path, default=DEFAULT_CALIB,
                        help="hand_eye_3D 的 handeye3d_result.json 路径")

    parser.add_argument("--camera-source", choices=["orbbec", "mock"], default="orbbec")
    parser.add_argument("--camera-serial", default=None, help="Orbbec 序列号（默认第一台）")

    parser.add_argument("--no-robot", action="store_true",
                        help="不连 DDS（纯模拟联调，页面上无法接管手臂）")
    parser.add_argument("--network-interface", default=None, help="DDS 网卡，如 enp86s0")
    parser.add_argument("--arm-max-speed", type=float, default=0.2,
                        help="执行时的最大关节速度 rad/s（默认 0.2）")
    args = parser.parse_args()

    if not args.calib.exists():
        print(f"[reach] 标定文件不存在: {args.calib}")
        return 1

    # 主应用（离线查看器 + IK/规划 API）原样加载
    import app as app_module
    from adapters import reach

    if args.robot not in app_module.robots:
        print(f"[reach] 未知机器人 {args.robot!r}，可选: {sorted(app_module.robots)}")
        return 1
    robot_model = app_module.robots[args.robot]

    from backend.camera import make_camera  # hand_eye_3D

    camera = make_camera(args.camera_source, serial=args.camera_serial)
    camera.start()
    print(f"[reach] camera = {args.camera_source}: {camera.info()}")

    arm = "right" if args.chain == "right_arm" else "left"
    joints_reader = None
    arm_factory = None
    if not args.no_robot:
        try:
            from backend.robot import H2PoseProvider  # hand_eye_3D（只读订阅）

            provider = H2PoseProvider(network_interface=args.network_interface, arm=arm)
            joints_reader = provider.read_arm_q
            print("[reach] rt/lowstate 只读订阅就绪（不发任何指令）")
        except Exception as exc:
            print(f"[reach] DDS 连接失败，退化为仅模拟模式: {exc}")

        if joints_reader is not None:
            from backend.arm import H2ArmController  # hand_eye_3D

            def arm_factory():
                print("[reach] !!! 前端请求接管手臂：开始发布 rt/arm_sdk。")
                return H2ArmController(arm=arm, network_interface=args.network_interface,
                                       max_speed_rad_s=args.arm_max_speed)

    reach.configure(
        camera=camera, robot_model=robot_model, robot_id=args.robot,
        chain_id=args.chain, calib_path=args.calib,
        arm_factory=arm_factory, joints_reader=joints_reader,
    )
    app_module.app.include_router(reach.router)
    print(f"[reach] calib = {reach.state.calib_meta}")
    print(f"[reach] p_tool(TCP) = {reach.state.p_tool}")
    print(f"[reach] 真机执行能力 = {'可用（由页面「接管手臂」触发）' if arm_factory else '不可用'}")
    print(f"[reach] serving on http://{args.host}:{args.port}")

    import uvicorn
    try:
        uvicorn.run(app_module.app, host=args.host, port=args.port)
    finally:
        if reach.state.controller is not None:
            print("[reach] 手臂仍处于接管状态，权重渐出交还本体控制器（请扶住手臂）...")
            reach.state.controller.shutdown()
        camera.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
