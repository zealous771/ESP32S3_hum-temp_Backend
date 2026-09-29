<script setup lang="ts">
/**
 * 仪表盘主页面（设计文档 §24 的布局 + 数据质量面板）。
 *
 * 布局：
 *
 *   ┌──────────────────────────────────────────────────────┐
 *   │ 标题 · 设备选择 · 时间窗口 · 全局状态 · 手动刷新      │
 *   ├──────────────────────────────────────────────────────┤
 *   │ 温度 │ 湿度 │ RSSI │ 状态      ← 四张卡片            │
 *   ├──────────────────────────────────────────────────────┤
 *   │ 温度曲线                                             │
 *   │ 湿度曲线                                             │
 *   ├──────────────────────────────────────────────────────┤
 *   │ 数据质量（数据流活性 / 断层 / 链路 / 丢数据 / 时间轴） │
 *   └──────────────────────────────────────────────────────┘
 *
 * ★ 三个值得说明的设计决定：
 *
 *   ① **时间窗口以"最新数据的 ts"为锚点，不用浏览器时钟。**
 *      `/api/telemetry` 的 start/end 过滤的是 `ts`，也就是**设备时钟**。
 *      用 `Date.now()`（浏览器时钟）当锚点的话，窗口会带上
 *      "浏览器时钟 − 设备时钟"的偏差。实测这个偏差约 2 秒，
 *      对 15 分钟的窗口可以忽略；但一旦设备 NTP 没同步（clock=boot），
 *      偏差可能是几小时，窗口就完全错了。
 *      锚在最新数据的 ts 上，窗口永远对——而且设备停发时，
 *      图表显示的是"最后 15 分钟的数据"而不是一片空白
 *      （数据陈旧这件事由数据质量面板明确表达，不靠图表变空来表达）。
 *
 *   ② **四个独立的轮询器，各自失败各自报。**
 *      合成一个大请求的话，任何一处失败整个页面都没数据，
 *      而"设备台账查不到"和"实时数据查不到"是完全不同的问题。
 *
 *   ③ **数据质量面板是这一页的主角，不是附录。**
 *      一条温度曲线在设备停发时只是"停止延伸"——和"还没加载完"
 *      看起来差不多。所以"数据还在不在流"必须由面板明确回答。
 */

import { computed, ref, watch } from 'vue'

import { api } from './api/client'
import type {
  DeviceOut,
  HealthResponse,
  LatestTelemetryOut,
  Page,
  TelemetryOut,
} from './api/types'
import DataQualityPanel from './components/DataQualityPanel.vue'
import MetricCard from './components/MetricCard.vue'
import StatusPill from './components/StatusPill.vue'
import TimeSeriesChart, { type ChartPoint } from './components/TimeSeriesChart.vue'
import { usePolling } from './composables/usePolling'
import { EMPTY, formatAgo, formatCount, formatNumber } from './format'

// ── 常量 ────────────────────────────────────────────────────────────

/** 文档 §25 的第一版方案：每 5 秒轮询一次。 */
const POLL_MS = 5000

/** 设备台账变化很慢，30 秒刷一次足够。 */
const DEVICE_POLL_MS = 30_000

/**
 * 固件侧的采样周期（`CONFIG_BOARD_SAMPLE_INTERVAL_MS`）。
 *
 * ★ 用它算 limit 是个**隐含耦合**：固件改了采样周期，这里不会自动跟着变。
 *   后果是窗口内的点数估算不准——改快了会取不满，改慢了会取多。
 *   两者都只是"显示的点数不对"，不会出错，所以先这样；
 *   真要做对，应该由后端提供一个 `bucket` 降采样参数（见 README 的已知缺口）。
 */
const SAMPLE_INTERVAL_MS = 2000

const WINDOWS = [
  { label: '5 分钟', minutes: 5 },
  { label: '15 分钟', minutes: 15 },
  { label: '30 分钟', minutes: 30 },
] as const

