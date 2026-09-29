<script setup lang="ts">
/**
 * 数据质量面板 —— **这个页面里最有价值的一块**。
 *
 * 为什么它不是"锦上添花的附加信息"：
 *
 *   这个项目实测踩过一类故障——**设备连续发了 41 分钟后彻底停发，
 *   持续 2.5 小时，而它本地一切正常**（MQTT 连接在、队列零丢弃、
 *   串口照常打印温湿度）。从后端看只是"没有新数据"，
 *   和"设备没采到"长得一模一样。
 *
 *   一条温度曲线在那种情况下会**只是停止延伸**——而停止延伸的曲线
 *   和"刚打开页面还没加载完"看起来差不多。光看曲线根本发现不了。
 *
 *   所以这个面板存在的理由是：**把"数据还在不在流"变成一个明确的判断**，
 *   而不是让人对着曲线猜。
 *
 * 面板回答四个递进的问题：
 *
 *   ① 数据还在进来吗？        data.age_ms / data.stale
 *   ② 如果不在，断了多久？    data.recent_gaps（从哪一刻到哪一刻）
 *   ③ 是哪一段链路的问题？    mqtt.connected / ingest.running / db.journal_mode
 *   ④ 丢了多少？              seq 跳号 / queue_dropped / 估算漏的条数
 *
 * ★ ①和③必须分开显示。
 *
 *   "请求失败"（后端不可达）和"请求成功但没有新数据"（设备侧问题）
 *   是完全不同的两件事，混在一起会把人引向错误的排查方向。
 *   前者是这一层的 error，后者是 data.stale——UI 上必须能一眼区分。
 */

import { computed } from 'vue'
import StatusPill from './StatusPill.vue'
import type { HealthResponse, TelemetryOut } from '../api/types'
import {
  EMPTY,
  formatAgo,
  formatBytes,
  formatCount,
  formatDateTime,
  formatDuration,
  formatSigned,
} from '../format'

const props = defineProps<{
  health: HealthResponse | null
  /** 最新一条遥测（来自曲线数据的最后一个点）。用来算两条时间轴的差。 */
  newest: TelemetryOut | null
  /** 后端是否可达。false 时整个面板的语义变成"数据是旧的，因为连不上" */
  reachable: boolean
}>()

// ── 数据流活性 ──────────────────────────────────────────────────────

const dataFlowTone = computed(() => {
  if (!props.reachable) return 'bad' as const
  const info = props.health?.data
  if (!info) return 'idle' as const
  if (info.stale) return 'bad' as const
  return 'ok' as const
})

const dataFlowLabel = computed(() => {
  if (!props.reachable) return '后端不可达'
  const info = props.health?.data
  if (!info) return '等待数据'
  return info.stale ? '数据已停' : '数据在流'
})

const lastReceivedText = computed(() => {
  const info = props.health?.data
  if (!info?.last_received_at) return EMPTY
  return `${formatDateTime(info.last_received_at)}（${formatAgo(info.last_received_at, props.health?.now_ms ?? Date.now())}）`
})

// ── 两条时间轴的差 ─────────────────────────────────────────────────

/**
 * `ts - received_at`。
 *
 * ★ 这个值**不能**直接叫"时钟偏差"。
 *
 *   ts 是设备**采样**时刻，received_at 是后端**收到**时刻，两者之间
 *   必然隔着一段传输时间（采样 → 组报文 → publish → broker → 后端）。
 *   所以：
 *
 *        ts - received_at = 设备时钟超前量 − 传输延迟
 *
 *   传输延迟不可能是负的，因此：
 *       · 值 > 0  → 设备时钟**至少**超前这么多
 *       · 值 < 0  → 设备时钟超前量小于传输延迟，无法分离
 *
 *   实测这个值在 −2.3 秒到 −0.3 秒之间波动，说明设备与后端用的
 *   不是同一个 NTP 源（设备 ntp.aliyun.com，WSL 用自己的）。
 *   量级很小不影响判断，但要知道它存在——将来做跨设备时间对齐时
 *   必须以 received_at 为准。
 */
