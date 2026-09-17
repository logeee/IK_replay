# 17001 统一拨闸 API（作业平台对接版）

- 文档版本：1.1
- 更新日期：2026-09-18
- 服务协议：HTTP + JSON
- 服务端口：`17001`
- 有线网络示例地址：`http://192.168.124.5:17001`

> 本文只描述作业平台需要调用的公开接口。左右手的具体执行程序、机器人控制和视觉服务由 17001 内部调度，调用方无需感知。

## 1. 接入约定

### 1.1 Base URL

同机调用：

```text
http://127.0.0.1:17001
```

同一有线网络中的其他设备：

```text
http://192.168.124.5:17001
```

下文用 `BASE` 表示上述地址：

```bash
BASE=http://192.168.124.5:17001
```

### 1.2 通用规则

- 请求和响应编码均为 UTF-8 JSON。
- 当前接口未设置 Token；调用设备应位于受信任的机器人局域网。
- 左右手任务共用一个全局任务锁，任何时刻只执行一个任务。
- 提交任务是异步操作。`POST /task/flip` 返回成功只代表任务已接收，不代表动作已经成功。
- 提交后应每 1～2 秒轮询 `GET /task/status`，直到 `state` 为 `done`。
- 最终执行结果必须读取 `result.ok`，不能使用状态接口顶层的 `ok` 判断任务是否成功。
- `/task/status` 返回当前或最近一次任务，没有按任务 ID 查询历史任务的接口；调用方必须核对返回的 `task_id`。
- `POST /task/flip` 不具备幂等性。任务结束后重复提交会创建并执行一个新任务。

## 2. 支持的任务

| `hand` | `task` | 动作 | 当前说明 |
|---|---|---|---|
| `left` | `left_to_right` | 左手将旋钮从左拨到右 | 当前正式使用；17001 本地默认选择灵巧手新模式 |
| `left` | `right_to_left` | 左手将旋钮从右拨到左 | 兼容的旧模式；只有 17001 能力配置启用后才能执行 |
| `left` | `carousel` | 左右方向交替轮播 | 仅萧山展会版本；YOLO自动决定首方向；双向各成功一次算一轮 |
| `right` | `counterclockwise` | 右手机构逆时针旋转 | 支持 |
| `right` | `clockwise` | 右手机构顺时针旋转 | 支持 |

作业平台只需发送 `hand`、`task` 和可选的 `retries`。轮播任务另传可选的 `cycles`。现场、轨迹、手势、推力及视觉算法等参数由机器人端配置，不建议平台传入。

左手新流程的固定路点速度可在 17001 页面“修改默认”中分别配置：去起手点、去准备点、重试回准备点、收尾回起手点；范围为 `0.05～0.5 rad/s`。该配置不影响 50Hz 主轨迹、IK 规划轨迹和横拨轨迹。

## 3. 提交任务

### 3.1 接口

```http
POST /task/flip
Content-Type: application/json
```

### 3.2 请求字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `hand` | string | 是 | `left` 或 `right` |
| `task` | string | 是 | 必须是该手支持的任务枚举，见上表 |
| `retries` | integer | 否 | 最大执行次数，包含第一次；默认 `3`，范围 `1`～`20` |
| `cycles` | integer | 轮播时否 | 完整轮播次数；默认 `1`，范围 `1`～`50`。左→右和右→左各成功一次计一轮 |

右手任务只有在下游明确返回 `FAILED` 时才自动重试；`PAUSED`、`CANCELED` 和通信异常不会自动创建下一次任务。

### 3.3 左手：旋钮左到右

```bash
curl --fail-with-body --silent --show-error \
  --max-time 10 \
  -X POST "$BASE/task/flip" \
  -H 'Content-Type: application/json' \
  -d '{
    "hand": "left",
    "task": "left_to_right",
    "retries": 3
  }'
```

当前机器人端默认执行灵巧手新流程。平台不需要选择柜体，也不需要指定 `workflow_mode`。

### 3.4 左手：旋钮右到左（旧模式兼容）

```bash
curl --fail-with-body --silent --show-error \
  --max-time 10 \
  -X POST "$BASE/task/flip" \
  -H 'Content-Type: application/json' \
  -d '{
    "hand": "left",
    "task": "right_to_left",
    "retries": 3
  }'
```

该任务始终进入旧模式，但仍受机器人端能力开关限制。若未启用，任务可能被接收，随后以 `NOT_IMPLEMENTED` 结束。

