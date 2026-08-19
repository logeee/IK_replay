#!/usr/bin/env bash
# 拨闸 API 真机联调服务。
#
# 启动：./prepare-test.sh
# 停止：./prepare-test.sh stop
#
# 对外仍监听 17001，但收到 /task/flip 后只执行：
# 接管右臂 → 关节插值到固定测试路点 → 保持 2s → 释放。

set -u
set -o pipefail
cd "$(dirname "$0")"

PYTHON=/home/robot/miniconda3/envs/fastapi/bin/python
PORT=17001
REACH_PORT=18001
REACH_BASE=http://127.0.0.1:$REACH_PORT
WAYPOINT=/home/robot/yx/project/IK_replay/data/waypoints/起手点测试_20260721_042250.json
CALIB=/home/robot/yx/project/calib/hand_eye_3D/handeye3d_data/biaoding/handeye3d_result.json
LOG_DIR=logs/service
LOG_FILE=$LOG_DIR/test_dispatch.log
PID_FILE=$LOG_DIR/test_dispatch.pid

mkdir -p "$LOG_DIR"

is_test_process() {
    local pid=$1
    [[ -r "/proc/$pid/cmdline" ]] || return 1
    tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q 'api\.test_dispatch'
}

stop_test() {
    local pids="" pid
    if [[ -f "$PID_FILE" ]]; then
        pid=$(<"$PID_FILE")
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && is_test_process "$pid"; then
            pids=$pid
        fi
    fi
    if [[ -z "$pids" ]]; then
        pids=$(pgrep -f 'python.*-m api\.test_dispatch' | tr '\n' ' ')
    fi
    if [[ -z "${pids// /}" ]]; then
        rm -f "$PID_FILE"
        echo "[测试API] 没有找到进程"
        return
    fi

    echo "[测试API] 正在停止（pid $pids）：先急停、释放手臂，再关闭 Reach…"
    kill $pids 2>/dev/null || true
    for ((i = 0; i < 50; i++)); do
        local alive=""
        for pid in $pids; do
            kill -0 "$pid" 2>/dev/null && alive="$alive $pid"
        done
        if [[ -z "$alive" ]]; then
            rm -f "$PID_FILE"
            echo "[测试API] 已安全关闭"
            return
        fi
        sleep 0.5
    done
    for pid in $pids; do
        kill -9 "$pid" 2>/dev/null || true
    done
    rm -f "$PID_FILE"
    echo "[测试API] ⚠ 25s 内未退出，已强制终止；请确认手臂已释放"
}

if [[ "${1:-}" == "stop" ]]; then
    stop_test
    exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "[测试API] Python 不存在或不可执行: $PYTHON"
    exit 1
fi
if [[ ! -f "$WAYPOINT" ]]; then
    echo "[测试API] 测试路点不存在: $WAYPOINT"
    exit 1
fi
if [[ ! -f "$CALIB" ]]; then
    echo "[测试API] 标定文件不存在: $CALIB"
    exit 1
fi
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    echo "[测试API] 端口 $PORT 已被监听，测试服务与正式 prepare.sh 不能同时运行"
    echo "          请先关闭占用 $PORT 的服务"
    exit 1
fi

nohup "$PYTHON" -m api.test_dispatch \
    --port "$PORT" \
    --reach-base "$REACH_BASE" \
    --reach-port "$REACH_PORT" \
    --network-interface enp86s0 \
    --calib "$CALIB" \
    --tool-out-mm 15 \
    --waypoint "$WAYPOINT" \
    --max-speed 0.2 \
    --hold-seconds 2 \
    >>"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" >"$PID_FILE"
echo "[测试API] 启动中 pid=$pid 日志=$LOG_FILE"

for ((i = 0; i < 20; i++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "[测试API] 启动失败，请查看 $LOG_FILE"
        exit 1
    fi
    if curl -sf --max-time 1 "http://127.0.0.1:$PORT/" >/dev/null; then
        IP=$(ip route get 8.8.8.8 2>/dev/null | grep -oP 'src \K\S+')
        echo "[测试API] 已就绪: http://${IP:-<机器人IP>}:$PORT"
        echo "[测试API] ⚠ 调用 POST /task/flip 会接管并真实移动右臂"
        exit 0
    fi
    sleep 0.5
done

echo "[测试API] 10s 内未就绪，请查看 $LOG_FILE"
stop_test
exit 1
