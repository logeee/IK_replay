<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import * as echarts from "echarts";
import {
  adjustmentMagnitude,
  fetchRecords,
  wallAdjustment,
  type PickRecord,
} from "../lib/api";

const loading = ref(true);
const error = ref("");

const kpi = ref({
  total: 0,
  avgAdj: 0,
  avgInlier: 0,
  avgConf: 0,
});

const adjChart = ref<HTMLDivElement>();
const wallAdjChart = ref<HTMLDivElement>();
const fitChart = ref<HTMLDivElement>();
const confChart = ref<HTMLDivElement>();
const wallChart = ref<HTMLDivElement>();
const charts: echarts.ECharts[] = [];

const NAME_COLORS: Record<string, string> = {
  // 新模型（Xuanniu_D.pt）按开关物理指向分类
  远方就地右: "#f2b84b",
  远方就地左: "#5ad46f",
  // 旧模型类别，历史记录仍会出现
  远方: "#f2b84b",
  就地: "#5ad46f",
};

const BASE_OPTS = {
  backgroundColor: "transparent",
  textStyle: { color: "#8fa3c0" },
  tooltip: { trigger: "axis" as const },
  grid: { left: 50, right: 24, top: 44, bottom: 40 },
};

function axisStyle() {
  return {
    axisLine: { lineStyle: { color: "#22314e" } },
    axisLabel: { color: "#8fa3c0", fontSize: 11 },
    splitLine: { lineStyle: { color: "rgba(34, 49, 78, 0.5)" } },
  };
}

function shortTime(saved_at?: string): string {
  if (!saved_at) return "-";
  return saved_at.slice(5, 16).replace("T", " ");
}

function groupByName(records: PickRecord[]): Map<string, PickRecord[]> {
  const groups = new Map<string, PickRecord[]>();
  for (const r of records) {
    const key = r.meta.matched_detection_name ?? "未知";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(r);
  }
  return groups;
}

function bestConf(r: PickRecord): number | null {
  const boxes = r.meta.yolo_boxes;
  if (!boxes?.length) return null;
  return Math.max(...boxes.map((b) => b.conf));
}

function makeChart(el: HTMLDivElement, option: echarts.EChartsOption) {
  const chart = echarts.init(el);
  chart.setOption(option);
  charts.push(chart);
}

