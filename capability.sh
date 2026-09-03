#!/usr/bin/env bash
# 一键启动能力配置中心（四级能力注册表 + 手眼标定归档）；默认端口 18000。
#
#   ./capability.sh                    启动（已运行则只打印地址）
#   ./capability.sh --port 18010      在指定端口启动
#   ./capability.sh stop               停止默认端口上的实例
#   ./capability.sh restart            重启
#
# 配置保存在 config/capability_registry.json；修改后需重启 17001/18001 生效。
# Python 查找顺序：PYTHON / FASTAPI_PY → conda 环境 fastapi → PATH 上的 python3。

set -euo pipefail
cd "$(dirname "$0")"

CMD=start
PORT="${CAPABILITY_PORT:-18000}"

usage() {
    echo "用法: $0 [start|stop|restart] [--port PORT]"
    echo "      默认端口 ${CAPABILITY_PORT:-18000}（也可设 CAPABILITY_PORT）"
    echo "      Python 可用 PYTHON=... 覆盖，默认找 conda 环境 fastapi"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        start|stop|restart)
            CMD=$1
            shift
            ;;
        --port|-p)
            if [[ -z "${2:-}" || ! "$2" =~ ^[0-9]+$ ]]; then
                echo "错误: --port 需要一个数字端口" >&2
                exit 2
            fi
            PORT=$2
            shift 2
            ;;
        --port=*)
            PORT="${1#*=}"
            if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
                echo "错误: --port 需要一个数字端口" >&2
                exit 2
            fi
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if (( PORT < 1 || PORT > 65535 )); then
    echo "错误: 端口必须在 1–65535 之间" >&2
    exit 2
fi

conda_bin() {
    local c
    for c in \
        "${CONDA_EXE:-}" \
        /opt/anaconda3/bin/conda \
        /opt/miniconda3/bin/conda \
        /home/robot/miniconda3/bin/conda \
        "${HOME}/miniconda3/bin/conda" \
        "${HOME}/anaconda3/bin/conda" \
        "${HOME}/mambaforge/bin/conda" \
        "${HOME}/miniforge3/bin/conda"
    do
        if [[ -n "$c" && -x "$c" ]]; then
            printf '%s' "$c"
            return 0
        fi
    done
    c=$(command -v conda 2>/dev/null || true)
    if [[ -n "$c" && -x "$c" ]]; then
        printf '%s' "$c"
        return 0
    fi
    return 1
}

conda_roots() {
    local bin base py
    if [[ -n "${CONDA_PREFIX:-}" ]]; then
        printf '%s\n' "$CONDA_PREFIX"
        printf '%s\n' "$(dirname "$CONDA_PREFIX")"
    fi
    if bin=$(conda_bin); then
        base=$("$bin" info --base 2>/dev/null) || true
        [[ -n "$base" ]] && printf '%s\n' "$base"
    fi
    py=$(command -v python3 2>/dev/null || true)
    if [[ -n "$py" ]]; then
        base=$(cd "$(dirname "$py")/.." && pwd)
        printf '%s\n' "$base"
    fi
    printf '%s\n' \
        /home/robot/miniconda3 \
        /opt/anaconda3 \
        /opt/miniconda3 \
        "${HOME}/miniconda3" \
        "${HOME}/anaconda3" \
        "${HOME}/mambaforge" \
        "${HOME}/miniforge3"
}

resolve_python() {
    local py root prefix seen=""

    for py in "${PYTHON:-}" "${FASTAPI_PY:-}"; do
        if [[ -n "$py" && -x "$py" ]]; then
            printf '%s' "$py"
            return 0
        fi
    done

    if [[ "${CONDA_DEFAULT_ENV:-}" == "fastapi" && -x "${CONDA_PREFIX:-}/bin/python" ]]; then
        printf '%s' "$CONDA_PREFIX/bin/python"
        return 0
    fi

    while IFS= read -r root; do
        [[ -z "$root" || ! -d "$root" ]] && continue
        case ":$seen:" in
            *":$root:"*) continue ;;
        esac
        seen="$seen:$root"
        prefix="$root/envs/fastapi"
        for py in "$prefix/bin/python" "$prefix/bin/python3"; do
            if [[ -x "$py" ]]; then
                printf '%s' "$py"
                return 0
            fi
        done
    done < <(conda_roots)

    py=$(command -v python3 2>/dev/null || true)
    if [[ -n "$py" ]]; then
        printf '%s' "$py"
        return 0
    fi
    return 1
}

port_in_use() {
    local port=$1
    if command -v ss >/dev/null 2>&1; then
        [[ -n "$(ss -ltnH "sport = :$port" 2>/dev/null)" ]]
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    else
        return 1
    fi
}