/** 30 分钟 = 900 点。再长就会超过后端的 limit 上限（1000），所以不做 1 小时。 */
const MAX_LIMIT = 1000

// ── 状态 ────────────────────────────────────────────────────────────

const selectedDeviceId = ref<string | null>(null)
const windowMinutes = ref<number>(15)

const health = usePolling<HealthResponse>(() => api.health(), POLL_MS)

/**
 * `/api/health` 的响应体。
 *
 * ★ 为什么要单独一个 computed，而不是在模板里写 `health.data.value`：
 *
 *   `health` 是轮询器，它的 `data` 字段装的是响应体，而响应体自己
 *   又有一个叫 `data` 的字段（数据流活性那一段）。于是"最后一条数据
 *   距今多久"要写成 `health.data.value?.data.age_ms` —— 两个 `.data`
 *   连着，非常容易看错、也容易漏掉 `.value`。
 *
 *   抽出这一层之后，模板里写 `healthInfo?.data.age_ms`，
 *   一眼能看出"第一个 data 是响应体的字段，第二个是数据流活性"。
 */
const healthInfo = computed<HealthResponse | null>(() => health.data.value)

const devices = usePolling<Page<DeviceOut>>(() => api.devices(), DEVICE_POLL_MS)

const latest = usePolling<LatestTelemetryOut | null>(
  () => (selectedDeviceId.value ? api.latest(selectedDeviceId.value) : Promise.resolve(null)),
  POLL_MS,
)

const telemetry = usePolling<Page<TelemetryOut>>(
  () => {
    const deviceId = selectedDeviceId.value
    if (!deviceId) {
      return Promise.resolve<Page<TelemetryOut>>({ total: 0, limit: 0, offset: 0, items: [] })
    }
    // ← 设计决定 ①：锚在最新数据的 ts 上
    const anchor =
      latest.data.value?.ts ?? health.data.value?.now_ms ?? Date.now()
    const windowMs = windowMinutes.value * 60_000
    const limit = Math.min(
      Math.ceil(windowMs / SAMPLE_INTERVAL_MS) + 10,
      MAX_LIMIT,
    )
    return api.telemetry({
      device_id: deviceId,
      start: anchor - windowMs,
      order: 'desc',
      limit,
    })
  },
  POLL_MS,
)

// 设备列表到手后自动选第一台。
// 不自动选的话，第一次打开页面会是一屏"暂无数据"，而数据其实一直在。
watch(
  () => devices.data.value,
  (page) => {
    if (!page || page.items.length === 0) return
    const exists = page.items.some((d) => d.device_id === selectedDeviceId.value)
    if (!exists) {
      selectedDeviceId.value = page.items[0]?.device_id ?? null
    }
  },
  { immediate: true },
)

// ── 派生数据 ────────────────────────────────────────────────────────

const deviceList = computed<DeviceOut[]>(() => devices.data.value?.items ?? [])

const currentDevice = computed<DeviceOut | null>(
  () => deviceList.value.find((d) => d.device_id === selectedDeviceId.value) ?? null,
)

/**
 * 曲线用的点。
 *
 * ★ 请求用的是 `order=desc`（拿**最新**的 N 条），画图要升序，
 *   所以这里反转一次。
 *   为什么不用 `order=asc` 直接拿：asc + limit 拿到的是**最早**的 N 条，
 *   设备跑了一整天之后，那 N 条可能是几天前的——曲线看着有数据，
 *   其实是陈年旧账。
 */
const ascendingRows = computed<TelemetryOut[]>(() =>
  [...(telemetry.data.value?.items ?? [])].reverse(),
)

/** 最新一条遥测（desc 的第一条）。用于卡片和数据质量面板。 */
const newestRow = computed<TelemetryOut | null>(
  () => telemetry.data.value?.items[0] ?? null,
)

function toPoints(pick: (row: TelemetryOut) => number | null): ChartPoint[] {
  return ascendingRows.value.map((row) => ({ ts: row.ts, value: pick(row) }))
}

