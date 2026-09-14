# 17001 统一 Flip API

外部系统只需要指定两项：使用哪只手、执行什么任务。所有任务严格串行；已有任务运行时，新请求返回 HTTP 409。

## 1. 开始任务

`POST http://<机器人IP>:17001/task/flip`

左手灵巧手，旋钮从左到右：

```json
{"hand":"left","task":"left_to_right"}
```

左手灵巧手，旋钮从右到左：

```json
{"hand":"left","task":"right_to_left"}
```

右手手车电机，逆时针旋转：

```json
{"hand":"right","task":"counterclockwise"}
```

右手手车电机，顺时针旋转：

```json
{"hand":"right","task":"clockwise"}
```

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
  "result": {
    "ok": true,
    "code_name": "SUCCESS",
    "message": "手车电机任务完成"
  }
}
```

状态只有四种：`idle`、`starting`、`running`、`done`。

## curl 示例

```bash
TASK_JSON=$(curl -sS --max-time 10 \
  -X POST 'http://127.0.0.1:17001/task/flip' \
  -H 'Content-Type: application/json' \
  -d '{"hand":"right","task":"counterclockwise"}')

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

注意：当前收到的 8876 资料没有停止/取消接口，因此右手任务运行期间，17001 无法真正停止它；`POST /task/abort` 会返回 HTTP 501，而不会假装停止成功。补充 8876 的取消接口后再接入统一急停。

右手任务运行时不要重启 17001；8876 当前资料也没有提供“查询全部运行中任务”的恢复接口。
