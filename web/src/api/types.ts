/**
 * 后端接口的 TypeScript 类型。
 *
 * ★ 这个文件是 `backend/app/api/schemas.py` 的**镜像**，不是从文档抄的。
 *
 *   改后端响应模型时**必须同步改这里**，否则前端会在运行期拿到
 *   `undefined`——而 TypeScript 不会报错，因为类型是你手写的、
 *   它当然相信你。表现是页面上出现空白或 NaN，而不是编译失败。
 *
 *   为什么不用 openapi-typescript 从 /openapi.json 自动生成：
 *     它需要在**构建时**能访问到运行中的后端。而前端是在 Docker
 *     构建阶段编译的，那时后端容器还没起——要么把构建拆成两步，
 *     要么在构建时连一个外部服务，两种都让构建变得不确定。
 *     手写 + 在接口旁边标注对应关系，是当前权衡下的选择。
 *     （将来后端稳定了，可以加一个 `npm run gen:api` 手动跑生成，
 *       然后 diff 检查有没有漂移——那是个值得做的后续改进。）
 *
 * ★ 时间字段一律是 **Unix 毫秒整数**（见后端 api/schemas.py 的说明）。
 *   渲染成人能读的格式是展示层的事，在 format.ts 里做。
 */

// ── 通用信封 ────────────────────────────────────────────────────────

/** 分页信封。`total` 是**符合筛选条件的总数**，不是当前页条数。 */
export interface Page<T> {
  total: number
  limit: number
  offset: number
  items: T[]
}

// ── 遥测 ────────────────────────────────────────────────────────────

/** `GET /api/telemetry` 的一条记录。字段名与数据库列名一致。 */
export interface TelemetryOut {
  id: number
  /** 设备**采样**时刻（设备时钟） */
  ts: number
  /** 后端**收到**时刻（后端时钟）。只有它能发现"设备静默停发"。 */
  received_at: number
  temperature: number
  humidity: number
  /** 未连接 WiFi 时是 null。**不要用 0 当"未知"**——0 dBm 是合法强信号。 */
  rssi: number | null
  seq: number
  /** ts 的时间基准：unix / boot */
  clock: string
}

/**
 * `GET /api/devices/{id}/latest` 的响应。
 *
 * 字段集合**恰好**是设计文档 §23 示例里的 5 个。刻意不含 received_at / seq——
 * 这个接口会被首页每 5 秒调一次（文档 §25），响应体越小越好。
 * 需要更多字段就用 /api/telemetry。
 */
export interface LatestTelemetryOut {
  device_id: string
  temperature: number
  humidity: number
  rssi: number | null
  ts: number
}

// ── 设备 ────────────────────────────────────────────────────────────

export interface DeviceOut {
  device_id: string
  name: string | null
  /** 设备最后上报的状态（来自 iot/<id>/status） */
  status: string
  /** 后端**读时计算**的在线状态：status=='online' 且 last_seen 距今 <=30 秒 */
  online: boolean
  last_seen: number | null
  created_at: number
}

export interface DeviceDetailOut extends DeviceOut {
  telemetry_count: number
  first_ts: number | null
  last_ts: number | null
  last_seq: number | null
}

// ── 异常与预测（AI 未实现，当前恒为空）──────────────────────────────

export interface AnomalyOut {
  id: number
  device_id: string
  ts: number
  metric: string
  score: number
  /** info / warning / critical */
  level: string
  reason: string | null
}

export interface PredictionOut {
  id: number
  device_id: string
  /** ★ 预测**生成**时刻，不是被预测的时刻。被预测时刻 = ts + horizon*1000 */
  ts: number
  /** 向前预测多少秒 */
  horizon: number
  metric: string
  value: number
}

// ── /api/health 的六段 ──────────────────────────────────────────────

export interface MqttInfo {
  enabled: boolean
  connected: boolean
  /** 组件缺失时（测试/初始化跳过）后端会返回 {enabled:false, reason:"..."} */
  reason?: string
  host?: string
  port?: number
  client_id?: string
  subscriptions?: string[]
  connects?: number
  disconnects?: number
  last_message_ms?: number
  last_message_age_ms?: number | null
}

export interface SeqDetail {
  /** 每台设备的最后一个 seq */
  last_seq: Record<string, number>
  /** 每台设备累计跳号缺口 */
  missing: Record<string, number>
  /** 每台设备检测到的重启次数 */
  restarts: Record<string, number>
}

export interface IngestInfo {
  /** 组件缺失时是 false */
  enabled?: boolean
  /** ★ 写库线程还活着吗。它退出后没有任何其他症状，所以必须报出来。 */
  running: boolean

  messages_received?: number
  topics_ignored?: number
  invalid_messages?: number
  mismatched_device_id?: number
  persisted?: number
  duplicates_skipped?: number
  boot_rows_persisted?: number
  boot_rows_dropped?: number
  queue_dropped?: number
  queue_high_watermark?: number
  seq_gap_events?: number
  seq_missing_messages?: number
  devices_seen?: string[]

  queue_depth?: number
  seq_detail?: SeqDetail
}

export interface DbInfo {
  url: string
  path: string | null
  size_bytes: number | null
  /** ★ 必须是 "wal"。不是的话说明库在 DrvFs 上，WAL 静默降级了。 */
  journal_mode: string
  telemetry_rows: number
  devices: number
}

export interface RetentionInfo {
  enabled: boolean
  running: boolean
  runs?: number
  deleted_total?: number
  last_deleted?: number
  last_cutoff_ms?: number | null
  last_run_ms?: number | null
  last_duration_ms?: number
  retention_days?: number
  interval_s?: number
  chunk_size?: number
}

/** 入库时间轴上的一个断层。 */
export interface ReceiveGap {
  device_id: string
  gap_start_ms: number
  gap_end_ms: number
  gap_ms: number
  /** 按采样周期估算漏了多少条 */
  missed_estimate: number
}

export interface DataInfo {
  last_received_at: number | null
  age_ms: number | null
  /** ★ age_ms 是否超过阈值。这是"后端还在收数据吗"的直接答案。 */
  stale: boolean
  stale_threshold_ms: number
  recent_gaps: ReceiveGap[]
}

export interface SettingsInfo {
  broker: string
  client_id: string
  subscriptions: string[]
  persist_boot_rows: boolean
  telemetry_retention_days: number
}

export interface HealthResponse {
  status: string
  now_ms: number
  mqtt: MqttInfo
  ingest: IngestInfo
  db: DbInfo
  retention: RetentionInfo
  data: DataInfo
  settings: SettingsInfo
}
