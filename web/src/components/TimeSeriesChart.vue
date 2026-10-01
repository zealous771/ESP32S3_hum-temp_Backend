<script setup lang="ts">
/**
 * 时间序列折线图（温度 / 湿度共用）。
 *
 * ★ 六个和"每 5 秒刷新一次"直接相关的实现细节，每一个都对应一个
 *   具体的坏现象：
 *
 *   ① `animation: false`
 *      默认动画是给"静态数据首次渲染"设计的。每 5 秒重画一次时，
 *      动画会让曲线来回抽动，看起来像数据在抖。
 *
 *   ② **y 轴范围自己算，不用 `scale: true`**
 *
 *      `scale: true` 在"数据完全平坦"时给出的范围过宽。实测踩到：
 *      最近 15 分钟 443 条数据恒为 34.0℃（DHT11 分辨率就是 1℃，
 *      室温确实没变），而 ECharts 画出来的 y 轴是 15~55 ——
 *      曲线被压成贴着中间的一条线，看起来像"数据没加载出来"。
 *
 *      自己算之后还有个附带好处：**可以设一个最小跨度**。
 *      否则数据漂 0.1 度，y 轴刻度就跟着变一次，每 5 秒刷新一次时
 *      刻度标签会不停跳动 —— 很烦，而且会让人误以为量级在变。
 *
 *   ③ `series.sampling: 'lttb'`
 *      Largest-Triangle-Three-Buckets 降采样。30 分钟窗口有 900 个点，
 *      而画布宽度只有几百像素——不降采样的话每帧要画 900 个点，
 *      而屏幕上根本分不出相邻两点。LTTB 保留视觉形状地抽稀。
 *
 *   ④ `connectNulls: false`
 *      rssi 在设备未联网时是 null。默认会把 null 两侧的点**直连**，
 *      于是"信号中断的那一段"看起来像一条正常的直线——
 *      这是最危险的一种图表谎言。
 *
 *   ⑤ `setOption(option)` 用**合并**而不是 `notMerge: true`
 *      notMerge 会把用户拖动的 dataZoom 缩放范围每 5 秒重置一次。
 *      表现是"刚放大想看细节，一秒后自己弹回去了"。
 *
 *   ⑥ `ResizeObserver`
 *      窗口大小变化（或者侧栏展开）时 ECharts 不会自动重绘，
 *      画布会保持旧尺寸。必须显式调 resize()。
 */

import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import echarts, { type EChartsInstance } from '../echarts'
import { formatNumber, formatTime } from '../format'

export interface ChartPoint {
  ts: number
  value: number | null
}

const props = withDefaults(
  defineProps<{
    title: string
    unit: string
    points: ChartPoint[]
    /** 折线颜色，直接给 CSS 色值 */
    color: string
    /** 是否正在请求（用于右上角的小提示，不影响已画出的曲线） */
    loading?: boolean
    /**
     * y 轴的**最小跨度**。数据变化小于它时轴不动，避免刻度每 5 秒跳一次。
     *
     * 取值参考传感器的分辨率与"多大变化算有意义"：
     *   DHT11 的分辨率是 1℃ / 1%RH，所以跨度至少要是分辨率的几倍，
     *   否则一个 1℃ 的跳变就会占满整个纵向空间，看起来像剧烈波动。
     *
     * ★ 温度从 4 调到 2（2026-10-01，配合长时间窗口）：
     *
     *   实测数据：半小时窗口内温度跨度 0.0℃、1 小时 1.0℃、
     *   24 小时 2.0℃、3 天 4.0℃。
     *
     *   最小跨度是 4 时，24 小时窗口的曲线只能占纵向空间的一半
     *   —— 本来就只有 2℃ 的变化被压扁了一半。调到 2 之后，
     *   24 小时窗口的曲线能占满整个高度，变化一眼可见。
     *
     *   不能调到 1：那样**单个 1℃ 的跳变**（正好是 DHT11 的量化台阶）
     *   就会让轴范围整个换掉，刻度跟着跳 —— 看起来像"量级在变"，
     *   而实际只是量化噪声。2℃ = 两个量化台阶，是"轴稳定"和
     *   "变化看得见"之间的平衡点。
     */
    minSpan?: number
  }>(),
  { loading: false, minSpan: 1 },
)

const hostRef = ref<HTMLDivElement | null>(null)
let chart: EChartsInstance | null = null
let resizeObserver: ResizeObserver | null = null

/** 曲线上的点数（含 null）。用于右上角的"当前显示多少点"。 */
const pointCount = ref(0)

/**
 * 算 y 轴范围：以数据为中心，取"数据跨度"和"最小跨度"里更大的那个，
 * 再留一点边距。
 *
 * 为什么以**中心**对齐而不是"下界固定、上界跟着数据涨"：
 *   后者会让曲线随着数据整体上移/下移，看起来像"在爬升"，
 *   而实际上只是坐标轴在动。以中心对齐时，同样的波动看起来就是
 *   同样的幅度，不会产生假的趋势感。
 */
