"""「远方/就地」开关的物理状态词表（Xuanniu_hhy.pt 模型的旋钮类）。

模型只认开关旋钮的物理指向、不读印刷文字，因此实验室柜/工厂柜识别结果
一致，视觉层不再需要区分现场。Xuanniu_hhy.pt 类别：
    0 = 面板     开关所在的面板（不参与状态判定；柜面坐标系方法二用它的 mask）
    1 = 旋钮左   开关拨向左（等同旧模型 Xuanniu_D.pt 的「远方就地左」）
    2 = 旋钮右   开关拨向右（等同旧模型的「远方就地右」）

语义与物理的对应按工厂柜印刷全局固定：就地=左、远方=右。任务 kind
（close_to_remote / remote_to_close）由此唯一决定物理方向，site 不再
参与判断——实验室柜印刷相反也按工厂语义执行与核验。

所有按类别筛框的地方（7004 /scene、17001 预检、7005 面板拟合选框）都
只看 SCENE_CLASSES，其他类别（如「面板」）一律忽略。
"""

from __future__ import annotations

SCENE_LEFT = "旋钮左"
SCENE_RIGHT = "旋钮右"
SCENE_CLASSES = (SCENE_LEFT, SCENE_RIGHT)
# 人读文案用：错误信息里的「左/右」比完整类别名简洁
SCENE_SHORT = {SCENE_LEFT: "左", SCENE_RIGHT: "右"}
# 面板类：不参与开关状态判定，只供柜面坐标系方法二（api/cabinet_frame/
# method2_panel_edges.py）取面板 mask 点云
PANEL_CLASS = "面板"
# 旧模型（Xuanniu_D.pt）的类别名，只用于读历史记录 / 兼容显示
LEGACY_SCENE_NAMES = {"远方就地左": SCENE_LEFT, "远方就地右": SCENE_RIGHT}


def opposite_scene(scene: str | None) -> str | None:
    """左↔右；不是这两类返回 None。"""
    if scene == SCENE_LEFT:
        return SCENE_RIGHT
    if scene == SCENE_RIGHT:
        return SCENE_LEFT
    return None
