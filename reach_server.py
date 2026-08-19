#!/usr/bin/env python3
"""IK_replay + reach adapter 启动入口：点击相机取目标 → IK 预演 → 确认后真机执行。

不加任何参数时等价于原离线查看器（app.py），reach 面板不出现。
生产相机从外部 teleimager ZMQ 只读获取；本项目不会启动或修改推流服务。
Orbbec SDK 直连仅用于显式调试/标定，手臂控制模块仍复用 hand_eye_3D。

是否接管手臂（真机执行）由【前端页面按钮】决定：
服务器启动时只做 rt/lowstate 只读订阅；页面上点「接管手臂」后才创建
控制器、发布 rt/arm_sdk；「释放手臂」权重渐出交还本体控制器。

示例
----
纯模拟联调（假相机，无机器人）:
    python reach_server.py --camera-source mock --no-robot

无手眼标定的相机预览（仅视频/深度观测，禁用机器人操作）:
    python reach_server.py --camera-only --camera-host 127.0.0.1

生产启动（外部 ZMQ RGB-D + DDS 只读；执行与否由页面决定）:
    python reach_server.py --camera-host 192.168.123.164 --network-interface enp86s0

显式 SDK 调试（会在本机打开相机，生产禁止使用）:
    python reach_server.py --camera-source orbbec \
      --camera-serial CP0BB53000FS --network-interface enp86s0

环境障碍：页面「扫描障碍」把当前深度图转成躯干系体素（默认 5cm），
注入碰撞检查——电柜等环境物体也参与轨迹校验。扫描时建议先把手臂放低
（自体过滤会剔除画面里的手臂点，但移出视野更干净）；躯干/腰转动后需重扫。
"""

from __future__ import annotations

import argparse
import fcntl
import socket
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
HAND_EYE_3D_ROOT = Path("/home/robot/yx/project/calib/hand_eye_3D")

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(HAND_EYE_3D_ROOT))

# 当前机器人使用 2026-08-18 多标记联合标定；TCP 取红蓝中点，
# 再沿腕系 +x 外移 15 mm。
DEFAULT_CALIB = (HAND_EYE_3D_ROOT / "handeye3d_data" / "biaoding"
                 / "handeye3d_result.json")
DEFAULT_RGBD_CALIB = PROJECT_ROOT / "config" / "camera" / "orbbec_rgbd_calibration.json"
DEFAULT_CAMERA_CONFIG_CACHE = PROJECT_ROOT / "config" / "camera" / "teleimager_config_cache.json"


def _browser_urls(host: str, port: int) -> list[str]:
    """列出真实可访问地址，过滤 Docker/虚拟网桥。"""
    if host not in {"0.0.0.0", "::"}:
        return [f"http://{host}:{port}/"]
    addresses: set[str] = set()
    for _, name in socket.if_nameindex():
        if (name == "lo" or name.startswith(("docker", "br-", "veth", "virbr"))):
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = struct.pack("256s", name.encode("utf-8")[:15])
            result = fcntl.ioctl(sock.fileno(), 0x8915, packed)  # SIOCGIFADDR
            addresses.add(socket.inet_ntoa(result[20:24]))
        except OSError:
            pass
        finally:
            sock.close()
    if not addresses:
        return [f"http://127.0.0.1:{port}/"]
    return [f"http://{address}:{port}/" for address in sorted(addresses)]


