#!/usr/bin/env bash
# 一键构建并启动拨动历史可视化；默认端口 7010。
#
#   ./look-history.sh          启动（已运行则只打印地址）
#   ./look-history.sh stop     停止
#   ./look-history.sh restart  重启

set -euo pipefail
cd "$(dirname "$0")"

PORT="${HISTORY_PORT:-7010}"
FASTAPI_PY=/home/robot/miniconda3/envs/fastapi/bin/python
WEB_DIR=web-picks
LOG_DIR=logs/service
PID_FILE="$LOG_DIR/picks_history.pid"
LOG_FILE="$LOG_DIR/picks_history.log"

mkdir -p "$LOG_DIR"

running_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(<"$PID_FILE")
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            printf '%s' "$pid"
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    return 1
}

stop_server() {
    local pid
    if ! pid=$(running_pid); then
        echo "[历史记录] 没有找到由本脚本启动的服务"
        return
    fi
    kill "$pid"
    for _ in {1..20}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "[历史记录] 已关闭（pid $pid）"
            return
        fi
        sleep 0.25
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "[历史记录] 超时未退出，已强制关闭（pid $pid）"
}

case "${1:-start}" in
    stop)
        stop_server
        exit 0
        ;;
    restart)
        stop_server
        ;;
    start|"")
        ;;
    *)
        echo "用法: $0 [start|stop|restart]"
        exit 2
        ;;
esac

if pid=$(running_pid); then
    echo "[历史记录] 已在运行（pid $pid，端口 $PORT）"
elif curl -sf --max-time 1 "http://127.0.0.1:$PORT/api/picks?limit=1" \
    >/dev/null; then
    echo "[历史记录] 端口 $PORT 上已有历史查看服务，直接使用"
elif [[ -n "$(ss -ltnH "sport = :$PORT" 2>/dev/null)" ]]; then
    echo "[历史记录] 端口 $PORT 已被其他程序占用"
    exit 1
else
    if [[ ! -x "$WEB_DIR/node_modules/.bin/vue-tsc" ]]; then
        echo "[历史记录] 首次运行，正在安装前端依赖…"
        (cd "$WEB_DIR" && npm ci)
    fi
    echo "[历史记录] 正在构建查看页面…"
    (cd "$WEB_DIR" && npm run build)

    nohup "$FASTAPI_PY" tools/picks_server.py --port "$PORT" \
        >>"$LOG_FILE" 2>&1 &
    pid=$!
    echo "$pid" >"$PID_FILE"

    ready=false
    for _ in {1..20}; do
        if curl -sf --max-time 1 "http://127.0.0.1:$PORT/api/picks?limit=1" \
            >/dev/null; then
            ready=true
            break
        fi
        sleep 0.25
    done
    if [[ "$ready" != true ]]; then
        echo "[历史记录] 启动失败，请查看 $LOG_FILE"
        exit 1
    fi
    echo "[历史记录] 已启动（pid $pid，日志 $LOG_FILE）"
fi

IP=$(ip route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
echo "浏览器打开: http://${IP:-127.0.0.1}:$PORT/"