### 3.5 左手：萧山双向轮播

```bash
curl --fail-with-body --silent --show-error \
  --max-time 10 \
  -X POST "$BASE/task/flip" \
  -H 'Content-Type: application/json' \
  -d '{
    "hand": "left",
    "task": "carousel",
    "cycles": 3,
    "retries": 1
  }'
```

`retries` 是每个方向的最大尝试次数。首方向由 YOLO 根据旋钮当前位置选择；成功后手臂保持高位并移动到对向起手式终点再复核，失败则回本方向起手式终点重试。达到 `cycles` 后，从最后所在的起手式终点倒序收尾并释放手臂。左右方向分别使用 17001 保存的对应默认偏移配置。

### 3.6 右手：逆时针

```bash
curl --fail-with-body --silent --show-error \
  --max-time 10 \
  -X POST "$BASE/task/flip" \
  -H 'Content-Type: application/json' \
  -d '{
    "hand": "right",
    "task": "counterclockwise",
    "retries": 3
  }'
```

### 3.7 右手：顺时针

```bash
curl --fail-with-body --silent --show-error \
  --max-time 10 \
  -X POST "$BASE/task/flip" \
  -H 'Content-Type: application/json' \
  -d '{
    "hand": "right",
    "task": "clockwise",
    "retries": 3
  }'
```

### 3.8 接收成功

HTTP `200`：

```json
{
  "ok": true,
  "task_id": "a1b2c3d4e5"
}
```

调用方应保存 `task_id`，随后轮询状态。

### 3.9 服务忙

已有左手任务、右手任务或站位检查正在执行时返回 HTTP `409`：

```json
{
  "ok": false,
  "error": "已有任务在执行",
  "task_id": "392f382b69",
  "state": "running"
}
```

建议平台等待当前任务完成，不要并发重试提交。

### 3.10 参数错误

字段缺失、枚举错误或 `retries` 超出范围时返回 HTTP `422`：

```json
{
  "ok": false,
  "error": "hand=left 不支持 task='clockwise'",
  "supported_tasks": [
    "left_to_right",
    "right_to_left",
    "carousel"
  ]
}
```

## 4. 查询任务状态

### 4.1 接口

```http
GET /task/status
```

```bash
curl --fail --silent --show-error \
  --max-time 10 \
  "$BASE/task/status" | python3 -m json.tool
```

### 4.2 状态值

| `state` | 含义 | 调用方动作 |
|---|---|---|
| `idle` | 本次服务启动后尚无任务 | 不需要轮询 |
| `starting` | 已接收，正在准备 | 继续轮询 |
| `running` | 正在执行 | 继续轮询 |
| `done` | 已结束，成功或失败均可能 | 停止轮询，检查 `result.ok` |

### 4.3 执行中的典型响应

```json
{
  "ok": true,
  "state": "running",
  "task_id": "a1b2c3d4e5",
  "hand": "left",
  "task": "left_to_right",
  "retries": 3,
  "result": null
}
```

### 4.4 成功结束

```json
{
  "ok": true,
  "state": "done",
  "task_id": "a1b2c3d4e5",
  "hand": "right",
  "task": "counterclockwise",
  "retries": 3,
  "attempt": 1,
  "result": {
    "ok": true,
    "code": 0,
    "code_name": "SUCCESS",
    "message": "手车电机任务完成"
  }
}
```

### 4.5 失败结束

状态接口本身调用成功，因此顶层 `ok` 仍为 `true`；任务是否成功只看 `result.ok`：

```json
{
  "ok": true,
  "state": "done",
  "task_id": "a1b2c3d4e5",
  "hand": "right",
  "task": "clockwise",
  "result": {
    "ok": false,
    "code": -1,
    "code_name": "HANDCART_DISPATCH_ERROR",
    "message": "下游服务连接失败",
    "detail": {}
  }
}
```

状态响应还可能包含 `log`、`service`、`started_at`、`finished_at`、`downstream`、`step_times` 等诊断字段。平台可以记录，但不应依赖这些字段控制主业务状态机。

## 5. 终止任务

### 5.1 接口

```http
POST /task/abort
```

```bash
curl --fail-with-body --silent --show-error \
  --max-time 20 \
  -X POST "$BASE/task/abort"
```

