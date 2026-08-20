#!/usr/bin/env bash
# 重力补偿标定实验台（18002）。只启动网页编排服务，不启动或接管 18001。

set -u
cd "$(dirname "$0")"

PYTHON=/home/robot/miniconda3/envs/fastapi/bin/python
PORT=18002
REACH_BASE=http://127.0.0.1:18001
LOG_DIR=logs/service
PID_FILE="$LOG_DIR/gravity_calibration.pid"
LOG_FILE="$LOG_DIR/gravity_calibration.log"
mkdir -p "$LOG_DIR"

if [[ "${1:-}" == "stop" ]]; then
    if [[ -f "$PID_FILE" ]]; then
        pid=$(<"$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            for _ in {1..20}; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.2
            done
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
            echo "[重力标定] 已关闭（pid $pid）"
        else
            echo "[重力标定] PID 文件已过期"
        fi
        rm -f "$PID_FILE"
    else
        echo "[重力标定] 没有找到 PID 文件"
    fi
    exit 0
fi

if ss -ltn 2>/dev/null | rg -q ":$PORT "; then
    echo "[重力标定] 端口 $PORT 已被监听，未重复启动"
    exit 0
fi

nohup "$PYTHON" -m api.gravity_calibration \
    --port "$PORT" \
    --reach-base "$REACH_BASE" \
    >>"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" >"$PID_FILE"
disown

sleep 1
if ! kill -0 "$pid" 2>/dev/null; then
    echo "[重力标定] 启动失败，请查看 $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi

IP=$(ip route get 8.8.8.8 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}')
echo "[重力标定] 已启动 pid=$pid"
echo "[重力标定] 页面: http://${IP:-127.0.0.1}:$PORT/"
echo "[重力标定] 18001需要另行运行: $REACH_BASE"
