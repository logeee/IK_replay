#!/usr/bin/env bash
# 18001 + 7004 + 7005 专用点云选点与拨动核验启动器。
#
# 启动：./prepare-pointcloud.sh
# 状态：./prepare-pointcloud.sh status
# 停止：./prepare-pointcloud.sh stop
#
# 如果服务已由用户手动启动，本脚本会复用它；
# stop 只关闭由本脚本启动并记录 PID 的进程，不会误关外部 18001。

set -u
set -o pipefail
cd "$(dirname "$0")"

PYTHON=${PYTHON:-/home/robot/miniconda3/envs/fastapi/bin/python}
REACH_PORT=${REACH_PORT:-18001}
YOLO_PORT=${YOLO_PORT:-7004}
POINTCLOUD_PORT=${POINTCLOUD_PORT:-7005}
REACH_BASE="http://127.0.0.1:$REACH_PORT"
YOLO_BASE="http://127.0.0.1:$YOLO_PORT"
NETWORK_INTERFACE=${NETWORK_INTERFACE:-enp86s0}
CAMERA_HOST=${CAMERA_HOST:-127.0.0.1}
# 默认留空：由 reach_server 严格读取 18000 当前激活臂+手型号的归档。
# 仅调试其他标定时通过环境变量显式覆盖。
HAND_EYE_CALIB=${HAND_EYE_CALIB:-}
TOOL_OUT_MM=${TOOL_OUT_MM:-}
POINTCLOUD_MODEL=${POINTCLOUD_MODEL:-models/Xuanniu_D.pt}
POINTCLOUD_CONF=${POINTCLOUD_CONF:-0.25}
YOLO_MODEL=${YOLO_MODEL:-$POINTCLOUD_MODEL}
YOLO_CONF=${YOLO_CONF:-$POINTCLOUD_CONF}
REQUIRE_YOLO_MODEL=${REQUIRE_YOLO_MODEL:-0}

LOG_DIR=logs/service
REACH_LOG="$LOG_DIR/pointcloud_reach.log"
YOLO_LOG="$LOG_DIR/pointcloud_yolo.log"
VIEWER_LOG="$LOG_DIR/pointcloud_viewer.log"
REACH_PID_FILE="$LOG_DIR/pointcloud_reach.pid"
YOLO_PID_FILE="$LOG_DIR/pointcloud_yolo.pid"
VIEWER_PID_FILE="$LOG_DIR/pointcloud_viewer.pid"
REACH_TOKEN="reach_server.py --port $REACH_PORT"
YOLO_TOKEN="api.yolo_server --port $YOLO_PORT"
VIEWER_TOKEN="api.pointcloud_viewer --port $POINTCLOUD_PORT"
mkdir -p "$LOG_DIR"

port_in_use() {
    ss -ltn 2>/dev/null | awk -v port="$1" \
        '$4 ~ (":" port "$") {found=1} END {exit !found}'
}

healthy() {
    curl -sf --max-time 2 "$1" >/dev/null
}

owned_pid() {
    local pid_file=$1 token=$2 pid cmdline
    [[ -f "$pid_file" ]] || return 1
    pid=$(<"$pid_file")
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    [[ -r "/proc/$pid/cmdline" ]] || return 1
    cmdline=$(tr '\0' ' ' <"/proc/$pid/cmdline")
    [[ "$cmdline" == *"$token"* ]]
}

