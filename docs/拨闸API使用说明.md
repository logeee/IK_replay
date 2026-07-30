# 开关拨动作业 API 使用说明

> 面向作业平台的对接文档。
> 版本：v4（2026-07-30，新增站位检查 POST /check/flip）　维护人：机器人侧

## 1. 这个服务做什么

机器人已停在电柜前的条件下，调用本 API 触发一次**开关拨动作业**：
机器人自动完成开关状态识别、位姿对正、执行拨动、结果复核、收臂归位，
全程无人值守。具体执行算法由机器人侧选择，调用方无需感知。

整个任务通常 **40 秒 ~ 2 分钟**（含失败自动重试）。

**推荐调用顺序**：导航到位 → `POST /check/flip`（站位检查，确认"站到位了、
确实需要拨"）→ 通过且 `need_flip=true` 时 `POST /task/flip`（拨闸）。
站位检查不是必须的，但能在动手臂之前把"站歪了/太远/开关本来就在目标
状态"这类问题拦下来，失败原因也更具体。

## 2. 调用前提（调用方需要保证的）

| 前提 | 说明 |
|------|------|
| 机器人已到位 | 正对目标电柜，柜面大致在正前方 |
| 距离合适 | 机器人距柜面 **≥ 0.44 m**（太近会返回错误码 10；上限建议 ≤ 0.8 m） |
| 机器人状态正常 | 本体处于正常运动模式，手臂空闲无遮挡 |
| 不要并发 | 同一时刻只允许一个任务（重复触发会被拒绝） |

## 3. 接口

服务地址：`http://<机器人IP>:17001`（示例中用 `192.168.61.142`）

### 3.1 站位检查（拨闸前置，推荐）

```
POST /check/flip
Content-Type: application/json

{"language": "Change the switch from close to remote"}
```

请求体字段：

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `language` | ✅ | string | 与 `/task/flip` 相同的固定指令（取值见 3.2）。检查靠它判断"开关是否已在目标状态" |

**同步阻塞接口**：请求会等检查全部做完才返回。典型 **15~60 秒**；
如果需要启动相机（约 40 秒）或原地转身纠正朝向（最长 90 秒），
总时长可达 3~4 分钟——**HTTP 客户端超时请设 ≥ 300 秒**。

> ⚠️ 检查期间机器人**可能原地转身**（第 2 步发现朝向偏了会自动纠正），
> 请保证机器人周围无人无障碍，与拨闸作业同等对待。

依次做四步检查，**任何一步不满足立即返回**，不再做后面的：

| 步骤 | 内容 | 通过条件 | 动机器人？ |
|------|------|----------|------------|
| 1 | 距离粗查 | 距柜面 0.44 ~ 0.60 m | 否 |
| 2 | 朝向检查 | 柜面朝向角收进指定带内；不在带内会**自动原地转身**纠正，转不进去才算不满足 | 可能转身 |
| 3 | 站姿终检 | 左右腿俯仰/偏航、腰偏航共 5 个关节角在允许区间内，且距离 0.44 ~ 0.55 m | 否 |
| 4 | 视觉确认 | 识别到开关，且检测框横向落在画面中间 60%；若识别到开关**已在目标状态**则直接判"无需拨动" | 否 |

（各步阈值为机器人侧调参项，可能随现场标定微调，调用方无需感知具体数值。）

返回（HTTP 200，无论检查通过与否）：

```json
{
  "ok": true,               // 请求本身被正常处理（参数错误/互斥冲突时才不是 200）
  "passed": true,           // 站位检查是否通过
  "need_flip": true,        // true=需要拨闸；false=开关已在目标状态，别再调 /task/flip
  "failed_step": null,      // 不通过时 = 卡在第几步（1~4）；通过为 null
  "message": "站位合格，可以调用 /task/flip（相机保持开启供其复用）",
  "steps": [                // 每一步的实测值，失败时用于定位
    {"step": 1, "name": "距离粗查", "distance_m": 0.503, "range_m": [0.44, 0.6],
     "passed": true, "message": "距柜面 0.503 m"},
    {"step": 2, "name": "朝向（平面指数）", "yaw_deg": -4.51, "range_deg": [-6.0, -3.0],
     "corrected": true, "passed": true, "message": "yaw -4.51°（已转动纠正）"},
    {"step": 3, "name": "电机与距离终检", "items": [
       {"item": "左腿俯仰#0", "q_deg": 1.2, "range_deg": [-6.0, 6.0], "passed": true},
       {"item": "距离", "distance_m": 0.5, "range_m": [0.44, 0.55], "passed": true}],
     "passed": true, "message": "5 电机全部在限内，距离 0.500 m"},
    {"step": 4, "name": "YOLO 状态与居中", "scene": "就地", "conf": 0.86,
     "cx_ratio": 0.469, "passed": true,
     "message": "「就地」框中心在画宽 46.9% 处（要求 20%~80%）"}
  ],
  "camera_kept": true,      // 见下方说明，调用方一般无需关心
  "duration_s": 16.2,
  "log": ["…"]              // 过程日志，仅供人读
}
```

