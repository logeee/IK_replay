"""只读查询 H2 运控状态，不下发任何运动指令。

用途：确认"运动1/运动2"等遥控器模式对应的 FSM id ——
先跑本脚本记下当前 fsm_id，用遥控器切模式后再跑一次对比。

用法：python3 query_fsm.py [网卡名，默认 enp86s0]
"""
import sys

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.h2.loco.h2_loco_client import LocoClient


def main():
    iface = sys.argv[1] if len(sys.argv) > 1 else "enp86s0"
    ChannelFactoryInitialize(0, iface)

    c = LocoClient()
    c.SetTimeout(5.0)
    c.Init()

    code, fsm_id = c.GetFsmId()
    print(f"当前 fsm_id = {fsm_id}（rpc={code}）")
    code, fsm_mode = c.GetFsmMode()
    print(f"当前 fsm_mode = {fsm_mode}（rpc={code}）")
    code, arm_sdk = c.GetArmSdkStatus()
    print(f"arm_sdk 状态 = {arm_sdk}（rpc={code}）")

    code, ids, names = c.GetAvailableFsmIds()
    print(f"\n可用 FSM 列表（rpc={code}）：")
    if ids:
        for i, n in zip(ids, names or [""] * len(ids)):
            # 动作库条目（HumanMimic/BeyondMimic 的具体动作）太多，只列骨架状态
            if i < 100000 and not (502000 <= i < 504000):
                print(f"  fsm_id={i:<6} {n}")
        extra = sum(1 for i in ids if i >= 100000 or 502000 <= i < 504000)
        print(f"  …另有 {extra} 条动作库条目"
              f"（HumanMimic/BeyondMimic 具体动作）未列出")


if __name__ == "__main__":
    main()