const clockDiffMs = computed(() => {
  const row = props.newest
  if (!row) return null
  return row.ts - row.received_at
})

const clockDiffHint = computed(() => {
  const diff = clockDiffMs.value
  if (diff === null) return ''
  if (diff > 0) {
    return `设备时钟至少超前 ${formatDuration(diff)}（ts − received_at；传输延迟不可分离，所以是下界）`
  }
  return `设备时钟超前量小于传输延迟（ts − received_at = ${formatDuration(diff)}），无法分离出具体偏差`
})

// ── 链路各段的状态 ─────────────────────────────────────────────────

const mqttTone = computed(() => {
  const mqtt = props.health?.mqtt
  if (!mqtt) return 'idle' as const
  if (!mqtt.enabled) return 'idle' as const
  return mqtt.connected ? ('ok' as const) : ('bad' as const)
})

const mqttLabel = computed(() => {
  const mqtt = props.health?.mqtt
  if (!mqtt) return '未知'
  if (!mqtt.enabled) return '未启用'
  return mqtt.connected ? '已连接' : '断开'
})

const ingestTone = computed(() => {
  const ingest = props.health?.ingest
  if (!ingest) return 'idle' as const
  return ingest.running ? ('ok' as const) : ('bad' as const)
})

const walTone = computed(() => {
  const mode = props.health?.db.journal_mode
  if (!mode) return 'idle' as const
  return mode === 'wal' ? ('ok' as const) : ('bad' as const)
})

const retentionTone = computed(() => {
  const r = props.health?.retention
  if (!r) return 'idle' as const
  if (!r.enabled) return 'idle' as const
  return r.running ? ('ok' as const) : ('bad' as const)
})

const retentionLabel = computed(() => {
  const r = props.health?.retention
  if (!r) return '未知'
  if (!r.enabled) return '未启用'
  return r.running ? '运行中' : '线程已停'
})

// ── seq 跳号 ────────────────────────────────────────────────────────

const seqDevices = computed(() => {
  const detail = props.health?.ingest.seq_detail
  if (!detail) return []
  return Object.entries(detail.last_seq).map(([deviceId, lastSeq]) => ({
    deviceId,
    lastSeq,
    missing: detail.missing[deviceId] ?? 0,
    restarts: detail.restarts[deviceId] ?? 0,
  }))
})

const totalMissing = computed(() =>
  seqDevices.value.reduce((sum, d) => sum + d.missing, 0),
)

// ── 入库断层 ────────────────────────────────────────────────────────

const gaps = computed(() => props.health?.data.recent_gaps ?? [])
</script>

