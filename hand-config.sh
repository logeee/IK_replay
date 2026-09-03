#!/usr/bin/env bash
# 一键启动灵巧手配置页（连接/调姿/命名保存姿态到 data/hand_poses）；默认端口 18003。
#
#   ./hand-config.sh                启动（已运行则只打印地址）
#   ./hand-config.sh stop           停止
#   ./hand-config.sh restart        重启
#
# 启动前拜访 18000（激活组合决定设备与侧），拿不到会拒绝启动；
# 改了 18000 配置后 restart 本服务生效。
# Python 查找顺序：PYTHON / FASTAPI_PY → conda 环境 fastapi → PATH 上的 python3。

set -euo pipefail
cd "$(dirname "$0")"

CMD="${1:-start}"
PORT="${HAND_CONFIG_PORT:-18003}"
PID_FILE="logs/service/hand_config.${PORT}.pid"
LOG_FILE="logs/service/hand_config.${PORT}.log"

case "$CMD" in
    start|stop|restart) ;;
    -h|--help)
        echo "用法: $0 [start|stop|restart]（默认端口 ${PORT}，可设 HAND_CONFIG_PORT）"
        exit 0
        ;;
    *)
        echo "未知参数: $CMD（支持 start|stop|restart）" >&2
        exit 2
        ;;
esac

resolve_python() {
    local py
    for py in "${PYTHON:-}" "${FASTAPI_PY:-}"; do
        [[ -n "$py" && -x "$py" ]] && { printf '%s' "$py"; return 0; }
    done
    if [[ "${CONDA_DEFAULT_ENV:-}" == "fastapi" && -x "${CONDA_PREFIX:-}/bin/python" ]]; then
        printf '%s' "$CONDA_PREFIX/bin/python"
        return 0
    fi
    for py in /home/robot/miniconda3/envs/fastapi/bin/python \
              "${HOME}/miniconda3/envs/fastapi/bin/python"; do
        [[ -x "$py" ]] && { printf '%s' "$py"; return 0; }
    done
    py=$(command -v python3 2>/dev/null || true)
    [[ -n "$py" ]] && { printf '%s' "$py"; return 0; }
    return 1
}

stop_server() {
    local pid=""
    [[ -f "$PID_FILE" ]] && pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -z "$pid" ]]; then
        # pid 文件丢了也要能收编孤儿进程
        pid=$(pgrep -f "tools/hand_config_server\.py .*--port $PORT" | head -1 || true)
    fi
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.25
        done
        kill -9 "$pid" 2>/dev/null || true
        echo "[手配置] 已关闭（pid ${pid}，端口 ${PORT}）"
    else
        echo "[手配置] 没有由本脚本启动的进程"
    fi
    rm -f "$PID_FILE"
}

health_ok() {
    curl -sf --max-time 1 "http://127.0.0.1:${PORT}/api/hand/info" >/dev/null 2>&1
}

if [[ "$CMD" == "stop" ]]; then
    stop_server
    exit 0
fi
if [[ "$CMD" == "restart" ]]; then
    stop_server
fi

if health_ok; then
    echo "[手配置] 已在运行: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}/"
    exit 0
fi

PY=$(resolve_python) || { echo "[手配置] 找不到可用 Python" >&2; exit 1; }
echo "[手配置] Python: $PY"
mkdir -p "$(dirname "$LOG_FILE")"

nohup "$PY" -u tools/hand_config_server.py --port "$PORT" \
    >>"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" > "$PID_FILE"

ready=false
for _ in {1..40}; do
    if ! kill -0 "$pid" 2>/dev/null; then
        break
    fi
    if health_ok; then
        ready=true
        break
    fi
    sleep 0.5
done
if [[ "$ready" != true ]]; then
    # 决不留孤儿：超时视为失败，把刚拉起的进程一并收掉
    kill "$pid" 2>/dev/null || true
    sleep 0.5
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "[手配置] 启动失败，请查看 $LOG_FILE"
    tail -n 20 "$LOG_FILE" 2>/dev/null || true
    exit 1
fi
echo "[手配置] 已启动（pid ${pid}，日志 ${LOG_FILE}）"
echo "[手配置] 页面: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}/"