- 左手：停止当前流程并释放机械臂控制权。
- 右手：向右手执行服务发送终止请求。
- 终止接口返回后仍应继续轮询 `/task/status`，直到 `state=done`。
- 正常终止的最终结果通常为 `result.ok=false`、`result.code_name=ABORTED`。

终止请求的 HTTP `200` 只表示终止动作已受理或已经尽力执行，不应直接当作原任务进入终态。

## 6. 服务信息与连通性检查

```http
GET /api/info
```

```bash
curl --fail --silent --show-error \
  --max-time 5 \
  "$BASE/api/info" | python3 -m json.tool
```

HTTP `200` 表示 17001 API 服务可访问。响应中的 `public_tasks` 会列出接口接受的任务枚举。

> `/api/info` 只能证明调度服务在线，不能证明机械臂、视觉系统或右手执行服务当前一定可用；执行侧异常会在任务最终结果中返回。

## 7. 结果码

平台应优先按 `result.ok` 判断成功，并保存 `code_name` 与 `message` 用于诊断。当前常见结果码如下；后续可能增加新值，未知值应按失败处理。

| `code_name` | 含义 |
|---|---|
| `SUCCESS` | 任务成功 |
| `NOT_IMPLEMENTED` | 当前能力配置未开放该任务 |
| `PRECONDITION` | 前置服务、机器人连接或接管条件不满足 |
| `ALIGN_FAILED` | 机器人对齐失败 |
| `MEASURE_FAILED` | 距离测量失败 |
| `YOLO_FAILED` | 场景识别或目标取点失败 |
| `IK_FAILED` | 目标不可达或规划失败 |
| `EXEC_FAILED` | 机器人动作执行失败 |
| `VERIFY_FAILED` | 动作后视觉复核未通过，重试已耗尽 |
| `POSE_UNAVAILABLE` | 没有适用的准备位或轨迹 |
| `HANDCART_FAILED` | 右手执行服务明确报告动作失败 |
| `HANDCART_PAUSED` | 右手任务暂停 |
| `HANDCART_DISPATCH_ERROR` | 右手执行服务通信或调度异常 |
| `DISPATCH_ERROR` | 17001 调度异常，或重启后任务无法安全续接 |
| `ABORTED` | 任务被人工或平台终止 |

## 8. 推荐的平台调用流程

```text
GET /api/info
  └─ 不可访问：报告“17001 离线”，不要提交

POST /task/flip
  ├─ HTTP 200：保存 task_id
  ├─ HTTP 409：报告“机器人忙”，稍后重试
  └─ 其他错误：记录 HTTP 状态码和 error

每 1～2 秒 GET /task/status
  ├─ task_id 不匹配：停止处理并报警，避免读取到其他任务结果
  ├─ starting/running：继续轮询
  └─ done：读取 result.ok、code_name、message
```

建议平台自行设置任务总超时。达到平台超时后，先调用 `/task/abort`，再继续轮询至 `done`，不要只停止轮询而让机器人任务继续运行。

## 9. 完整 Python 示例

```python
import time
import requests

BASE = "http://192.168.124.5:17001"


def run_flip(hand: str, task: str, retries: int = 3) -> dict:
    started = requests.post(
        f"{BASE}/task/flip",
        json={"hand": hand, "task": task, "retries": retries},
        timeout=10,
    )
    started.raise_for_status()
    task_id = started.json()["task_id"]

    while True:
        response = requests.get(f"{BASE}/task/status", timeout=10)
        response.raise_for_status()
        status = response.json()

        if status.get("task_id") != task_id:
            raise RuntimeError(
                f"任务 ID 不匹配：期望 {task_id}，收到 {status.get('task_id')}"
            )

        if status.get("state") == "done":
            result = status.get("result") or {}
            if not result.get("ok"):
                raise RuntimeError(
                    f"任务失败：{result.get('code_name')} - {result.get('message')}"
                )
            return status

        time.sleep(1)


# 左手：旋钮左到右
run_flip("left", "left_to_right")

# 右手：逆时针
# run_flip("right", "counterclockwise")
```

## 10. 重启恢复说明

- 17001 会持久化最近一次任务状态。
- 右手任务在已经取得下游任务 ID 后，如果 17001 重启，会恢复状态轮询并继续保持全局任务互斥。
- 正在执行的左手机械臂任务无法在 17001 重启后安全续接，会以 `DISPATCH_ERROR` 结束。
- 平台在网络恢复后应重新查询 `/task/status`，并核对原 `task_id`，不要直接重复提交。
