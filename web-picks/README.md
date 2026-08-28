# 选点记录可视化（web-picks）

浏览 `data/pick_history` 选点证据包的独立 Web 应用：画廊、单条详情（截图
YOLO 轮廓叠加 + three.js 点云）、统计分析（微调量 / 拟合质量 / 置信度 /
墙面系散点）。只读，不依赖相机、YOLO 或 18001，机器人离线也能用。

每条新记录会把关联的 18001 执行诊断保存为自身目录内的
`executions.jsonl`，因此单独复制一个 `data/pick_history/<记录ID>/`
目录即可完整查看。旧记录仍可按 `capture_id` 关联集中日志
（`logs/reach/reach_*.jsonl`，可用 `--reach-log-dir` 指定其它目录）；
`look-history.sh` 启动时会自动将能够关联的旧日志回填到记录目录。画廊卡片
显示执行结果角标，详情页
展示每次执行的误差分解（IK / 关节跟踪 / 总误差 / 对漂移后目标）、躯干
旋转与目标漂移量，以及执行期间 5Hz 的基坐标系漂移时间线。

## 日常使用（单命令启动）

```bash
# 首次或前端代码变更后需要构建一次
cd web-picks && npm install && npm run build

# 之后每次只需
python tools/picks_server.py --port 7010
```

浏览器打开 `http://<主机>:7010/`。

## 前端开发模式

```bash
python tools/picks_server.py          # 终端 1：数据接口（7010，已开 CORS）
cd web-picks && npm run dev           # 终端 2：Vite 热更新（5173）
```

## 技术栈

Vue 3 + TypeScript + Vite / vue-router（hash 路由）/ three.js（PLY 点云）/
ECharts（统计图）。后端是 `tools/picks_server.py` 里约百行的只读 FastAPI。
