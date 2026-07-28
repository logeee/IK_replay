"""监听 rt/arm_sdk 15 秒，统计每个发布者(writer GUID/进程)实际发送的消息数。"""
import time
import sys
from collections import Counter

sys.path.insert(0, "/home/robot/yx/project/IK_replay")

from cyclonedds.domain import DomainParticipant
from cyclonedds.topic import Topic
from cyclonedds.sub import DataReader
from cyclonedds.core import Qos, Policy

from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_

DURATION = 15.0

dp = DomainParticipant(0)
topic = Topic(dp, "rt/arm_sdk", LowCmd_)
qos = Qos(Policy.Reliability.BestEffort, Policy.History.KeepLast(64))
reader = DataReader(dp, topic, qos=qos)

counts = Counter()
t0 = time.time()
while time.time() - t0 < DURATION:
    for sample in reader.take(N=64):
        info = sample.sample_info
        counts[info.publication_handle] += 1
    time.sleep(0.02)

print(f"监听时长: {DURATION}s, 总消息数: {sum(counts.values())}")
if not counts:
    print("期间没有任何程序往 rt/arm_sdk 发消息")
for handle, n in counts.most_common():
    try:
        pub = reader.matched_publication_data(handle)
        guid = pub.key if pub else None
    except Exception as e:
        pub, guid = None, f"<查询失败 {e}>"
    print(f"- writer handle={handle} guid={guid}: {n} 条 (~{n/DURATION:.1f} Hz)")