<template>
  <section class="panel">
    <header class="panel__head">
      <h2 class="panel__title">数据质量</h2>
      <StatusPill :tone="dataFlowTone" :label="dataFlowLabel" hint="基于后端时钟：最后一条数据距今多久" />
    </header>

    <!-- ── ① 数据流活性（最重要的一行，给最大的字号） ────────────── -->
    <div class="hero" :class="`hero--${dataFlowTone}`">
      <div class="hero__main">
        <div class="hero__label">最后一条数据</div>
        <div class="hero__value">
          {{ health?.data.age_ms != null ? formatDuration(health.data.age_ms) + '前' : EMPTY }}
        </div>
        <div class="hero__sub">{{ lastReceivedText }}</div>
      </div>
      <div class="hero__side">
        <div class="hero__label">陈旧阈值</div>
        <div class="hero__small">
          {{ health?.data.stale_threshold_ms != null ? formatDuration(health.data.stale_threshold_ms) : EMPTY }}
        </div>
        <div class="hero__hint">超过它就算"数据已停"</div>
      </div>
    </div>

    <!-- ── ② 断层：断了多久、什么时候断的 ────────────────────────── -->
    <div class="block">
      <h3 class="block__title">
        入库断层
        <span class="block__note">按 <code>received_at</code>（后端时钟）检测，最近 {{ gaps.length }} 次</span>
      </h3>

      <p v-if="gaps.length === 0" class="ok-note">
        没有断层 —— 从后端开始记录以来，数据一直是连续的。
      </p>

      <table v-else class="table">
        <thead>
          <tr>
            <th>设备</th>
            <th>断开</th>
            <th>恢复</th>
            <th class="num">时长</th>
            <th class="num">估算漏</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="gap in gaps" :key="`${gap.device_id}-${gap.gap_start_ms}`">
            <td class="mono">{{ gap.device_id }}</td>
            <td class="mono">{{ formatDateTime(gap.gap_start_ms) }}</td>
            <td class="mono">{{ formatDateTime(gap.gap_end_ms) }}</td>
            <td class="num mono">{{ formatDuration(gap.gap_ms) }}</td>
            <td class="num mono">{{ formatCount(gap.missed_estimate) }} 条</td>
          </tr>
        </tbody>
      </table>
      <p v-if="gaps.length" class="hint">
        「估算漏」按采样周期推算，<b>包含设备没采到的情况</b>——它衡量的是
        "这段时间没有数据入库"，不区分原因。要区分得看设备侧串口日志。
      </p>
    </div>

    <!-- ── ③ 链路各段 ───────────────────────────────────────────── -->
    <div class="block">
      <h3 class="block__title">链路状态</h3>
      <div class="pills">
        <StatusPill
          :tone="mqttTone"
          :label="`MQTT ${mqttLabel}`"
          :hint="`broker=${health?.mqtt.host ?? '?'}:${health?.mqtt.port ?? '?'}  client_id=${health?.mqtt.client_id ?? '?'}`"
        />
        <StatusPill
          :tone="ingestTone"
          :label="ingestTone === 'ok' ? '写库线程在跑' : '写库线程已停'"
          hint="它退出后 MQTT 连接仍显示正常、队列只进不出，但数据不再落库 —— 所以单独报出来"
        />
        <StatusPill
          :tone="walTone"
          :label="`SQLite ${health?.db.journal_mode ?? '?'}`"
          hint="必须是 wal。不是的话说明库文件在 DrvFs（/mnt/c）上，WAL 会静默降级"
        />
        <StatusPill
          :tone="retentionTone"
          :label="`保留策略 ${retentionLabel}`"
          :hint="health?.retention.enabled ? `保留 ${health.retention.retention_days} 天` : '未启用（数据不清理，一直攒着）'"
        />
      </div>

      <div class="grid">
        <div class="kv">
          <span class="kv__k">MQTT 连接次数</span>
          <span class="kv__v mono">{{ formatCount(health?.mqtt.connects) }}</span>
        </div>
        <div class="kv">
          <span class="kv__k">断开次数</span>
          <span class="kv__v mono" :class="{ 'kv__v--warn': (health?.mqtt.disconnects ?? 0) > 0 }">
            {{ formatCount(health?.mqtt.disconnects) }}
          </span>
        </div>
        <div class="kv">
          <span class="kv__k">订阅</span>
          <span class="kv__v mono small">{{ (health?.mqtt.subscriptions ?? []).join(' , ') || EMPTY }}</span>
        </div>
        <div class="kv">
          <span class="kv__k">写队列深度</span>
          <span class="kv__v mono">{{ formatCount(health?.ingest.queue_depth) }}</span>
        </div>
        <div class="kv">
          <span class="kv__k">队列历史水位</span>
          <span class="kv__v mono" :class="{ 'kv__v--warn': (health?.ingest.queue_high_watermark ?? 0) > 1 }">
            {{ formatCount(health?.ingest.queue_high_watermark) }}
          </span>
        </div>
        <div class="kv">
          <span class="kv__k">库文件</span>
          <span class="kv__v mono">
            {{ formatBytes(health?.db.size_bytes) }} / {{ formatCount(health?.db.telemetry_rows) }} 行
          </span>
        </div>
      </div>
    </div>

    <!-- ── ④ 丢了多少 ───────────────────────────────────────────── -->
    <div class="block">
      <h3 class="block__title">
        丢数据统计
        <span class="block__note">seq 跳号是"设备发了但没到后端"的唯一证据</span>
      </h3>

      <div class="grid">
        <div class="kv">
          <span class="kv__k">seq 跳号事件</span>
          <span class="kv__v mono" :class="{ 'kv__v--warn': (health?.ingest.seq_gap_events ?? 0) > 0 }">
            {{ formatCount(health?.ingest.seq_gap_events) }}
          </span>
        </div>
        <div class="kv">
          <span class="kv__k">seq 累计缺口</span>
          <span class="kv__v mono" :class="{ 'kv__v--warn': totalMissing > 0 }">
            {{ formatCount(totalMissing) }} 条
          </span>
        </div>
        <div class="kv">
          <span class="kv__k">队列满丢弃</span>
          <span class="kv__v mono" :class="{ 'kv__v--warn': (health?.ingest.queue_dropped ?? 0) > 0 }">
            {{ formatCount(health?.ingest.queue_dropped) }}
          </span>
        </div>
        <div class="kv">
          <span class="kv__k">QoS1 重投去重</span>
          <span class="kv__v mono">{{ formatCount(health?.ingest.duplicates_skipped) }}</span>
        </div>
        <div class="kv">
          <span class="kv__k">报文校验失败</span>
          <span class="kv__v mono" :class="{ 'kv__v--warn': (health?.ingest.invalid_messages ?? 0) > 0 }">
            {{ formatCount(health?.ingest.invalid_messages) }}
          </span>
        </div>
        <div class="kv">
          <span class="kv__k">device_id 不一致</span>
          <span class="kv__v mono" :class="{ 'kv__v--warn': (health?.ingest.mismatched_device_id ?? 0) > 0 }">
            {{ formatCount(health?.ingest.mismatched_device_id) }}
          </span>
        </div>
      </div>

      <table v-if="seqDevices.length" class="table table--tight">
        <thead>
          <tr>
            <th>设备</th>
            <th class="num">最后 seq</th>
            <th class="num">累计缺口</th>
            <th class="num">检测到重启</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in seqDevices" :key="d.deviceId">
            <td class="mono">{{ d.deviceId }}</td>
            <td class="num mono">{{ formatCount(d.lastSeq) }}</td>
            <td class="num mono" :class="{ 'kv__v--warn': d.missing > 0 }">{{ formatCount(d.missing) }}</td>
            <td class="num mono">{{ formatCount(d.restarts) }}</td>
          </tr>
        </tbody>
      </table>
      <p class="hint">
        「检测到重启」和「跳号」是**分开统计**的：设备断电重启后 seq 会从 1 重新开始，
        只看"比上次小就是异常"的话，每次重启都会报出一个巨大缺口（比如 5000 掉到 1，
        报丢 4998 条），把真正的丢包淹没掉。
      </p>
    </div>

    <!-- ── 两条时间轴 ───────────────────────────────────────────── -->
    <div class="block">
      <h3 class="block__title">
        两条时间轴
        <span class="block__note">ts = 设备采样时刻 · received_at = 后端收到时刻</span>
      </h3>

      <div v-if="newest" class="grid">
        <div class="kv">
          <span class="kv__k">最新采样时刻 ts</span>
          <span class="kv__v mono">{{ formatDateTime(newest.ts) }}</span>
        </div>
        <div class="kv">
          <span class="kv__k">后端收到时刻 received_at</span>
          <span class="kv__v mono">{{ formatDateTime(newest.received_at) }}</span>
        </div>
        <div class="kv">
          <span class="kv__k" :title="clockDiffHint">ts − received_at</span>
          <span class="kv__v mono">{{ formatSigned(clockDiffMs ? clockDiffMs / 1000 : null, 1) }} 秒</span>
        </div>
        <div class="kv">
          <span class="kv__k">采样序号 seq</span>
          <span class="kv__v mono">{{ formatCount(newest.seq) }}</span>
        </div>
      </div>
      <p v-else class="hint">还没有数据，无法比较。</p>

      <p class="hint">
        <code>ts − received_at</code> 不是纯粹的"时钟偏差"——它等于
        <b>设备时钟超前量 − 传输延迟</b>。传输延迟不可能是负的，
        所以正值表示设备时钟<b>至少</b>超前这么多；负值则说明超前量小于延迟，无法分离。
        实测这个值在 −2.3 秒到 −0.3 秒之间，说明设备与后端用的不是同一个 NTP 源。
        将来做跨设备时间对齐时，以 <code>received_at</code> 为准。
      </p>
    </div>
  </section>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.panel__title {
  margin: 0;
  font-size: 17px;
  font-weight: 650;
  color: var(--c-text-strong);
}

