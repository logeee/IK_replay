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
exec "$PYTHON" "$ROOT/tools/pointcloud_annotator.py" --collector "$COLLECTOR" --port "${PORT:-18006}" "$@"
