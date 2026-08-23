# 拨闸服务开机自启（systemd）

把 `prepare.sh` 注册为系统服务：开机自动拉起 调度17001 / YOLO7004 /
点云7005 / 确认台7002（reach_server 18001 仍由调度按需自动开关）。

## 安装（执行一次）

```bash
cd /home/robot/yx/project/IK_replay
sudo cp deploy/ik-replay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ik-replay.service
```

`enable --now` = 注册开机自启 + 立刻启动一次。如果当前服务已经在跑，
`prepare.sh` 会看到端口被占用而逐个跳过，不会起重复进程。

## 验证

```bash
systemctl status ik-replay.service      # 应为 active (exited)
curl -s http://127.0.0.1:17001/api/info | head -c 200; echo
```

或重启机器后直接开 `http://<机器人IP>:17001/` 看页面。

## 日常操作

```bash
sudo systemctl stop ik-replay.service      # 优雅停止全部（含调度拉起的 18001）
sudo systemctl start ik-replay.service     # 手动启动
sudo systemctl restart ik-replay.service   # 重启全部（改完代码后用这个）
sudo systemctl disable ik-replay.service   # 取消开机自启（不停止当前进程）
```

习惯用 `./prepare.sh` / `./prepare.sh stop` 手动操作也完全兼容：
脚本对已监听的端口自动跳过，不会和 systemd 打架。

## 日志

服务输出仍写在原来的位置：`logs/service/*.log`
（dispatch.log / yolo_server.log / pointcloud_viewer.log / console.log）。

`journalctl -u ik-replay.service` 只记录 prepare.sh 本身的启动/自检输出。

## 说明与限制

- 服务以 `robot` 用户运行，工作目录固定在本项目根目录。
- 这是"整组"服务：某个子服务单独崩了 systemd 不会自动拉活它
  （和现在手动跑 `prepare.sh` 的行为一致）。需要时执行
  `sudo systemctl restart ik-replay.service` 即可整组重启；
  以后要做到单服务自愈，再把各条 start_one 拆成独立 unit。
- 卸载：`sudo systemctl disable --now ik-replay.service &&
  sudo rm /etc/systemd/system/ik-replay.service &&
  sudo systemctl daemon-reload`