**调用方的判断逻辑（三分支）**：

| passed | need_flip | 含义 | 下一步 |
|--------|-----------|------|--------|
| `true` | `true` | 站位合格，需要拨 | **马上**调 `/task/flip`（相机已就绪，可省约 40 秒启动） |
| `true` | `false` | 开关已在目标状态 | 作业结束，**不要**调 `/task/flip` |
| `false` | — | 站位不合格 | 看 `failed_step` / `message`：第 1/3 步失败通常要**导航重新进位**；第 2 步失败是朝向纠不过来；第 4 步失败按 `message` 提示（偏左/偏右多少）平移站位 |

异常返回：

```json
// language 缺失或不是固定指令 → HTTP 422（同 /task/flip）
// 拨闸任务执行中 → HTTP 409
{"ok": false, "error": "拨闸任务执行中，不能同时做站位检查", "task_id": "…", "state": "running"}
```

关于 `camera_kept`：检查通过且需要拨闸时，机器人侧会保持相机开启，
紧接着的 `/task/flip` 会复用它（更快）；其余情况相机自动释放。
这对调用方透明，不需要做任何处理。

### 3.2 触发任务

```
POST /task/flip
Content-Type: application/json

{"language": "Change the switch from close to remote", "retries": 3}
```

请求体字段：

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `language` | ✅ | string | 作业指令，取值见下表（大小写和空格有容错，其他句子会被拒绝） |
| `retries` | 可选 | int | 最大尝试轮数（含第一次），取值 1~20，**不传默认 3**。首轮约 40~60 秒，之后每多重试一轮约多 10~20 秒。注：若本次作业由 VLA 算法执行，此字段会被忽略 |

`language` 支持的指令：

| language | 作业 | 当前支持情况 |
|----------|------|--------------|
| `Change the switch from close to remote` | 就地 → 远方 | ✅ 已验证 |
| `Change the switch from remote to close` | 远方 → 就地 | ⏳ 暂未支持，任务会立即以错误码 1（NOT_IMPLEMENTED）结束 |

**立即返回**，不等任务完成：

```json
{"ok": true, "task_id": "78dfd3c094"}
```

异常返回：

```json
// language 缺失/不是固定指令，或 retries 非法 → HTTP 422
{"ok": false, "error": "无法识别的指令: 'open the door'",
 "supported": ["Change the switch from close to remote",
               "Change the switch from remote to close"]}

// 已有任务在执行 → HTTP 409（不会打断当前任务）
{"ok": false, "error": "已有任务在执行", "task_id": "…", "state": "running"}
```

### 3.3 查询进度与结果（轮询）

```
GET /task/status
```

建议每 1~2 秒轮询一次，直到 `state` 变为 `done`。

```json
{
  "ok": true,
  "state": "running",              // idle | starting | running | done
  "task_id": "78dfd3c094",
  "language": "Change the switch from close to remote",
  "retries": 3,
  "started_at": "2026-07-28T20:49:23",
  "finished_at": null,             // done 后才有值
  "result": null,                  // done 后才有值，见下
  "log": ["[20:49:29] [flow] ═══ 2️⃣ 场景判断 ═══", "…"]
}
```

`state` 含义：

| state | 含义 |
|-------|------|
| `idle` | 从未执行过任务（服务刚启动） |
| `starting` | 正在启动相机等底层服务（约 5~10 秒） |
| `running` | 流程执行中（`log` 里有实时进度） |
| `done` | 已结束，看 `result` |

任务结束后的 `result`：

```json
{
  "ok": false,
  "code": 10,
  "code_name": "POSE_UNAVAILABLE",
  "message": "距柜面 0.431 m，小于最近的起手式门槛 0.44 m——距离太近，无可用起手式",
  "detail": {"elapsed_s": 4.4}
}
```

判断逻辑：**`result.ok` 为 `true` 即拨闸成功**；为 `false` 时按
`result.code` 分支处理，`result.message` 是给人看的中文原因。

### 3.4 中止任务

```
POST /task/abort
```

急停当前动作并强制结束任务（机械臂就地停住后释放）。没有任务在跑时返回 409。

## 4. 错误码

> ⚠️ **错误码表后续会不断完善**：现阶段取值和粒度是占位性质的，随着现场
> 案例积累会细分、增补（编号会保持向后兼容，已有编号含义不变）。
> 对接时请把「未知 code」也当失败处理，不要枚举穷尽。