wait_until_ready() {
    local name=$1 url=$2 pid=$3 attempts=$4 log_file=$5
    local i
    for ((i=0; i<attempts; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "[$name] 启动进程已退出，请查看 $log_file"
            return 1
        fi
        if healthy "$url"; then
            return 0
        fi
        sleep 0.5
    done
    echo "[$name] 等待就绪超时，请查看 $log_file"
    return 1
}

stop_owned() {
    local name=$1 pid_file=$2 token=$3 wait_steps=$4
    local pid i
    if ! owned_pid "$pid_file" "$token"; then
        rm -f "$pid_file"
        echo "[$name] 没有由本脚本启动的进程"
        return
    fi
    pid=$(<"$pid_file")
    kill "$pid" 2>/dev/null || true
    for ((i=0; i<wait_steps; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pid_file"
            echo "[$name] 已关闭（pid $pid）"
            return
        fi
        sleep 0.5
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$pid_file"
    echo "[$name] ⚠ 优雅退出超时，已终止（pid $pid）"
}

show_status() {
    if healthy "$REACH_BASE/api/reach/status"; then
        if owned_pid "$REACH_PID_FILE" "$REACH_TOKEN"; then
            echo "[18001] 运行中，由本脚本启动（pid $(<"$REACH_PID_FILE")）"
        else
            echo "[18001] 运行中，外部进程（本脚本不会关闭）"
        fi
    else
        echo "[18001] 未就绪"
    fi
    if healthy "$YOLO_BASE/api/yolo/status"; then
        if owned_pid "$YOLO_PID_FILE" "$YOLO_TOKEN"; then
            echo "[7004] 运行中，由本脚本启动（pid $(<"$YOLO_PID_FILE")）"
        else
            echo "[7004] 运行中，外部进程（本脚本不会关闭）"
        fi
    else
        echo "[7004] 未就绪"
    fi
    if healthy "http://127.0.0.1:$POINTCLOUD_PORT/api/pointcloud/status"; then
        if owned_pid "$VIEWER_PID_FILE" "$VIEWER_TOKEN"; then
            echo "[7005] 运行中，由本脚本启动（pid $(<"$VIEWER_PID_FILE")）"
        else
            echo "[7005] 运行中，外部进程（本脚本不会关闭）"
        fi
    else
        echo "[7005] 未就绪"
    fi
}

case "${1:-start}" in
    stop)
        stop_owned "7005点云" "$VIEWER_PID_FILE" "$VIEWER_TOKEN" 20
        stop_owned "7004 YOLO" "$YOLO_PID_FILE" "$YOLO_TOKEN" 20
        stop_owned "18001 Reach" "$REACH_PID_FILE" "$REACH_TOKEN" 50
        exit 0
        ;;
    status)
        show_status
        exit 0
        ;;
    start)
        ;;
    *)
        echo "用法: $0 [start|status|stop]"
        exit 2
        ;;
esac

if [[ ! -x "$PYTHON" ]]; then
    echo "[启动失败] Python 不存在或不可执行: $PYTHON"
    exit 1
fi
if [[ -n "$HAND_EYE_CALIB" && ! -f "$HAND_EYE_CALIB" ]]; then
    echo "[启动失败] 手眼标定文件不存在: $HAND_EYE_CALIB"
    exit 1
fi
yolo_degraded=0
pointcloud_degraded=0
if [[ ! -f "$YOLO_MODEL" ]]; then
    yolo_degraded=1
fi
if [[ ! -f "$POINTCLOUD_MODEL" ]]; then
    pointcloud_degraded=1
fi
if (( yolo_degraded || pointcloud_degraded )); then
    if [[ "$REQUIRE_YOLO_MODEL" == "1" ]]; then
        echo "[启动失败] YOLO 模型不存在（REQUIRE_YOLO_MODEL=1）"
        (( yolo_degraded )) && echo "  7004: $YOLO_MODEL"
        (( pointcloud_degraded )) && echo "  7005: $POINTCLOUD_MODEL"
        exit 1
    fi
    (( yolo_degraded )) && \
        echo "[启动警告] 7004 YOLO 模型不存在: $YOLO_MODEL（核验接口降级）"
    (( pointcloud_degraded )) && \
        echo "[启动警告] 7005 YOLO 模型不存在: $POINTCLOUD_MODEL（保留手动点云选点）"
fi

started_reach=0
if healthy "$REACH_BASE/api/reach/status"; then
    echo "[18001] 已经就绪，直接复用"
elif port_in_use "$REACH_PORT"; then
    echo "[启动失败] 端口 $REACH_PORT 已被其他服务占用，但 Reach 状态接口不可达"
    exit 1
else
    reach_args=(
        reach_server.py
        --port "$REACH_PORT"
        --camera-source zmq
        --camera-host "$CAMERA_HOST"
        --network-interface "$NETWORK_INTERFACE"
        --yolo-base "$YOLO_BASE"
    )
    [[ -n "$HAND_EYE_CALIB" ]] && reach_args+=(--calib "$HAND_EYE_CALIB")
    [[ -n "$TOOL_OUT_MM" ]] && reach_args+=(--tool-out-mm "$TOOL_OUT_MM")
    nohup env PYTHONUNBUFFERED=1 "$PYTHON" "${reach_args[@]}" \
        >>"$REACH_LOG" 2>&1 &
    reach_pid=$!
    echo "$reach_pid" >"$REACH_PID_FILE"
    started_reach=1
    echo "[18001] 启动中 pid=$reach_pid 日志=$REACH_LOG"
    if ! wait_until_ready \
        "18001" "$REACH_BASE/api/reach/status" "$reach_pid" 80 "$REACH_LOG"; then
        stop_owned "18001 Reach" "$REACH_PID_FILE" "$REACH_TOKEN" 20
        exit 1
    fi
    echo "[18001] 已就绪"