lan_ip() {
    local ip
    ip=$(ip route get 8.8.8.8 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
    [[ -n "$ip" ]] && { printf '%s' "$ip"; return 0; }
    for ip in \
        "$(ipconfig getifaddr en0 2>/dev/null || true)" \
        "$(ipconfig getifaddr en1 2>/dev/null || true)"
    do
        [[ -n "$ip" ]] && { printf '%s' "$ip"; return 0; }
    done
    printf '127.0.0.1'
}

WEB_DIR=web-capability
LOG_DIR=logs/service
PID_FILE="$LOG_DIR/capability.${PORT}.pid"
LOG_FILE="$LOG_DIR/capability.${PORT}.log"

mkdir -p "$LOG_DIR"

pid_alive() {
    local pid=$1
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

running_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid=$(<"$PID_FILE")
    if pid_alive "$pid"; then
        printf '%s' "$pid"
        return 0
    fi
    rm -f "$PID_FILE"
    return 1
}

stop_server() {
    local pid
    if ! pid=$(running_pid); then
        # pid 文件丢了（如上次启动健康检查超时）也要能收编孤儿进程
        pid=$(pgrep -f "tools/capability_server\.py --port $PORT" | head -1)
        if ! pid_alive "$pid"; then
            echo "[能力配置] 没有找到端口 $PORT 上由本脚本启动的服务"
            return
        fi
        echo "[能力配置] pid 文件缺失，按启动命令行找到孤儿进程 ${pid}"
    fi
    kill "$pid"
    for _ in {1..20}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "[能力配置] 已关闭（pid ${pid}，端口 ${PORT}）"
            return
        fi
        sleep 0.25
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "[能力配置] 超时未退出，已强制关闭（pid ${pid}，端口 ${PORT}）"
}

case "$CMD" in
    stop)
        stop_server
        exit 0
        ;;
    restart)
        stop_server
        ;;
    start)
        ;;
esac

if pid=$(running_pid); then
    echo "[能力配置] 已在运行（pid ${pid}，端口 ${PORT}）"
elif curl -sf --max-time 1 "http://127.0.0.1:$PORT/api/capability/registry" \
    >/dev/null; then
    echo "[能力配置] 端口 $PORT 上已有能力配置服务，直接使用"
elif port_in_use "$PORT"; then
    echo "[能力配置] 端口 $PORT 已被其他程序占用"
    exit 1
else
    if ! FASTAPI_PY=$(resolve_python); then
        echo "[能力配置] 找不到 Python。请创建 conda 环境 fastapi，或设置 PYTHON=/path/to/python" >&2
        exit 1
    fi
    echo "[能力配置] Python: $FASTAPI_PY"

    # resolve_python may select a conda interpreter without activating that
    # environment. Make sibling tools (notably node/npm) available as well.
    FASTAPI_BIN_DIR=$(dirname "$FASTAPI_PY")
    if [[ -x "$FASTAPI_BIN_DIR/node" && -x "$FASTAPI_BIN_DIR/npm" ]]; then
        export PATH="$FASTAPI_BIN_DIR:$PATH"
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "[能力配置] 找不到 npm。请安装 Node.js/npm，或将其加入 PATH" >&2
        exit 1
    fi

    if [[ ! -x "$WEB_DIR/node_modules/.bin/vue-tsc" ]]; then
        echo "[能力配置] 首次运行，正在安装前端依赖…"
        (cd "$WEB_DIR" && npm ci)
    fi
    echo "[能力配置] 正在构建配置页面…"
    (cd "$WEB_DIR" && npm run build)

    # -u：无缓冲输出，否则 print 堆在块缓冲里日志一直是 0 字节
    nohup "$FASTAPI_PY" -u tools/capability_server.py --port "$PORT" \
        >>"$LOG_FILE" 2>&1 &
    pid=$!
    echo "$pid" >"$PID_FILE"

    # 机器忙时 Python 导入可能超过 5s，窗口放宽到约 20s
    ready=false
    for _ in {1..40}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        if curl -sf --max-time 1 \
            "http://127.0.0.1:$PORT/api/capability/registry" >/dev/null; then
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
        echo "[能力配置] 启动失败，请查看 $LOG_FILE"
        tail -n 20 "$LOG_FILE" 2>/dev/null || true
        exit 1
    fi
    echo "[能力配置] 已启动（pid ${pid}，日志 ${LOG_FILE}）"
fi

echo "浏览器打开: http://$(lan_ip):$PORT/"
echo "提示: 修改配置保存后，需重启 17001/18001 才会生效"