| code | code_name | 含义 | 调用方建议动作 |
|------|-----------|------|----------------|
| 0 | OK | 拨闸成功 | 继续后续任务 |
| 1 | NOT_IMPLEMENTED | 该任务暂不支持（如「远方 → 就地」） | 上报人工 |
| 2 | PRECONDITION | 机器人侧服务/硬件前置条件不满足 | 上报机器人侧检查 |
| 3 | ALIGN_FAILED | 对正柜面失败：抬手前对中不收敛，或抬手后朝向漂出保持带（此时不做转身纠正，手臂受控回落） | 重新走 `/check/flip` 站位后再触发 |
| 4 | MEASURE_FAILED | 柜面测量失败（点云拟合不出平面） | 检查是否正对柜面、有无遮挡 |
| 5 | YOLO_FAILED | 视觉识别失败：连问 3 帧都没识别到开关（手臂已受控回落） | 检查光线/遮挡/站位后重试 |
| 6 | IK_FAILED | 机械臂无法到达目标点 | 调整机器人站位后重试 |
| 7 | EXEC_FAILED | 真机执行失败（急停/超时等） | 上报人工 |
| 8 | VERIFY_FAILED | 拨了但复核未通过，重试轮次耗尽 | 上报人工确认开关状态 |
| 9 | ABORTED | 被人工中止（abort 接口或确认台） | 按业务逻辑处理 |
| 10 | POSE_UNAVAILABLE | 距柜面太近，无可用起手姿态 | **后退到 ≥0.44 m 再触发** |
| -1 | DISPATCH_ERROR | 调度层故障（相机服务拉不起来等） | 上报机器人侧 |

## 5. 对接示例

### Python

```python
import requests, time

BASE = "http://192.168.61.142:17001"
CMD = "Change the switch from close to remote"

# 1) 站位检查（同步，超时务必给足 300 秒）
chk = requests.post(f"{BASE}/check/flip", timeout=300,
                    json={"language": CMD}).json()
if not chk["passed"]:
    raise RuntimeError(f"站位不合格（第{chk['failed_step']}步）: {chk['message']}")
if not chk["need_flip"]:
    print("开关已在目标状态，无需拨动")
    raise SystemExit(0)

# 2) 触发拨闸（异步，立即返回 task_id）
r = requests.post(f"{BASE}/task/flip", timeout=5,
                  json={"language": CMD, "retries": 3}   # retries 可省略，默认 3
                  ).json()
if not r["ok"]:
    raise RuntimeError(f"触发失败: {r}")

# 3) 轮询直到结束
while True:
    st = requests.get(f"{BASE}/task/status", timeout=5).json()
    if st["state"] == "done":
        break
    time.sleep(2)

res = st["result"]
if res["ok"]:
    print("拨闸成功", res["detail"])
else:
    print(f"失败 [{res['code']} {res['code_name']}] {res['message']}")
```

### 命令行（调试用）

```bash
# 站位检查（同步，等它返回）
curl -X POST http://192.168.61.142:17001/check/flip --max-time 300 \
     -H 'Content-Type: application/json' \
     -d '{"language": "Change the switch from close to remote"}'

# 触发拨闸 + 轮询
curl -X POST http://192.168.61.142:17001/task/flip \
     -H 'Content-Type: application/json' \
     -d '{"language": "Change the switch from close to remote"}'
watch -n 2 'curl -s http://192.168.61.142:17001/task/status | python3 -m json.tool'
```

## 6. 常见问题

**Q：POST 之后多久有结果？**
典型 40 秒~1 分钟；每多重试一轮约多 10~20 秒（按默认 retries=3 最长
约 2 分钟）。轮询时长明显超出「1 分钟 + retries × 20 秒」仍是
`running` 属异常，可 `POST /task/abort` 后上报。

**Q：可以连续触发多次吗？**
可以，但必须等上一个任务 `done` 之后。任务执行中重复 POST 会收到 409，
不会打断当前任务。站位检查和拨闸任务也互斥：检查进行中触发
`/task/flip` 会收到 409，反之亦然。

**Q：`/check/flip` 必须调吗？**
不必须，直接 `/task/flip` 也能工作（流程内部有自己的对正和识别）。
但推荐调：它能在动手臂之前拦下"站太远/站歪/开关本来就在目标状态"，
失败原因逐项量化（差多少度、偏多少像素），方便导航侧修正。

**Q：站位检查失败后需要清理吗？**
不需要。检查失败时机器人侧自动释放相机等资源，机器人保持可移动状态，
按 `failed_step` 修正站位后可立即再次调用。

**Q：任务失败后机器人是什么状态？**
机械臂会被释放、相机服务会被关闭，机器人回到可移动状态。失败不需要
调用方做任何清理，直接按错误码决定下一步即可。

**Q：`log` 字段能用来做什么？**
流程的实时中文日志（对齐进度、第几轮重试等），可原样展示在你们的
监控界面上，仅供人读，格式不承诺稳定，请勿解析。

**Q：服务没响应怎么办？**
17001 连不上说明调度服务没起或机器人断网，联系机器人侧。
