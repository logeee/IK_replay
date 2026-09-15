#!/usr/bin/env bash
# 安装并立即启动 IK_replay 拨闸服务组。
# 用法：sudo ./deploy/install-autostart.sh

set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "请使用 sudo 运行：sudo ./deploy/install-autostart.sh" >&2
    exit 1
fi

PROJECT_DIR=/home/robot/yx/project/IK_replay
SYSTEMD_DIR=/etc/systemd/system

if [[ ! -f "$PROJECT_DIR/deploy/ik-capability.service" ||
      ! -f "$PROJECT_DIR/deploy/ik-replay.service" ]]; then
    echo "找不到 systemd unit，请确认项目位于 $PROJECT_DIR" >&2
    exit 1
fi

install -m 0644 "$PROJECT_DIR/deploy/ik-capability.service" \
    "$SYSTEMD_DIR/ik-capability.service"
install -m 0644 "$PROJECT_DIR/deploy/ik-replay.service" \
    "$SYSTEMD_DIR/ik-replay.service"

systemctl daemon-reload
systemctl enable ik-capability.service ik-replay.service
systemctl restart ik-capability.service
systemctl restart ik-replay.service

echo
echo "== systemd 状态 =="
systemctl is-enabled ik-capability.service ik-replay.service
systemctl is-active ik-capability.service ik-replay.service

echo
echo "== 17001 探活 =="
curl --fail --silent --show-error --max-time 5 \
    http://127.0.0.1:17001/api/info >/dev/null
echo "17001 已启动并可访问；开机自启已启用。"
