/**
 * 展示层的格式化工具。
 *
 * 为什么集中在一个文件：后端一律返回 Unix 毫秒整数（见 api/schemas.py 的说明），
 * "怎么显示"是前端的事。散在各组件里的话，"多久之前"这套算法会出现
 * 五六个略有差异的版本，而且改一处漏一处。
 */

/** 数值占位符。数据缺失时统一显示它，而不是 undefined / NaN / 空字符串。 */
export const EMPTY = '—'

export function formatNumber(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY
  return value.toFixed(digits)
}

/** 带正负号，用于显示"偏差"这类可正可负的量。 */
export function formatSigned(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(digits)}`
}

/** `HH:MM:SS`（本地时区）。 */
export function formatTime(ts: number | null | undefined): string {
  if (!ts) return EMPTY
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
}

/** `MM-DD HH:MM:SS`（本地时区）。 */
export function formatDateTime(ts: number | null | undefined): string {
  if (!ts) return EMPTY
  const d = new Date(ts)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}

/**
 * 把毫秒时长说成人话。
 *
 * ★ 为什么要分档而不是统一显示秒数：
 *   这个页面上会出现三种量级完全不同的时长——
 *     · 数据延迟（几百毫秒到几秒）
 *     · 网络断开的空档（几十秒到几分钟）
 *     · 后端没运行的一整段（几小时到一天）
 *   统一显示成秒的话，"1069.9 分钟"这种数字要心算才能理解，
 *   而心算错误会让人对严重程度判断失误。
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return EMPTY
  const abs = Math.abs(ms)

  if (abs < 1000) return `${Math.round(ms)} ms`
  if (abs < 60_000) return `${(ms / 1000).toFixed(abs < 10_000 ? 1 : 0)} 秒`
  if (abs < 3_600_000) return `${(ms / 60_000).toFixed(1)} 分钟`
  if (abs < 86_400_000) return `${(ms / 3_600_000).toFixed(1)} 小时`
  return `${(ms / 86_400_000).toFixed(1)} 天`
}

/** 相对时间："3.2 秒前"。用于"最后一次收到数据是什么时候"。 */
export function formatAgo(ts: number | null | undefined, now: number = Date.now()): string {
  if (!ts) return EMPTY
  return `${formatDuration(now - ts)}前`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return EMPTY
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  const unit = units[unitIndex] ?? 'B'
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${unit}`
}

/** 数字千分位。行数、消息数这类会到万级的数字需要它。 */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY
  return value.toLocaleString('zh-CN')
}
