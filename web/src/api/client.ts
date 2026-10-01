/**
 * HTTP 客户端。
 *
 * ★ 基址是空字符串 —— 也就是**同源相对路径** `/api/...`。
 *
 *   开发时：页面在 5173，请求发到 5173 的 /api/...，
 *           由 Vite 的开发服务器代理到 8005（见 vite.config.ts）。
 *   生产时：页面和 API 都由 FastAPI 在 8005 提供，本来就是同源。
 *
 *   两种环境都**不需要 CORS**。如果这里写死 `http://localhost:8005`，
 *   开发环境立刻变成跨域，就不得不去后端开 allow_origins——
 *   而那是个只在开发环境才需要的配置，很容易忘了在部署时收紧。
 */

import type {
  AnomalyOut,
  DeviceDetailOut,
  DeviceOut,
  HealthResponse,
  LatestTelemetryOut,
  Page,
  PredictionOut,
  TelemetryBucketOut,
  TelemetryOut,
} from './types'

/** 请求超时。轮询间隔是 5 秒，超时必须明显小于它。 */
const DEFAULT_TIMEOUT_MS = 8000

export class ApiError extends Error {
  readonly status: number | undefined

  constructor(message: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function buildUrl(path: string, params?: Record<string, string | number | undefined>): string {
  // 用相对路径 + window.location.origin 拼，而不是直接拼字符串：
  // 这样如果将来前端被挂到子路径（比如 /dashboard/），
  // 相对路径会自动跟着走，不用改代码。
  const url = new URL(path, window.location.origin)
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) {
        url.searchParams.set(key, String(value))
      }
    }
  }
  return url.toString()
}

async function readErrorDetail(res: Response): Promise<string> {
  // FastAPI 的错误体是 {"detail": ...}。
  // 422 的 detail 是一个数组（每个字段一条），这里尽量原样带出来——
  // 参数写错时那一串比 "HTTP 422" 有用得多。
  try {
    const body: unknown = await res.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      return typeof detail === 'string' ? detail : JSON.stringify(detail)
    }
    return JSON.stringify(body)
  } catch {
    return ''
  }
}

async function getJson<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  // ★ AbortController 是必需的，不是可选的健壮性装饰。
  //
  //   没有超时的话，一个卡住的请求会一直挂着，而轮询的下一次又发出去——
  //   请求越堆越多，最后浏览器把同域并发连接占满，**整个页面都刷不动**。
  //   而且失败会表现成"页面一直在转圈"而不是"报错"。
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)

  try {
    const res = await fetch(buildUrl(path, params), {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
      // 不用 cache: 'no-store'：GET 请求本来就不带缓存头，
      // 而 no-store 会阻止浏览器的正常 HTTP 缓存语义。
      // 轮询的时效性由"每 5 秒重新请求"保证，不靠禁缓存。
    })

    if (!res.ok) {
      const detail = await readErrorDetail(res)
      throw new ApiError(`HTTP ${res.status}${detail ? ` — ${detail}` : ''}`, res.status)
    }

    return (await res.json()) as T
  } catch (err) {
    if (err instanceof ApiError) throw err
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError(`请求超时（${timeoutMs} ms）`)
    }
    // fetch 只在网络层失败时抛（DNS、连接被拒、CORS 被拦）。
    // 最常见的原因是后端没起或容器挂了。
    throw new ApiError(err instanceof Error ? err.message : String(err))
  } finally {
    window.clearTimeout(timer)
  }
}

export interface TelemetryQuery {
  device_id?: string
  /** 起始时刻（Unix 毫秒，闭区间） */
  start?: number
  /** 结束时刻（Unix 毫秒，闭区间） */
  end?: number
  limit?: number
  offset?: number
  /** asc / desc，默认 desc */
  order?: 'asc' | 'desc'
  /**
   * 时间桶宽度（毫秒）。**只有 `/api/telemetry/series` 认这个参数**。
   *
   * 给定时每条返回记录代表一个桶的聚合值（ts 是桶起点），
   * 于是"24 小时窗口"才装得下——逐条返回的话 2 秒一条会超出
   * 1000 条的分页上限（只覆盖 33 分钟）。
   */
  bucket_ms?: number
}

export const api = {
  health: () => getJson<HealthResponse>('/api/health'),

  devices: (limit = 100) => getJson<Page<DeviceOut>>('/api/devices', { limit }),

  device: (deviceId: string) =>
    getJson<DeviceDetailOut>(`/api/devices/${encodeURIComponent(deviceId)}`),

  latest: (deviceId: string) =>
    getJson<LatestTelemetryOut>(`/api/devices/${encodeURIComponent(deviceId)}/latest`),

  telemetry: (query: TelemetryQuery) =>
    getJson<Page<TelemetryOut>>('/api/telemetry', {
      device_id: query.device_id,
      start: query.start,
      end: query.end,
      limit: query.limit,
      offset: query.offset,
      order: query.order,
    }),

  /**
   * 降采样时间序列 —— 长时间窗口画曲线用。
   *
   * ★ 和 `telemetry()` 的分工：
   *
   *   telemetry()        逐条原始记录。受 MAX_LIMIT=1000 约束，
   *                      2 秒一条 → 最多覆盖 **33 分钟**。
   *                      要 `received_at` / `seq` 的场合（数据质量面板）
   *                      只能用这个。
   *
   *   telemetrySeries()  按 `bucket_ms` 聚合。24 小时用 5 分钟桶
   *                      只有 288 个点。**画长窗口曲线只能用这个。**
   *
   *   两个都不传 limit：series 不分页（桶数天然只有几百）。
   */
  telemetrySeries: (query: TelemetryQuery) =>
    getJson<Page<TelemetryBucketOut>>('/api/telemetry/series', {
      device_id: query.device_id,
      start: query.start,
      end: query.end,
      bucket_ms: query.bucket_ms,
    }),

  anomalies: (query: { device_id?: string; limit?: number } = {}) =>
    getJson<Page<AnomalyOut>>('/api/anomalies', {
      device_id: query.device_id,
      limit: query.limit ?? 50,
    }),

  predictions: (query: { device_id?: string; limit?: number } = {}) =>
    getJson<Page<PredictionOut>>('/api/predictions', {
      device_id: query.device_id,
      limit: query.limit ?? 50,
    }),
}