function yExtent(values: number[]): [number, number] {
  const finite = values.filter((v) => Number.isFinite(v))
  if (finite.length === 0) return [0, 1]

  let lo = finite[0] as number
  let hi = lo
  for (const v of finite) {
    if (v < lo) lo = v
    if (v > hi) hi = v
  }

  const span = Math.max(hi - lo, props.minSpan)
  const center = (lo + hi) / 2
  // 留 10% 边距，让曲线不贴着上下边
  const padded = span * 1.1

  // ★ 向外取整到整数。
  //
  //   ECharts 在**显式给 min/max** 时，轴两端的标签就是这两个值本身
  //   （中间的刻度才会被"美化"）。不取整的话，湿度轴会显示成
  //   73.78022580959752 / 60.39141794419676 这种 —— 而 DHT11 的
  //   分辨率只有 1℃，那些小数位全是桶平均算出来的噪声，没有信息量。
  //
  //   代价：实际跨度会比 padded 略大，曲线稍微小一点点。可忽略。
  return [Math.floor(center - padded / 2), Math.ceil(center + padded / 2)]
}

function buildOption(): Record<string, unknown> {
  // ECharts 的时间轴要 [时间戳, 值] 的二元组数组。
  const seriesData = props.points.map((p) => [p.ts, p.value])
  const [yMin, yMax] = yExtent(
    props.points.map((p) => p.value).filter((v): v is number => v !== null),
  )

  return {
    animation: false, // ①

    grid: { left: 8, right: 14, top: 10, bottom: 8, containLabel: true },

    tooltip: {
      trigger: 'axis',
      // 只显示有值的点：鼠标划过时如果一堆 null 项，提示框会被撑得很长
      axisPointer: { type: 'line' },
      formatter: (params: unknown) => {
        const list = Array.isArray(params) ? params : [params]
        const first = list[0] as { value?: [number, number | null] } | undefined
        const pair = first?.value
        if (!pair) return ''
        const [ts, value] = pair
        return `${formatTime(ts)}<br/><b>${formatNumber(value)} ${props.unit}</b>`
      },
    },

    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: '#d8dee9' } },
      axisLabel: {
        color: '#6b7280',
        fontSize: 11,
        hideOverlap: true,
        formatter: (value: number) => formatTime(value),
      },
      splitLine: { show: false },
    },

    yAxis: {
      type: 'value',
      // ② 显式给范围，不依赖 scale: true
      min: yMin,
      max: yMax,
      axisLabel: { color: '#6b7280', fontSize: 11 },
      splitLine: { lineStyle: { color: '#eef1f5' } },
    },

    dataZoom: [
      // ★ 只缩放**时间轴**（xAxisIndex: 0）。
      //   不写这一项的话，滚轮缩放会同时改 y 轴范围——
      //   而 y 轴已经被我们显式钉住了，结果会变成"缩放时纵轴乱跳"。
      //   对时间序列来说，用户想缩放的也永远只是时间。
      { type: 'inside', xAxisIndex: 0, throttle: 50 },
    ],

    series: [
      {
        type: 'line',
        name: props.title,
        showSymbol: false, // 900 个点画 900 个圆点纯属噪音
        sampling: 'lttb', // ③
        connectNulls: false, // ④
        lineStyle: { width: 1.8, color: props.color },
        itemStyle: { color: props.color },
        areaStyle: {
          // 淡填充让曲线在浅色背景上有"体量感"，不至于细得看不见
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: `${props.color}33` },
              { offset: 1, color: `${props.color}00` },
            ],
          },
        },
        data: seriesData,
      },
    ],
  }
}

function render(): void {
  if (!chart) return
  pointCount.value = props.points.length
  // ⑤ 合并更新，保住用户的缩放范围
  chart.setOption(buildOption())
}

onMounted(() => {
  if (!hostRef.value) return
  chart = echarts.init(hostRef.value)
  render()

  resizeObserver = new ResizeObserver(() => {
    chart?.resize() // ⑥
  })
  resizeObserver.observe(hostRef.value)
})

// 数据变化就重画。用 watch 而不是把 setOption 写在父组件的更新里，
// 是为了让"什么时候重画"这件事由图表自己决定——
// 父组件只负责给数据，不用知道 ECharts 的存在。
watch(() => props.points, render, { deep: false })

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  // ★ 必须 dispose：ECharts 实例持有 canvas 和一堆事件监听，
  //   不释放的话热重载时会累积，越改越卡。
  chart?.dispose()
  chart = null
})
</script>

<template>
  <section class="chart">
    <header class="chart__head">
      <h3 class="chart__title">{{ title }}</h3>
      <span class="chart__meta">
        <span v-if="loading" class="chart__loading">刷新中…</span>
        <span class="chart__count">{{ pointCount }} 点</span>
      </span>
    </header>

    <div v-if="points.length === 0" class="chart__empty">
      暂无数据 —— 设备还没有上报，或者所选时间窗口内没有记录
    </div>
    <div v-show="points.length > 0" ref="hostRef" class="chart__canvas" />
  </section>
</template>

<style scoped>
.chart {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 12px;
  padding: 14px 16px 10px;
  min-width: 0;
}

.chart__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}

.chart__title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--c-text-strong);
}

.chart__meta {
  display: flex;
  gap: 10px;
  font-size: 12px;
  color: var(--c-muted);
  font-variant-numeric: tabular-nums;
}

.chart__loading {
  color: var(--c-accent);
}

.chart__canvas {
  width: 100%;
  height: 220px;
}

.chart__empty {
  height: 220px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--c-muted);
  font-size: 13px;
  text-align: center;
  padding: 0 20px;
}
</style>
