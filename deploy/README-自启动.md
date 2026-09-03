# 拨闸服务开机自启（systemd）

两个 unit：
- `ik-capability.service`：18000 能力配置中心，常驻 + 崩溃自动拉活（先装这个）
- `ik-replay.service`：拨闸服务组（调度17001 / YOLO7004 / 点云7005 / 确认台7002）

## 能力配置中心 18000（ik-capability.service）

18000 是全组的启动依赖——17001 / 18001 启动第一步都要拜访它，机器重启后
若没人拉起 18000，`prepare*.sh` 会直接报「启动拜访 18000 失败」。注册为
常驻服务后：开机先于拨闸组启动，进程崩溃 3 秒后自动拉活。

```bash
cd /home/robot/yx/project/IK_replay
./capability.sh stop        # 先收掉手动起的实例（没有也不报错），避免端口冲突
sudo cp deploy/ik-capability.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ik-capability.service
systemctl status ik-capability.service --no-pager   # 应为 active (running)
```

日常操作（systemd 接管后起停改用 systemctl，别再用 capability.sh——
它杀掉的进程 3 秒后会被 systemd 拉活）：

```bash
sudo systemctl restart ik-capability.service   # 改完后端代码用这个
sudo systemctl stop ik-capability.service      # 临时停（开机仍会自启）
sudo systemctl disable --now ik-capability.service   # 彻底停用
```

- 业务日志仍写 `logs/service/capability.18000.log`；
  `journalctl -u ik-capability.service` 只有 systemd 侧的启停记录。
- 服务不做前端构建：改了 `web-capability/` 前端后先
  `cd web-capability && npm run build`，再 `sudo systemctl restart ik-capability.service`。

## 拨闸服务组（ik-replay.service）

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
