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
 *
 *   ④ **画曲线一律走降采样接口，但"报得准"的数据另走一条。**
 *      设备 2 秒一条，而 `/api/telemetry` 的分页上限是 1000 条
 *      —— 只能覆盖 **33 分钟**。所以曲线走 `/api/telemetry/series`
 *      按 `bucket_ms` 聚合；而数据质量面板要的 `received_at` / `seq`
 *      必须来自逐条原始记录（`/api/telemetry?limit=1`）。
 *
 *      两条数据源刻意分开：**降采样接口不该假装自己能回答逐条的问题**。
 *      让它在桶里"随便挑一条"返回 received_at，面板上那个数字就变成
 *      假的了 —— 而它恰恰是用来判断数据真假的。
 */

import { computed, ref, watch } from 'vue'

import { api } from './api/client'
import type {
  DeviceOut,
  HealthResponse,
  LatestTelemetryOut,
  Page,
  TelemetryBucketOut,
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
 * ★ 用它当"最短窗口的桶宽"是个**隐含耦合**：固件改了采样周期，
 *   这里不会自动跟着变。影响只是最短窗口的平滑程度，不会出错。
 */
const SAMPLE_INTERVAL_MS = 2000

interface WindowOption {
  label: string
  minutes: number
  /** 时间桶宽度（毫秒）。等于采样周期时相当于取原始记录。 */
  bucketMs: number
}

/**
 * 可选的时间窗口。
 *
 * ★ 每个档位配一个 `bucketMs`，目标是**点数保持在 300 上下**：
 *   太少看不出形状，太多也画不下（30 分钟 900 点已经是屏幕像素的
 *   两三倍了）。
 *
 * ★ 为什么必须降采样，而不是简单把窗口调大：
 *   `/api/telemetry` 的分页上限是 1000 条，设备 2 秒一条
 *   —— 1000 × 2 秒 = **33 分钟**，这就是原来的天花板。
 *
 *   实测数据：半小时窗口内温度跨度 **0.0℃**（室内环境，DHT11 分辨率
 *   1℃ 确实没变），而 24 小时跨度 2.0℃、3 天 4.0℃。
 *   **要看温度变化就必须拉长时间窗口，而拉长必然超过 1000 条**，
 *   所以走 `/api/telemetry/series` 按桶聚合。
 */
const DEFAULT_WINDOW: WindowOption = {
  label: '24 小时',
  minutes: 1440,
  bucketMs: 300_000,
}

const WINDOWS: WindowOption[] = [
  { label: '15 分钟', minutes: 15, bucketMs: SAMPLE_INTERVAL_MS },
  { label: '1 小时', minutes: 60, bucketMs: 10_000 },
  { label: '6 小时', minutes: 360, bucketMs: 60_000 },
  DEFAULT_WINDOW,
  { label: '3 天', minutes: 4320, bucketMs: 900_000 },
]

// ── 状态 ────────────────────────────────────────────────────────────

const selectedDeviceId = ref<string | null>(null)
const windowMinutes = ref<number>(DEFAULT_WINDOW.minutes)

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

/** 当前选中的窗口档位（含它对应的桶宽）。 */
const currentWindow = computed<WindowOption>(
  () => WINDOWS.find((w) => w.minutes === windowMinutes.value) ?? DEFAULT_WINDOW,
)

/**
 * 曲线数据 —— 走**降采样**接口。
 *
 * 锚点仍是"最新数据的 ts"（设计决定 ①）：`start` 过滤的是设备时钟 `ts`。
 */
const telemetry = usePolling<Page<TelemetryBucketOut>>(
  () => {
    const deviceId = selectedDeviceId.value
    if (!deviceId) {
      return Promise.resolve<Page<TelemetryBucketOut>>({
        total: 0,
        limit: 0,
        offset: 0,
        items: [],
      })
    }
    // ← 设计决定 ①：锚在最新数据的 ts 上
    const anchor = latest.data.value?.ts ?? health.data.value?.now_ms ?? Date.now()
    const win = currentWindow.value
    return api.telemetrySeries({
      device_id: deviceId,
      start: anchor - win.minutes * 60_000,
      bucket_ms: win.bucketMs,
    })
  },
  POLL_MS,
)

/**
 * 最新一条**原始**记录 —— 只给数据质量面板用。
 *
 * ★ 为什么不复用上面那份曲线数据：
 *   降采样之后 `ts` 是**桶起点**，而且聚合结果里**没有 `received_at`
 *   和 `seq`**（一个桶里有很多条，保留其中任何一条都会误导）。
 *   而数据质量面板恰恰要靠 `received_at` 画"两条时间轴"、
 *   靠 `seq` 看跳号。
 *
 * ★ 代价是每 5 秒多一个请求，但 `limit=1`，响应体只有几百字节。
 *   这个代价值得付：**降采样接口不该假装自己能回答逐条的问题**。
 *   真让它在桶里"随便挑一条"返回 received_at，面板上那个数字就变成
 *   假的了 —— 而它恰恰是用来判断数据真假的。
 */
const newestRaw = usePolling<Page<TelemetryOut>>(
  () => {
    const deviceId = selectedDeviceId.value
    if (!deviceId) {
      return Promise.resolve<Page<TelemetryOut>>({
        total: 0,
        limit: 0,
        offset: 0,
        items: [],
      })
    }
    return api.telemetry({ device_id: deviceId, order: 'desc', limit: 1 })
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
/**
 * 曲线用的点，**显式按 ts 升序排序**。
 *
 * ★ 不依赖后端的返回顺序 —— 这里踩过一次：
 *
 *   原来是 `[...items].reverse()`，因为 `/api/telemetry` 用
 *   `order=desc` 返回（这样能拿"最新的 N 条"）。但画曲线改走
 *   `/api/telemetry/series` 之后，那个接口返回的是**升序**
 *   （后端 `order_by(bucket_expr)`）—— 再 reverse 就变成降序，
 *   于是曲线左右颠倒，而且下面插缺口的判断也会失效
 *   （`row.ts - prev.ts` 算出来是负数，永远不大于阈值）。
 *
 *   显式排序之后，两个接口的顺序差异不再影响这里。
 *   `sort` 比 `reverse` 贵一点，但几百个点可以忽略。
 */
const ascendingRows = computed<TelemetryBucketOut[]>(() =>
  [...(telemetry.data.value?.items ?? [])].sort((a, b) => a.ts - b.ts),
)

/**
 * 最新一条**原始**遥测。
 *
 * 用于数据质量面板的两条时间轴（`ts` / `received_at`）和 seq 跳号 ——
 * 这三样都必须来自逐条记录，降采样后的桶里没有。
 */
const newestRow = computed<TelemetryOut | null>(
  () => newestRaw.data.value?.items[0] ?? null,
)

/**
 * 曲线用的点。
 *
 * ★ 缺口处必须**插一个 null**，否则图表会撒谎。
 *
 *   降采样之后点与点之间是稀疏的：24 小时窗口里一个点代表 5 分钟。
 *   而实测出现过 **18.8 小时**的缺口（设备/后端停机）。
 *   ECharts 的时间轴会把相邻两点用直线连起来，**不管中间隔了多久**
 *   —— 于是"18.8 小时没有数据"在图上看起来就是"温度在几分钟内
 *   从 35℃ 掉到 33℃"。
 *
 *   这和 `connectNulls: false` 要解决的是同一类问题：
 *   **中断不能画成正常数据**。插一个 null 之后，配合
 *   `connectNulls: false`，那一段会如实断开。
 *
 *   阈值取 **3 倍桶宽**：正常波动（偶尔丢一两条报文）不会断开，
 *   真的断了才断。
 */
function toPoints(pick: (row: TelemetryBucketOut) => number | null): ChartPoint[] {
  const rows = ascendingRows.value
  const gapLimit = currentWindow.value.bucketMs * 3
  const out: ChartPoint[] = []

  for (let i = 0; i < rows.length; i++) {
    const row = rows[i] as TelemetryBucketOut
    if (i > 0) {
      const prev = rows[i - 1] as TelemetryBucketOut
      const delta = row.ts - prev.ts
      if (delta > gapLimit) {
        out.push({ ts: prev.ts + delta / 2, value: null })
      }
    }
    out.push({ ts: row.ts, value: pick(row) })
  }
  return out
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
    newestRaw.error.value ??
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
  void newestRaw.refresh()
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
        :min-span="2"
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
        <code>/api/devices/{id}/latest</code> · <code>/api/telemetry</code> ·
        <code>/api/telemetry/series</code>
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
