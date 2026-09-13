# 旧式合并标定隔离区

这里保存从 18000 运行注册表退出的旧式 `handeye3d_result.json`，仅供追溯，运行时不会读取。

2026-09-13 隔离了两份右臂标定：

- `right_arm__yinshi-1-right`：相机序列号 `CP0BB53000FS`
- `right_arm__qiangnao-1-right`：相机序列号 `CP0T263000BE`

两份文件均没有 `unit_code`，且相机序列号与当前 `H2-1336` 的头部、腰部相机不匹配。原文件内容保持不变；如需恢复，必须先确认实体机器人和相机身份，再显式迁回 `config/hand_eye/` 并重新登记。