/* ── 数据流活性 ───────────────────────────────────────────────── */

.hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 16px 18px;
  border-radius: 12px;
  border: 1px solid var(--c-border);
  background: var(--c-surface);
  border-left: 5px solid var(--c-accent);
}

.hero--ok { border-left-color: var(--c-ok); }
.hero--warn { border-left-color: var(--c-warn); }
.hero--bad { border-left-color: var(--c-bad); }
.hero--idle { border-left-color: var(--c-border-strong); }

.hero__label {
  font-size: 12px;
  color: var(--c-muted);
  font-weight: 500;
}

.hero__value {
  font-size: 26px;
  font-weight: 650;
  color: var(--c-text-strong);
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
  margin-top: 2px;
}

.hero__sub {
  font-size: 12px;
  color: var(--c-muted);
  font-variant-numeric: tabular-nums;
  margin-top: 3px;
}

.hero__side {
  text-align: right;
  flex: none;
}

.hero__small {
  font-size: 16px;
  font-weight: 600;
  color: var(--c-text);
  font-variant-numeric: tabular-nums;
  margin-top: 2px;
}

.hero__hint {
  font-size: 11px;
  color: var(--c-muted);
  margin-top: 2px;
}

/* ── 区块 ─────────────────────────────────────────────────────── */