const temperaturePoints = computed(() => toPoints((r) => r.temperature))
const humidityPoints = computed(() => toPoints((r) => r.humidity))

const cardsLoading = computed(() => latest.refreshing.value || telemetry.refreshing.value)

// ── 卡片的值与语义色 ────────────────────────────────────────────────

const temperatureValue = computed(() => latest.data.value?.temperature ?? null)
const humidityValue = computed(() => latest.data.value?.humidity ?? null)
const rssiValue = computed(() => latest.data.value?.rssi ?? null)

/**
 * 温度语义色。
 *
 * 阈值参考设计文档 §27 的规则检测（`<0 或 >50` 算异常），
 * 但**显示用的阈值更宽松**：§27 那条是"告警"，而这里是"标个颜色"。
 * 用同一套阈值的话，正常波动（室温 34℃）会被涂成告警色，
 * 看久了就对这个颜色脱敏了。
 */
const temperatureTone = computed<'normal' | 'warn' | 'bad'>(() => {
  const t = temperatureValue.value
  if (t === null) return 'normal'
  if (t >= 45 || t <= 0) return 'bad'
  if (t >= 35 || t <= 5) return 'warn'
  return 'normal'
})

const humidityTone = computed<'normal' | 'warn' | 'bad'>(() => {
  const h = humidityValue.value
  if (h === null) return 'normal'
  if (h >= 90 || h <= 10) return 'bad'
  if (h >= 80 || h <= 20) return 'warn'
  return 'normal'
})

const rssiTone = computed<'normal' | 'warn' | 'bad'>(() => {
  const r = rssiValue.value
  if (r === null) return 'normal'
  if (r <= -90) return 'bad'
  if (r <= -80) return 'warn'
  return 'normal'
})

const deviceOnline = computed(() => currentDevice.value?.online ?? false)

/** 卡片副标题：这条读数是**什么时候采的**，而不是"什么时候拿到的"。 */
const sampleCaption = computed(() => {
  const ts = latest.data.value?.ts
  if (!ts) return EMPTY
  return `采样 ${formatAgo(ts)}`
})

// ── 全局状态 ────────────────────────────────────────────────────────

/**
 * 后端是否可达。
 *
 * ★ 阈值取 3 次而不是 1 次：偶发的一次网络抖动不该弹大红条。
 *   一次失败就报警会让人对红色脱敏，真正挂掉的时候反而不当回事。
 */
const backendDown = computed(() => health.failures.value >= 3)

const firstError = computed(
  () =>
    health.error.value ??
    latest.error.value ??
    telemetry.error.value ??
    devices.error.value ??
    null,
)

const lastUpdatedText = computed(() => {
  const ts = health.lastUpdatedAt.value
  if (!ts) return EMPTY
  return formatAgo(ts)
})

const refreshAll = (): void => {
  void health.refresh()
  void devices.refresh()
  void latest.refresh()
  void telemetry.refresh()
}
</script>

