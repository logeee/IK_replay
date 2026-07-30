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

环境障碍：页面「扫描障碍」把当前深度图转成躯干系体素（默认 5cm），
注入碰撞检查——电柜等环境物体也参与轨迹校验。扫描时建议先把手臂放低
（自体过滤会剔除画面里的手臂点，但移出视野更干净）；躯干/腰转动后需重扫。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
HAND_EYE_3D_ROOT = Path("/home/robot/yx/project/calib/hand_eye_3D")

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(HAND_EYE_3D_ROOT))

# 真机试用后退回 20260720 标定（20260727 凌晨的重标 RMS 更小但实测效果反而差，
# 存疑待查；两套必须整组使用：0720 配 --tool-out-mm 10，0726 配 0，不可混搭）
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
    parser.add_argument("--arm-max-speed", type=float, default=0.4,
                        help="最大关节速度 rad/s（默认 0.4）。这是限速天花板，"
                             "正常轨迹快慢仍由执行时长控制；带推力的快拨段需要它放行")
    parser.add_argument("--arm-kp", type=float, default=140.0,
                        help="肩/肘位置环刚度（默认 140，与官方遥操一致）。"
                             "有了重力前馈后不需要再靠大 kp 硬扛下垂")
    parser.add_argument("--arm-kd", type=float, default=3.0,
                        help="肩/肘阻尼（默认 3.0）。kp 调大后如有振颤就同步调大一点")
    parser.add_argument("--arm-kp-wrist", type=float, default=50.0,
                        help="腕部三关节刚度（默认 50）。腕电机额定只有 10Nm，"
                             "跟肩同档会发抖")
    parser.add_argument("--arm-kd-wrist", type=float, default=2.0, help="腕部阻尼（默认 2.0）")
    parser.add_argument("--arm-grav-ff", type=float, default=1.0,
                        help="重力前馈系数（默认 1.0 = 完整补偿，与官方遥操一致）。"
                             "首次上真机想保守可先给 0.5~0.7 看有没有上飘；给 0 关闭")
    parser.add_argument("--arm-payload-kg", type=float, default=0.0,
                        help="URDF 之外的额外手部负载（kg）。换装的因时灵巧手比 URDF 里的"
                             "官方手重就填差值，会加到手掌质心上一起补")
    parser.add_argument("--arm-grav-in-float", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="卸力拖动时也给重力前馈（按实测角实时算）：手臂近似失重、"
                             "推到哪停哪，录路点省力得多（默认开）。补过头会缓慢上飘，"
                             "用 --no-arm-grav-in-float 关闭")
    parser.add_argument("--tool-out-mm", type=float, default=10.0,
                        help="TCP 沿法兰盘法线（腕系 +x）向外的附加偏移，毫米。"
                             "0720 旧标定点的是手指标记点，需要 +10 补到指尖（默认）；"
                             "0726 重标已直接标到指尖尖端，换用它时给 0")
    parser.add_argument("--arm-imu-gravity", action="store_true",
                        help="用 IMU 实测姿态修正重力方向（躯干前倾/后仰时更准）。"
                             "先看页面诊断里的 IMU 数值是否合理再开")
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
    torso_reader = None
    motors_reader = None
    arm_factory = None
    if not args.no_robot:
        try:
            from backend.robot import H2PoseProvider  # hand_eye_3D（只读订阅）

            provider = H2PoseProvider(network_interface=args.network_interface, arm=arm)
            joints_reader = provider.read_arm_q
            torso_reader = provider.read_torso_state
            motors_reader = provider.read_motor_q
            print("[reach] rt/lowstate 只读订阅就绪（不发任何指令）")
        except Exception as exc:
            print(f"[reach] DDS 连接失败，退化为仅模拟模式: {exc}")

        if joints_reader is not None:
            from backend.arm import H2ArmController  # hand_eye_3D

            def arm_factory():
                print(f"[reach] !!! 前端请求接管手臂：开始发布 rt/arm_sdk "
                      f"(kp={args.arm_kp}/{args.arm_kp_wrist}, "
                      f"kd={args.arm_kd}/{args.arm_kd_wrist}, "
                      f"重力前馈 α={args.arm_grav_ff}, 负载 {args.arm_payload_kg}kg)。")
                ctl = H2ArmController(arm=arm, network_interface=args.network_interface,
                                      max_speed_rad_s=args.arm_max_speed,
                                      kp=args.arm_kp, kd=args.arm_kd,
                                      kp_wrist=args.arm_kp_wrist, kd_wrist=args.arm_kd_wrist,
                                      grav_alpha=args.arm_grav_ff,
                                      payload_kg=args.arm_payload_kg,
                                      grav_in_float=args.arm_grav_in_float,
                                      use_imu_gravity=args.arm_imu_gravity)
                print(f"[reach] 重力前馈: {ctl.describe_gravity()}")
                return ctl

    reach.configure(
        camera=camera, robot_model=robot_model, robot_id=args.robot,
        chain_id=args.chain, calib_path=args.calib,
        collision_checker=app_module.collision_checkers[args.robot],
        ik_solver=app_module.solvers[args.robot]["numerical"],
        arm_factory=arm_factory, joints_reader=joints_reader,
        torso_reader=torso_reader, motors_reader=motors_reader,
        tool_out_mm=args.tool_out_mm,
    )
    app_module.app.include_router(reach.router)
    print(f"[reach] calib = {reach.state.calib_meta}")
    print(f"[reach] p_tool(TCP) = {reach.state.p_tool}")
    print(f"[reach] 真机执行能力 = {'可用（由页面「接管手臂」触发）' if arm_factory else '不可用'}")
    print(f"[reach] 执行诊断日志 = {reach.state.log_dir}/reach_<日期>.jsonl（每段动作一行）")
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