.block {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 12px;
  padding: 14px 16px;
}

.block__title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
  color: var(--c-text-strong);
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.block__note {
  font-size: 12px;
  font-weight: 400;
  color: var(--c-muted);
}

.pills {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

/* ── 键值网格 ─────────────────────────────────────────────────── */

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 8px 18px;
}

.kv {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 5px 0;
  border-bottom: 1px dashed var(--c-border);
  min-width: 0;
}

.kv__k {
  font-size: 12.5px;
  color: var(--c-muted);
  flex: none;
}

.kv__v {
  font-size: 13px;
  color: var(--c-text);
  font-weight: 550;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kv__v--warn {
  color: var(--c-warn);
}

.kv__v.small {
  font-size: 11.5px;
}

/* ── 表格 ─────────────────────────────────────────────────────── */

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
  margin-top: 4px;
}

.table--tight {
  margin-top: 12px;
}

.table th {
  text-align: left;
  font-weight: 600;
  color: var(--c-muted);
  padding: 6px 8px;
  border-bottom: 1px solid var(--c-border-strong);
  white-space: nowrap;
}

.table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--c-border);
  color: var(--c-text);
}

.table .num {
  text-align: right;
}

.mono {
  font-variant-numeric: tabular-nums;
}

.ok-note {
  margin: 0;
  font-size: 13px;
  color: var(--c-ok);
  background: var(--c-ok-bg);
  border: 1px solid var(--c-ok-border);
  border-radius: 8px;
  padding: 8px 12px;
}

.hint {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.65;
  color: var(--c-muted);
}

.hint code,
.block__note code {
  background: var(--c-idle-bg);
  border: 1px solid var(--c-border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 11.5px;
}
</style>