fi

started_yolo=0
if healthy "$YOLO_BASE/api/yolo/status"; then
    echo "[7004] 已经就绪，未重复启动"
elif port_in_use "$YOLO_PORT"; then
    echo "[启动失败] 端口 $YOLO_PORT 已被其他服务占用"
    if (( started_reach )); then
        stop_owned "18001 Reach" "$REACH_PID_FILE" "$REACH_TOKEN" 50
    fi
    exit 1
else
    nohup env PYTHONUNBUFFERED=1 "$PYTHON" -m api.yolo_server \
        --port "$YOLO_PORT" \
        --reach-base "$REACH_BASE" \
        --model "$YOLO_MODEL" \
        --conf "$YOLO_CONF" \
        >>"$YOLO_LOG" 2>&1 &
    yolo_pid=$!
    echo "$yolo_pid" >"$YOLO_PID_FILE"
    started_yolo=1
    echo "[7004] 启动中 pid=$yolo_pid 日志=$YOLO_LOG"
    if ! wait_until_ready \
        "7004" "$YOLO_BASE/api/yolo/status" "$yolo_pid" 80 "$YOLO_LOG"; then
        stop_owned "7004 YOLO" "$YOLO_PID_FILE" "$YOLO_TOKEN" 20
        if (( started_reach )); then
            stop_owned "18001 Reach" "$REACH_PID_FILE" "$REACH_TOKEN" 50
        fi
        exit 1
    fi
    echo "[7004] 已就绪"
fi

if healthy "http://127.0.0.1:$POINTCLOUD_PORT/api/pointcloud/status"; then
    echo "[7005] 已经就绪，未重复启动"
elif port_in_use "$POINTCLOUD_PORT"; then
    echo "[启动失败] 端口 $POINTCLOUD_PORT 已被其他服务占用"
    if (( started_yolo )); then
        stop_owned "7004 YOLO" "$YOLO_PID_FILE" "$YOLO_TOKEN" 20
    fi
    if (( started_reach )); then
        stop_owned "18001 Reach" "$REACH_PID_FILE" "$REACH_TOKEN" 50
    fi
    exit 1
else
    nohup env PYTHONUNBUFFERED=1 "$PYTHON" -m api.pointcloud_viewer \
        --port "$POINTCLOUD_PORT" \
        --reach-base "$REACH_BASE" \
        --model "$POINTCLOUD_MODEL" \
        --conf "$POINTCLOUD_CONF" \
        >>"$VIEWER_LOG" 2>&1 &
    viewer_pid=$!
    echo "$viewer_pid" >"$VIEWER_PID_FILE"
    echo "[7005] 启动中 pid=$viewer_pid 日志=$VIEWER_LOG"
    if ! wait_until_ready \
        "7005" "http://127.0.0.1:$POINTCLOUD_PORT/api/pointcloud/status" \
        "$viewer_pid" 80 "$VIEWER_LOG"; then
        stop_owned "7005点云" "$VIEWER_PID_FILE" "$VIEWER_TOKEN" 20
        if (( started_yolo )); then
            stop_owned "7004 YOLO" "$YOLO_PID_FILE" "$YOLO_TOKEN" 20
        fi
        if (( started_reach )); then
            stop_owned "18001 Reach" "$REACH_PID_FILE" "$REACH_TOKEN" 50
        fi
        exit 1
    fi
    echo "[7005] 已就绪"
fi

IP=$(ip route get 8.8.8.8 2>/dev/null | awk \
    '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}')
echo
if (( yolo_degraded )); then
    echo "YOLO 核验服务:  $YOLO_BASE/（未加载模型）"
else
    echo "YOLO 核验服务:  $YOLO_BASE/"
fi
echo "点云选点页面: http://${IP:-127.0.0.1}:$POINTCLOUD_PORT/"
echo "停止本脚本启动的服务: $0 stop"