function buildCharts(records: PickRecord[]) {
  // 时间正序方便看趋势
  const asc = [...records].reverse();
  const times = asc.map((r) => shortTime(r.meta.saved_at));
  const groups = groupByName(asc);

  // ---- 1. 微调量趋势：算法准不准的核心指标 ----
  makeChart(adjChart.value!, {
    ...BASE_OPTS,
    title: {
      text: "微调量趋势（mm，越小说明算法目标越准）",
      left: 0,
      textStyle: { color: "#e7eef9", fontSize: 14 },
    },
    legend: { textStyle: { color: "#8fa3c0" }, top: 4, right: 0 },
    xAxis: { type: "category", data: times, ...axisStyle() },
    yAxis: { type: "value", name: "mm", ...axisStyle() },
    series: [...groups.entries()].map(([name, rs]) => ({
      name,
      type: "line",
      connectNulls: true,
      symbolSize: 7,
      lineStyle: { width: 2 },
      itemStyle: { color: NAME_COLORS[name] },
      data: asc.map((r) =>
        rs.includes(r) ? adjustmentMagnitude(r.meta)?.toFixed(1) ?? null : null,
      ),
    })),
  });

  // ---- 1b. 墙面系微调分量：分方向看系统性偏差，指导模型偏移修正 ----
  const wallSeries = [
    { key: "x" as const, name: "右", color: "#ff5d5d" },
    { key: "z" as const, name: "上", color: "#5a8bff" },
    { key: "y" as const, name: "入墙", color: "#56d9c5" },
  ];
  makeChart(wallAdjChart.value!, {
    ...BASE_OPTS,
    grid: { ...BASE_OPTS.grid, right: 110 },
    title: {
      text: "墙面系微调分量趋势（mm，均值线偏离 0 说明该方向有系统性偏差）",
      left: 0,
      textStyle: { color: "#e7eef9", fontSize: 14 },
    },
    legend: { textStyle: { color: "#8fa3c0" }, top: 4, right: 0 },
    xAxis: { type: "category", data: times, ...axisStyle() },
    yAxis: { type: "value", name: "mm", ...axisStyle() },
    series: wallSeries.map(({ key, name, color }) => ({
      name,
      type: "line",
      connectNulls: true,
      symbolSize: 6,
      lineStyle: { width: 2 },
      itemStyle: { color },
      data: asc.map((r) => {
        const w = wallAdjustment(r.meta);
        return w ? w[key].toFixed(1) : null;
      }),
      markLine: {
        silent: true,
        symbol: "none",
        lineStyle: { color, type: "dashed", opacity: 0.55 },
        label: {
          color,
          fontSize: 11,
          formatter: (p: any) => `${name}均值 ${Number(p.value).toFixed(1)}`,
        },
        data: [{ type: "average" }],
      },
    })),
  });

  // ---- 2. 拟合质量：内点率 + RMS 双轴 ----
  makeChart(fitChart.value!, {
    ...BASE_OPTS,
    title: {
      text: "面板拟合质量趋势",
      left: 0,
      textStyle: { color: "#e7eef9", fontSize: 14 },
    },
    legend: { textStyle: { color: "#8fa3c0" }, top: 4, right: 70 },
    xAxis: { type: "category", data: times, ...axisStyle() },
    yAxis: [
      { type: "value", name: "内点率 %", min: 0, max: 100, ...axisStyle() },
      { type: "value", name: "RMS mm", ...axisStyle(), splitLine: { show: false } },
    ],
    series: [
      {
        name: "内点率",
        type: "line",
        symbolSize: 6,
        itemStyle: { color: "#56d9c5" },
        data: asc.map((r) => {
          const v = r.meta.auto_target?.panel_fit_quality?.inlier_ratio;
          return v != null ? (v * 100).toFixed(1) : null;
        }),
      },
      {
        name: "RMS",
        type: "line",
        yAxisIndex: 1,
        symbolSize: 6,
        itemStyle: { color: "#e662d8" },
        data: asc.map((r) => {
          const v = r.meta.auto_target?.panel_fit_quality?.rms_m;
          return v != null ? (v * 1000).toFixed(2) : null;
        }),
      },
    ],
  });

  // ---- 3. YOLO 置信度分布（按识别来源分组的直方图）----
  const bins = Array.from({ length: 10 }, (_, i) => 0.5 + i * 0.05);
  const histSeries = [...groups.entries()].map(([name, rs]) => {
    const counts = new Array(bins.length).fill(0);
    for (const r of rs) {
      const c = bestConf(r);
      if (c === null) continue;
      const idx = Math.min(
        bins.length - 1,
        Math.max(0, Math.floor((c - 0.5) / 0.05)),
      );
      counts[idx] += 1;
    }
    return {
      name,
      type: "bar" as const,
      itemStyle: { color: NAME_COLORS[name] },
      data: counts,
    };
  });
  makeChart(confChart.value!, {
    ...BASE_OPTS,
    title: {
      text: "YOLO 置信度分布",
      left: 0,
      textStyle: { color: "#e7eef9", fontSize: 14 },
    },
    legend: { textStyle: { color: "#8fa3c0" }, top: 4, right: 0 },
    xAxis: {
      type: "category",
      data: bins.map((b) => `${b.toFixed(2)}~`),
      ...axisStyle(),
    },
    yAxis: { type: "value", name: "次数", minInterval: 1, ...axisStyle() },
    series: histSeries,
  });

  // ---- 4. 墙面系目标散点：看选点空间一致性 ----
  makeChart(wallChart.value!, {
    ...BASE_OPTS,
    tooltip: {
      trigger: "item",
      formatter: (p: any) =>
        `${p.seriesName}<br/>右 ${(p.value[0] * 1000).toFixed(1)} mm，上 ${(p.value[1] * 1000).toFixed(1)} mm`,
    },
    title: {
      text: "墙面系目标位置散点（相对面板拟合原点）",
      left: 0,
      textStyle: { color: "#e7eef9", fontSize: 14 },
    },
    legend: { textStyle: { color: "#8fa3c0" }, top: 4, right: 0 },
    xAxis: { type: "value", name: "右 (m)", ...axisStyle() },
    yAxis: { type: "value", name: "上 (m)", ...axisStyle() },
    series: [...groups.entries()].map(([name, rs]) => ({
      name,
      type: "scatter",
      symbolSize: 11,
      itemStyle: { color: NAME_COLORS[name], opacity: 0.75 },
      data: rs
        .map((r) => r.meta.auto_target?.target_wall_m)
        .filter((v): v is number[] => Array.isArray(v) && v.length === 3)
        .map((v) => [v[0], v[2]]),
    })),
  });
}