def main() -> int:
    parser = argparse.ArgumentParser(description="IK replay viewer + click-to-reach adapter")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18001)

    parser.add_argument("--robot", default="h2", help="使用的机器人配置（默认 h2）")
    parser.add_argument("--chain", default="right_arm", help="执行链（默认 right_arm）")
    parser.add_argument("--calib", type=Path, default=DEFAULT_CALIB,
                        help="hand_eye_3D 的 handeye3d_result.json 路径")
    runtime_mode = parser.add_mutually_exclusive_group()
    runtime_mode.add_argument(
        "--camera-only",
        action="store_true",
        help="无手眼标定预览：只开放相机流/深度观测，禁用 DDS、规划和执行",
    )
    runtime_mode.add_argument(
        "--robot-only",
        action="store_true",
        help="无相机控制模式：只连接 DDS 并开放关节读取/手臂执行，供 API 联调",
    )

    parser.add_argument("--camera-source", choices=["zmq", "orbbec", "mock"], default="zmq",
                        help="生产默认 zmq；orbbec 会主动打开本机相机，仅限调试")
    parser.add_argument("--camera-serial", default=None,
                        help="仅 --camera-source orbbec 使用的 Orbbec 序列号")
    parser.add_argument("--camera-host", default="127.0.0.1",
                        help="teleimager 主机地址")
    parser.add_argument("--camera-request-port", type=int, default=60000,
                        help="teleimager 配置请求端口")
    parser.add_argument("--camera-port", type=int, default=None,
                        help="RGB-D ZMQ 端口；默认通过配置请求获取，服务无配置接口时可显式给出")
    parser.add_argument("--camera-name", default="head_rgbd_camera",
                        help="teleimager RGB-D stream 名称")
    parser.add_argument("--camera-rgbd-calib", type=Path, default=DEFAULT_RGBD_CALIB,
                        help="SDK 调试工具一次性导出的本地 RGB-D 标定 JSON")
    parser.add_argument("--camera-config-cache", type=Path, default=DEFAULT_CAMERA_CONFIG_CACHE,
                        help="teleimager 只读配置的本地缓存")
    parser.add_argument("--camera-stale-after", type=float, default=2.0,
                        help="超过该秒数未收到 RGB-D 帧即视为过期")

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
    parser.add_argument("--tool-out-mm", type=float, default=15.0,
                        help="TCP 沿法兰盘法线（腕系 +x）向外的附加偏移，毫米。"
                             "当前默认在红蓝中点基础上向外 15 mm")
    parser.add_argument("--arm-imu-gravity", action="store_true",
                        help="用 IMU 实测姿态修正重力方向（躯干前倾/后仰时更准）。"
                             "先看页面诊断里的 IMU 数值是否合理再开")
    args = parser.parse_args()

    if not args.camera_only and not args.calib.exists():
        print(f"[reach] 标定文件不存在: {args.calib}")
        return 1
    if args.camera_only:
        print("[reach] 相机预览模式：不加载手眼标定，不连接/控制机器人")
    if args.robot_only:
        print("[reach] 机器人控制模式：不连接相机，只开放 DDS 和手臂执行")

    # 主应用（离线查看器 + IK/规划 API）原样加载
    import app as app_module
    from adapters import reach

    if args.robot not in app_module.robots:
        print(f"[reach] 未知机器人 {args.robot!r}，可选: {sorted(app_module.robots)}")
        return 1
    robot_model = app_module.robots[args.robot]

    camera = None
    if not args.robot_only:
        if args.camera_source == "zmq":
            from camera_sources import ZmqRGBDCamera

            camera = ZmqRGBDCamera(
                host=args.camera_host,
                calibration_path=args.camera_rgbd_calib,
                camera_name=args.camera_name,
                request_port=args.camera_request_port,
                stream_port=args.camera_port,
                config_cache_path=args.camera_config_cache,
                stale_after_s=args.camera_stale_after,
            )
        else:
            # 只有显式选择 orbbec 才 import SDK 后端并可能打开本机设备。
            from backend.camera import make_camera  # hand_eye_3D

            camera = make_camera(args.camera_source, serial=args.camera_serial)
        camera.start()
        print(f"[reach] camera = {args.camera_source}: {camera.info()}")

    arm = "right" if args.chain == "right_arm" else "left"
    joints_reader = None
    torso_reader = None
    motors_reader = None
    arm_factory = None
    if not args.no_robot and not args.camera_only:
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
        chain_id=args.chain,
        calib_path=None if args.camera_only else args.calib,
        camera_only=args.camera_only,
        robot_only=args.robot_only,
        collision_checker=app_module.collision_checkers[args.robot],
        ik_solver=app_module.solvers[args.robot]["numerical"],
        arm_factory=arm_factory, joints_reader=joints_reader,
        torso_reader=torso_reader, motors_reader=motors_reader,
        tool_out_mm=args.tool_out_mm,
    )
    if args.camera_only:
        from fastapi.responses import JSONResponse

        allowed_preview_paths = {
            "/api/reach/status",
            "/api/reach/stream",
            "/api/reach/perpendicular",
            "/api/reach/rgbd_snapshot",
        }

        @app_module.app.middleware("http")
        async def guard_camera_only(request, call_next):
            path = request.url.path
            if (path.startswith("/api/reach/")
                    and path not in allowed_preview_paths
                    and request.method != "OPTIONS"):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "相机预览模式：缺少手眼标定，机器人坐标、规划和执行均已禁用",
                    },
                    status_code=409,
                )
            return await call_next(request)

    app_module.app.include_router(reach.router)
    print(f"[reach] calib = {reach.state.calib_meta}")
    print(f"[reach] p_tool(TCP) = {reach.state.p_tool}")
    print(f"[reach] 真机执行能力 = {'可用（由页面「接管手臂」触发）' if arm_factory else '不可用'}")
    print(f"[reach] 执行诊断日志 = {reach.state.log_dir}/reach_<日期>.jsonl（每段动作一行）")
    print("[reach] 浏览器访问地址:")
    for url in _browser_urls(args.host, args.port):
        print(f"  {url}")

    import uvicorn
    try:
        uvicorn.run(app_module.app, host=args.host, port=args.port)
    finally:
        if reach.state.controller is not None:
            print("[reach] 手臂仍处于接管状态，权重渐出交还本体控制器（请扶住手臂）...")
            reach.state.controller.shutdown()
        if camera is not None:
            camera.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