<template>
  <div class="page">
    <!-- ── 顶栏 ──────────────────────────────────────────────────── -->
    <header class="topbar">
      <div class="topbar__brand">
        <h1 class="topbar__title">智能温湿度监控系统</h1>
        <p class="topbar__sub">
          ESP32-S3 · MQTT · FastAPI · SQLite
          <span class="topbar__dot">·</span>
          <span>每 {{ POLL_MS / 1000 }} 秒轮询</span>
        </p>
      </div>

      <div class="topbar__controls">
        <label class="field">
          <span class="field__label">设备</span>
          <select v-model="selectedDeviceId" class="field__input">
            <option v-if="deviceList.length === 0" :value="null">（暂无设备）</option>
            <option v-for="d in deviceList" :key="d.device_id" :value="d.device_id">
              {{ d.device_id }}{{ d.online ? '' : '（离线）' }}
            </option>
          </select>
        </label>

        <label class="field">
          <span class="field__label">窗口</span>
          <select v-model.number="windowMinutes" class="field__input">
            <option v-for="w in WINDOWS" :key="w.minutes" :value="w.minutes">
              {{ w.label }}
            </option>
          </select>
        </label>

        <button class="btn" type="button" :disabled="health.refreshing.value" @click="refreshAll">
          {{ health.refreshing.value ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </header>

    <!-- ── 后端不可达时的横幅 ────────────────────────────────────── -->
    <!--
      只在连续失败 3 次后才出现。而且它和"数据陈旧"是**两个不同的提示**：
      这个说"连不上后端"，数据质量面板里的 stale 说"后端在跑但没数据"。
      混在一起会把人引向错误的排查方向。
    -->
    <div v-if="backendDown" class="banner banner--bad">
      <strong>无法连接后端</strong>
      <span class="banner__detail">{{ firstError }}</span>
      <span class="banner__hint">
        页面上的数据是<b>最后一次成功请求时</b>的，不是当前的。
        检查后端容器：<code>docker compose ps</code>
      </span>
    </div>
    <div v-else-if="firstError" class="banner banner--warn">
      <strong>部分请求失败</strong>
      <span class="banner__detail">{{ firstError }}</span>
      <span class="banner__hint">已保留上一次成功的数据，下一轮会重试。</span>
    </div>

    <!-- ── 状态条 ────────────────────────────────────────────────── -->
    <div class="statusbar">
      <StatusPill
        :tone="backendDown ? 'bad' : 'ok'"
        :label="backendDown ? '后端不可达' : '后端在线'"
        :hint="`最后成功刷新：${lastUpdatedText}`"
      />
      <StatusPill
        :tone="deviceOnline ? 'ok' : 'bad'"
        :label="deviceOnline ? '设备在线' : '设备离线'"
        hint="读时计算：设备最后上报的状态是 online，且 last_seen 距今不超过 30 秒。只看 status 不够——固件卡死时它不会断开 TCP，broker 不会代发 LWT。"
      />
      <StatusPill
        :tone="healthInfo?.data.stale === false ? 'ok' : healthInfo ? 'bad' : 'idle'"
        :label="healthInfo?.data.stale === false ? '数据在流' : '数据已停'"
        hint="后端时钟：最后一条数据距今多久。这与上面的「设备在线」是两件事。"
      />
      <span class="statusbar__meta">
        库中 {{ formatCount(healthInfo?.db.telemetry_rows) }} 行
        <span class="topbar__dot">·</span>
        {{ formatCount(devices.data.value?.total) }} 台设备
      </span>
    </div>

    <!-- ── 四张卡片 ──────────────────────────────────────────────── -->
    <section class="cards">
      <MetricCard
        label="温度"
        :value="formatNumber(temperatureValue, 1)"
        unit="℃"
        :tone="temperatureTone"
        :caption="sampleCaption"
        :missing="temperatureValue === null"
      />
      <MetricCard
        label="湿度"
        :value="formatNumber(humidityValue, 1)"
        unit="%RH"
        :tone="humidityTone"
        :caption="sampleCaption"
        :missing="humidityValue === null"
      />
      <MetricCard
        label="信号强度 RSSI"
        :value="rssiValue === null ? EMPTY : formatNumber(rssiValue, 0)"
        unit="dBm"
        :tone="rssiTone"
        :caption="rssiValue === null ? '设备未联网时为 null（不是 0）' : '0 dBm 是合法且极强的读数'"
        :missing="rssiValue === null"
      />
      <MetricCard
        label="状态"
        :value="deviceOnline ? 'ONLINE' : 'OFFLINE'"
        :tone="deviceOnline ? 'normal' : 'bad'"
        :caption="currentDevice?.last_seen ? `最后报文 ${formatAgo(currentDevice.last_seen)}` : EMPTY"
      />
    </section>

    <!-- ── 曲线 ──────────────────────────────────────────────────── -->
    <section class="charts">
      <TimeSeriesChart
        title="温度曲线"
        unit="℃"
        :points="temperaturePoints"
        color="#d9480f"
        :loading="cardsLoading"
        :min-span="4"
      />
      <TimeSeriesChart
        title="湿度曲线"
        unit="%RH"
        :points="humidityPoints"
        color="#1c7ed6"
        :loading="cardsLoading"
        :min-span="10"
      />
    </section>

    <!-- ── 数据质量 ──────────────────────────────────────────────── -->
    <DataQualityPanel
      :health="healthInfo"
      :newest="newestRow"
      :reachable="!backendDown"
    />

    <footer class="footer">
      <span>
        数据来源 <code>/api/health</code> · <code>/api/devices</code> ·
        <code>/api/devices/{id}/latest</code> · <code>/api/telemetry</code>
      </span>
      <span>
        接口文档：<a href="/docs" target="_blank" rel="noopener">/docs</a>
      </span>
    </footer>
  </div>
</template>

<style scoped>
.page {
  max-width: 1180px;
  margin: 0 auto;
  padding: 20px 20px 40px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ── 顶栏 ─────────────────────────────────────────────────────── */

.topbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
}

.topbar__title {
  margin: 0;
  font-size: 22px;
  font-weight: 680;
  color: var(--c-text-strong);
  letter-spacing: -0.2px;
}

.topbar__sub {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--c-muted);
}

.topbar__dot {
  margin: 0 6px;
  color: var(--c-border-strong);
}

.topbar__controls {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field__label {
  font-size: 11.5px;
  color: var(--c-muted);
  font-weight: 500;
}

.field__input {
  appearance: none;
  border: 1px solid var(--c-border-strong);
  border-radius: 8px;
  background: var(--c-surface);
  color: var(--c-text);
  font-size: 13px;
  padding: 6px 10px;
  min-width: 130px;
  cursor: pointer;
}

.field__input:focus {
  outline: 2px solid var(--c-accent);
  outline-offset: 1px;
}

.btn {
  border: 1px solid var(--c-border-strong);
  border-radius: 8px;
  background: var(--c-surface);
  color: var(--c-text);
  font-size: 13px;
  font-weight: 550;
  padding: 6px 16px;
  cursor: pointer;
  transition: background 0.12s ease;
}

.btn:hover:not(:disabled) {
  background: var(--c-idle-bg);
}

.btn:disabled {
  color: var(--c-muted);
  cursor: default;
}

/* ── 横幅 ─────────────────────────────────────────────────────── */

.banner {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 10px;
  border-radius: 10px;
  padding: 11px 14px;
  font-size: 13px;
  line-height: 1.6;
}

.banner--bad {
  background: var(--c-bad-bg);
  border: 1px solid var(--c-bad-border);
  color: var(--c-bad);
}

.banner--warn {
  background: var(--c-warn-bg);
  border: 1px solid var(--c-warn-border);
  color: var(--c-warn);
}

.banner__detail {
  font-family: var(--font-mono);
  font-size: 12px;
  opacity: 0.95;
  word-break: break-all;
}

.banner__hint {
  color: var(--c-muted);
  font-size: 12px;
  flex: 1 1 100%;
}

/* ── 状态条 ───────────────────────────────────────────────────── */

.statusbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.statusbar__meta {
  margin-left: auto;
  font-size: 12.5px;
  color: var(--c-muted);
  font-variant-numeric: tabular-nums;
}

/* ── 卡片与曲线 ───────────────────────────────────────────────── */

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 14px;
}

.charts {
  display: grid;
  gap: 14px;
}

/* ── 页脚 ─────────────────────────────────────────────────────── */

.footer {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--c-muted);
  border-top: 1px solid var(--c-border);
  padding-top: 14px;
}

.footer code {
  background: var(--c-idle-bg);
  border: 1px solid var(--c-border);
  border-radius: 4px;
  padding: 0 4px;
}

.footer a {
  color: var(--c-accent);
}
</style>
