# 17001 统一 Flip API

外部系统指定使用哪只手、执行什么任务；可选指定最多执行次数。所有任务严格串行；已有任务运行时，新请求返回 HTTP 409。

## 1. 开始任务

`POST http://<机器人IP>:17001/task/flip`

左手灵巧手，旋钮从左到右：

```json
{"hand":"left","task":"left_to_right","retries":3}
```

左手灵巧手，旋钮从右到左：

```json
{"hand":"left","task":"right_to_left","retries":3}
```

右手手车电机，逆时针旋转：

```json
{"hand":"right","task":"counterclockwise","retries":3}
```

右手手车电机，顺时针旋转：

```json
{"hand":"right","task":"clockwise","retries":3}
```

`retries` 可不传，默认值为 `3`，取值范围为 `1`～`20`，表示包含第一次执行在内的最多执行次数。右手仅在 8876 明确返回 `FAILED` 时重试；`PAUSED` 和 `CANCELED` 不重试。

成功接收：

```json
{"ok":true,"task_id":"a1b2c3d4e5"}
```

已有任务时：HTTP 409。

```json
{"ok":false,"error":"已有任务在执行","task_id":"...","state":"running"}
```

## 2. 查询状态

`GET http://<机器人IP>:17001/task/status`

持续轮询，直到 `state` 为 `done`。最终是否成功看 `result.ok`。

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
    "code_name": "SUCCESS",
    "message": "手车电机任务完成"
  }
}
```

状态只有四种：`idle`、`starting`、`running`、`done`。

## 3. 终止任务

`POST http://<机器人IP>:17001/task/abort`

左手任务会停止并释放机械臂；右手任务会调用 8876 的 `/terminate`，之后继续通过状态接口观察，直到 `state=done` 且 `result.code_name=ABORTED`。

## curl 示例

```bash
TASK_JSON=$(curl -sS --max-time 10 \
  -X POST 'http://127.0.0.1:17001/task/flip' \
  -H 'Content-Type: application/json' \
  -d '{"hand":"right","task":"counterclockwise","retries":3}')

TASK_ID=$(echo "$TASK_JSON" | jq -r '.task_id')

while true; do
  STATUS=$(curl -sS --max-time 10 \
    'http://127.0.0.1:17001/task/status')
  echo "$STATUS" | jq
  [ "$(echo "$STATUS" | jq -r '.state')" = "done" ] && break
  sleep 1
done
```

## 当前内部映射

- `left + left_to_right`：左手灵巧手新流程。
- `left + right_to_left`：预留为左手旧流程；当前能力配置未启用该方向时，任务结果为 `NOT_IMPLEMENTED`。
- `right + counterclockwise`：8876 的 `motor_action=left`。
- `right + clockwise`：8876 的 `motor_action=right`。
- 转发给 8876 时，`process_restart` 固定为 `false`。
- 8876 默认地址为 `http://192.168.61.137:8876`，可用 17001 启动参数 `--handcart-base` 覆盖。

右手任务可通过 `POST /task/abort` 终止；17001 会调用 8876 的 `POST /v1/handcart/jobs/{job_id}/terminate`，并继续轮询到终态。

最近任务保存在 `logs/service/dispatch_task_state.json`。17001 重启时，如果右手任务已经取得 8876 `job_id`，会恢复轮询并继续保持全局互斥；尚未取得 `job_id` 的右手任务以及执行中的左手任务无法安全续接，会保留原 `task_id` 并标记为 `DISPATCH_ERROR`。
