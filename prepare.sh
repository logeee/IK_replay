#!/usr/bin/env bash
# 拨闸服务预备脚本：一次拉起所有常驻服务，可重复执行（已在跑的自动跳过）。
#
#   调度   17001  外部触发入口（按需自动开/关 18001 reach_server）
#   YOLO    7004  常驻推理
#   点云    7005  冻结 RGB-D、双视图选点与三维微调
#   确认台  7002  人工兜底（不想要就注释掉那一行）
#
# reach_server(18001) 不需要在这里启动——任务来了调度服务会自动拉起。
# 以后做成开机服务时，把下面各条 start_one 拆成独立 systemd unit 即可。
#
# 停止全部：./prepare.sh stop   （逐个报告：已关闭 / 没有找到进程）

set -u
cd "$(dirname "$0")"

FASTAPI_PY=/home/robot/miniconda3/envs/fastapi/bin/python
REACH_PORT=18001
REACH_BASE=http://127.0.0.1:$REACH_PORT
HAND_EYE_CALIB=/home/robot/yx/project/calib/hand_eye_3D/handeye3d_data/biaoding/handeye3d_result.json
LOG_DIR=logs/service
mkdir -p "$LOG_DIR"

# ---- 停止模式：./prepare.sh stop ----
stop_one() {   # 用法: stop_one 名字 进程匹配模式 等待秒数
    local name=$1 pattern=$2 wait_s=$3 pids
    pids=$(pgrep -f "$pattern" | tr '\n' ' ')
    if [[ -z "${pids// /}" ]]; then
        echo "[$name] 没有找到进程"
        return
    fi
    pkill -f "$pattern"    # SIGTERM，服务走优雅退出（调度会顺带收掉它拉起的 18001）
    local i
    for ((i = 0; i < wait_s * 2; i++)); do
        if ! pgrep -f "$pattern" >/dev/null; then
            echo "[$name] 已关闭（pid $pids）"
            return
        fi
        sleep 0.5
    done
    pkill -9 -f "$pattern"
    echo "[$name] ⚠ ${wait_s}s 未退出，已强杀（pid $pids）"
}

if [[ "${1:-}" == "stop" ]]; then
    # 先停调度：它的退出钩子会关掉自己拉起的 reach_server(18001)，最长等 15s
    stop_one "调度 " 'python -m api\.dispatch' 25
    stop_one "点云 " 'python -m api\.pointcloud_viewer' 5
    stop_one "YOLO " 'python -m api\.yolo_server' 5
    stop_one "确认台" 'python -m api\.console' 5
    if ss -ltn 2>/dev/null | grep -q ":$REACH_PORT "; then
        echo "[reach] ⚠ $REACH_PORT 仍在监听——应该是手动启动的（谁启动谁负责关）。"
        echo "        如需一并关闭: pkill -f reach_server.py"
    else
        echo "[reach] $REACH_PORT 未在运行（调度拉起的会随调度退出自动关闭）"
    fi
    exit 0
fi

start_one() {   # 用法: start_one 名字 端口 日志文件 命令...
    local name=$1 port=$2 log=$3
    shift 3
    if ss -ltn 2>/dev/null | grep -q ":$port "; then
        echo "[$name] 端口 $port 已被监听，跳过"
        return
    fi
    nohup "$@" >>"$LOG_DIR/$log" 2>&1 &
    disown
    echo "[$name] 启动中 pid=$! 日志=$LOG_DIR/$log"
}

start_one "调度 " 17001 dispatch.log "$FASTAPI_PY" -m api.dispatch \
    --reach-base "$REACH_BASE" \
    --reach-port "$REACH_PORT" \
    --camera-host 127.0.0.1 \
    --calib "$HAND_EYE_CALIB" \
    --tool-out-mm 15
start_one "YOLO " 7004 yolo_server.log "$FASTAPI_PY" -m api.yolo_server \
    --reach-base "$REACH_BASE" \
    --model models/Xuanniu.pt
start_one "点云 " 7005 pointcloud_viewer.log "$FASTAPI_PY" -m api.pointcloud_viewer \
    --reach-base "$REACH_BASE" \
    --model models/Xuanniu.pt \
    --conf 0.25
start_one "确认台" 7002 console.log "$FASTAPI_PY" -m api.console \
    --reach-base "$REACH_BASE"

# ---- 自检：等服务起来后逐个探活 ----
echo
sleep 4
check() {   # 用法: check 名字 URL
    if curl -sf --max-time 3 "$2" >/dev/null; then
        echo "  ✔ $1"
    else
        echo "  ✘ $1 —— 没起来，查日志 $LOG_DIR/"
    fi
}
echo "== 自检 =="
check "调度   17001" "http://127.0.0.1:17001/task/status"
check "YOLO   7004"  "http://127.0.0.1:7004/api/yolo/status"
check "点云   7005"  "http://127.0.0.1:7005/api/pointcloud/status"
check "确认台 7002"  "http://127.0.0.1:7002/api/console/pending"

IP=$(ip route get 8.8.8.8 2>/dev/null | grep -oP 'src \K\S+')
echo
echo "对外入口: POST http://${IP:-<机器人IP>}:17001/task/flip"
echo "流程监控: http://${IP:-<机器人IP>}:17001/"
echo "确认台:   http://${IP:-<机器人IP>}:7002/"
echo "点云选点: http://${IP:-<机器人IP>}:7005/"