function avg(values: (number | null | undefined)[]): number {
  const nums = values.filter((v): v is number => v != null);
  if (!nums.length) return 0;
  return nums.reduce((a, b) => a + b, 0) / nums.length;
}

const onResize = () => charts.forEach((c) => c.resize());

onMounted(async () => {
  try {
    const records = await fetchRecords();
    kpi.value = {
      total: records.length,
      avgAdj: avg(records.map((r) => adjustmentMagnitude(r.meta))),
      avgInlier:
        avg(
          records.map(
            (r) => r.meta.auto_target?.panel_fit_quality?.inlier_ratio,
          ),
        ) * 100,
      avgConf: avg(records.map((r) => bestConf(r))),
    };
    loading.value = false;
    // 等 DOM 挂上再画图
    requestAnimationFrame(() => buildCharts(records));
    window.addEventListener("resize", onResize);
  } catch (e) {
    error.value = String(e);
    loading.value = false;
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onResize);
  charts.forEach((c) => c.dispose());
});
</script>

<template>
  <div v-if="error" class="error-box">{{ error }}</div>
  <div v-else-if="loading" class="loading">加载中…</div>
  <template v-else>
    <div class="kpis">
      <div class="card kpi">
        <div class="num">{{ kpi.total }}</div>
        <div class="label">选点记录总数</div>
      </div>
      <div class="card kpi">
        <div class="num">{{ kpi.avgAdj.toFixed(1) }}<small> mm</small></div>
        <div class="label">平均微调量</div>
      </div>
      <div class="card kpi">
        <div class="num">{{ kpi.avgInlier.toFixed(1) }}<small> %</small></div>
        <div class="label">平均拟合内点率</div>
      </div>
      <div class="card kpi">
        <div class="num">{{ kpi.avgConf.toFixed(2) }}</div>
        <div class="label">平均 YOLO 置信度</div>
      </div>
    </div>

    <div class="charts">
      <div ref="adjChart" class="card chart wide" />
      <div ref="wallAdjChart" class="card chart wide" />
      <div ref="fitChart" class="card chart wide" />
      <div ref="confChart" class="card chart" />
      <div ref="wallChart" class="card chart" />
    </div>
  </template>
</template>

<style scoped>
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.kpi {
  padding: 20px 22px;
}

.kpi .num {
  font-size: 30px;
  font-weight: 800;
  color: var(--accent);
}

.kpi .num small {
  font-size: 15px;
  color: var(--text-dim);
  font-weight: 600;
}

.kpi .label {
  margin-top: 4px;
  color: var(--text-dim);
  font-size: 13px;
}

.charts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.chart {
  height: 360px;
  padding: 14px;
}

.chart.wide {
  grid-column: 1 / -1;
}

@media (max-width: 1000px) {
  .charts {
    grid-template-columns: 1fr;
  }
}
</style>
