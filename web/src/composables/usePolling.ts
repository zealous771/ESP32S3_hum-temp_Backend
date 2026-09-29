/**
 * 轮询 composable（文档 §25 的第一版方案：每 5 秒 GET 一次）。
 *
 * ★ 四个必须处理的问题，每一个都有具体的故障场景：
 *
 *   ① **请求重叠**：上一次还没回来，下一次又发出去。
 *      后端慢的时候（比如它自己在等 broker），请求会越堆越多，
 *      最后浏览器把同域并发连接占满，**整个页面都刷不动**。
 *      而且症状是"页面一直转圈"而不是报错，很难联想到是轮询造成的。
 *      → 用 inFlight 标记，上一次没回来就跳过这一轮。
 *
 *   ② **失败时清空数据**：一失败就把界面清空，看起来像"没有数据"，
 *      而实际情况可能只是"暂时连不上"。
 *      这两者指向完全不同的排查方向（设备侧 vs 网络/后端）。
 *      → 失败时保留上一次的好数据，另外用一个 error 字段表达"连不上"。
 *
 *   ③ **页面不可见时还在轮询**：用户切到别的标签页了，还每 5 秒发请求。
 *      → 监听 visibilitychange，隐藏时停、回来时立刻刷一次。
 *      回来立刻刷是关键：否则要等下一个周期，用户会看到几秒的旧数据。
 *
 *   ④ **卸载后定时器还在跑**：组件销毁了但 setInterval 没清，
 *      回调里访问已销毁的响应式对象。开发时热重载会反复出现这种情况，
 *      表现为"越改越慢、控制台里一堆重复请求"。
 *      → onBeforeUnmount 里清定时器和监听器。
 */

import { onBeforeUnmount, onMounted, ref, shallowRef, type Ref, type ShallowRef } from 'vue'

export interface Poller<T> {
  /** 最近一次**成功**拿到的数据。失败时保持上一次的值，不会被清空。 */
  data: ShallowRef<T | null>
  /** 最近一次失败的原因；成功时是 null。 */
  error: Ref<string | null>
  /** 最近一次成功的时刻（浏览器本地时钟） */
  lastUpdatedAt: Ref<number | null>
  /** 是否有请求在飞 */
  refreshing: Ref<boolean>
  /**
   * 连续失败次数。用来区分"偶发抖动"和"后端真的挂了"——
   * 一次失败就弹大红条会让人对真正的问题麻木，
   * 而连续 3 次以上基本可以确定不是抖动。
   */
  failures: Ref<number>
  /** 立刻刷一次（用户点"刷新"时用） */
  refresh: () => Promise<void>
}

export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
): Poller<T> {
  // 用 shallowRef 而不是 ref：每次拿到的是**整个替换**的新对象，
  // 不需要 Vue 递归做响应式代理。深代理一个几百条的数组纯属浪费。
  const data = shallowRef<T | null>(null)
  const error = ref<string | null>(null)
  const lastUpdatedAt = ref<number | null>(null)
  const refreshing = ref(false)
  const failures = ref(0)

  let inFlight = false
  let timer: number | null = null

  async function refresh(): Promise<void> {
    if (inFlight) return // ← 问题 ①
    inFlight = true
    refreshing.value = true
    try {
      const result = await fetcher()
      data.value = result
      lastUpdatedAt.value = Date.now()
      failures.value = 0
      error.value = null
    } catch (err) {
      // ← 问题 ②：不清空 data
      error.value = err instanceof Error ? err.message : String(err)
      failures.value += 1
    } finally {
      inFlight = false
      refreshing.value = false
    }
  }

  function start(): void {
    if (timer !== null) return
    void refresh()
    timer = window.setInterval(() => {
      void refresh()
    }, intervalMs)
  }

  function stop(): void {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
  }

  function onVisibilityChange(): void {
    // ← 问题 ③
    if (document.hidden) stop()
    else start()
  }

  onMounted(() => {
    start()
    document.addEventListener('visibilitychange', onVisibilityChange)
  })

  onBeforeUnmount(() => {
    // ← 问题 ④
    stop()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  return { data, error, lastUpdatedAt, refreshing, failures, refresh }
}
