"""「远方/就地」开关的物理状态词表（Xuanniu_D.pt 模型）。

新模型只认开关的物理指向、不读印刷文字，因此实验室柜/工厂柜识别结果
一致，视觉层不再需要区分现场：
    0 = 远方就地左   开关拨向左
    1 = 远方就地右   开关拨向右

语义与物理的对应按工厂柜印刷全局固定：就地=左、远方=右。任务 kind
（close_to_remote / remote_to_close）由此唯一决定物理方向，site 不再
参与判断——实验室柜印刷相反也按工厂语义执行与核验。
"""

from __future__ import annotations

SCENE_LEFT = "远方就地左"
SCENE_RIGHT = "远方就地右"
SCENE_CLASSES = (SCENE_LEFT, SCENE_RIGHT)
# 人读文案用：错误信息里的「左/右」比完整类别名简洁
SCENE_SHORT = {SCENE_LEFT: "左", SCENE_RIGHT: "右"}


def opposite_scene(scene: str | None) -> str | None:
    """左↔右；不是这两类返回 None。"""
    if scene == SCENE_LEFT:
        return SCENE_RIGHT
    if scene == SCENE_RIGHT:
        return SCENE_LEFT
    return None
