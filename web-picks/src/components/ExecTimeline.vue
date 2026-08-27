<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import * as echarts from "echarts";
import type { TraceSample } from "../lib/api";

const props = defineProps<{ trace: TraceSample[] }>();

const host = ref<HTMLDivElement>();
let chart: echarts.ECharts | null = null;
const onResize = () => chart?.resize();

function build() {
  const trace = props.trace;
  if (!host.value || !trace.length) return;

  const times = trace.map((s) => s.t.toFixed(1));
  // IMU 姿态相对起点的变化量：这才是"执行过程中身体动了多少"
  const first = trace.find((s) => s.imu_rpy_deg)?.imu_rpy_deg;
  const rpyNames = ["横滚", "俯仰", "偏航"];
  const rpyColors = ["#ff5d5d", "#f2b84b", "#5a8bff"];

  // 阶段背景：traj（轨迹回放）/ settle（收尾）等
  const phaseAreas: [{ xAxis: string }, { xAxis: string }][] = [];
  let start = 0;
  for (let i = 1; i <= trace.length; i += 1) {
    if (i === trace.length || trace[i].phase !== trace[start].phase) {
      if (trace[start].phase === "settle") {
        phaseAreas.push([{ xAxis: times[start] }, { xAxis: times[i - 1] }]);
      }
      start = i;
    }
  }

  chart = echarts.init(host.value);
  chart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#8fa3c0" }, top: 0 },
    grid: { left: 46, right: 52, top: 34, bottom: 34 },
    xAxis: {
      type: "category",
      data: times,
      name: "s",
      axisLine: { lineStyle: { color: "#22314e" } },
      axisLabel: { color: "#8fa3c0", fontSize: 11 },
    },
    yAxis: [
      {
        type: "value",
        name: "IMU 变化 °",
        axisLabel: { color: "#8fa3c0", fontSize: 11 },
        splitLine: { lineStyle: { color: "rgba(34, 49, 78, 0.5)" } },
      },
      {
        type: "value",
        name: "跟随误差 °",
        axisLabel: { color: "#8fa3c0", fontSize: 11 },
        splitLine: { show: false },
      },
    ],
    series: [
      ...rpyNames.map((name, axis) => ({
        name: `IMU ${name}`,
        type: "line" as const,
        symbol: "none",
        lineStyle: { width: 2 },
        itemStyle: { color: rpyColors[axis] },
        data: trace.map((s) =>
          s.imu_rpy_deg && first
            ? (s.imu_rpy_deg[axis] - first[axis]).toFixed(3)
            : null,
        ),
        ...(axis === 0 && phaseAreas.length
          ? {
              markArea: {
                silent: true,
                itemStyle: { color: "rgba(86, 217, 197, 0.07)" },
                label: {
                  show: true,
                  color: "#56d9c5",
                  fontSize: 11,
                  formatter: "收尾段",
                },
                data: phaseAreas,
              },
            }
          : {}),
      })),
      {
        name: "手臂最大跟随误差",
        type: "line",
        yAxisIndex: 1,
        symbol: "none",
        lineStyle: { width: 1.5, type: "dashed" },
        itemStyle: { color: "#e662d8" },
        data: trace.map((s) => s.follow_max_deg ?? null),
      },
    ],
  });
}

onMounted(() => {
  build();
  window.addEventListener("resize", onResize);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onResize);
  chart?.dispose();
});
</script>

<template>
  <div ref="host" class="timeline" />
</template>

<style scoped>
.timeline {
  width: 100%;
  height: 280px;
}
</style>
