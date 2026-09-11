#!/usr/bin/env bash
# 18006 点云点选页面：orbbec_rgbd_collector 的界面 + IK_replay 的柜面坐标系（默认按 18000 配置）。
# 直接读 7003「RGB-D 标定」落盘的 data/calibration_datasets/，不用 rsync 到 Mac。
# 代码副本在 ../orbbec_rgbd_collector（rsync 自 macos-yx，不含相机 SDK 依赖）。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
COLLECTOR=${COLLECTOR:-$ROOT/../orbbec_rgbd_collector}
PYTHON=${PYTHON:-/home/robot/miniconda3/envs/fastapi/bin/python}
if [[ ! -d "$COLLECTOR/src/rgbd_collector" ]]; then
    echo "缺少 $COLLECTOR/src/rgbd_collector；同步：" >&2
    echo "  rsync -az --exclude datasets --exclude Log --exclude .venv --exclude '*.egg-info' \\" >&2
    echo "      --exclude __pycache__ --exclude .git macos-yx:/Users/timo/code/Python/project/orbbec_rgbd_collector/ $COLLECTOR/" >&2
    exit 1
fi
PORT=${PORT:-18006}
# 用法：run_pointcloud_annotator.sh            前台启动
#       run_pointcloud_annotator.sh stop       停掉占用 18006 的实例
#       run_pointcloud_annotator.sh restart    停掉旧实例再前台启动
listening_pid() { ss -ltnp 2>/dev/null | sed -n "s/.*:$PORT .*pid=\([0-9]*\).*/\1/p" | head -1; }
case "${1:-}" in
    stop|restart)
        pid=$(listening_pid)
        if [[ -n "$pid" ]]; then
            kill "$pid" && echo "[18006] 已停止 pid $pid"
            for _ in 1 2 3 4 5 6 7 8 9 10; do [[ -z "$(listening_pid)" ]] && break; sleep 0.5; done
        else
            echo "[18006] 没有实例在监听 $PORT"
        fi
        [[ "$1" == "stop" ]] && exit 0
        shift ;;
esac
pid=$(listening_pid)
if [[ -n "$pid" ]]; then
    echo "[18006] 端口 $PORT 已被 pid $pid 占用；用 $0 restart 替换它" >&2
    exit 1
fi
exec "$PYTHON" "$ROOT/tools/pointcloud_annotator.py" --collector "$COLLECTOR" --port "$PORT" "$@"
