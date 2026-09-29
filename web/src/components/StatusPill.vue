<script setup lang="ts">
/**
 * 状态胶囊：一个带颜色圆点的短标签。
 *
 * 单独抽出来是因为页面上有七八处需要它（MQTT 连接、数据流、写库线程、
 * WAL、保留策略……）。每处自己写一遍 span + class 的话，
 * 颜色语义很快就会不一致——同样表示"正常"出现三种绿。
 */

export type Tone = 'ok' | 'warn' | 'bad' | 'idle'

defineProps<{
  tone: Tone
  label: string
  /** 鼠标悬停时的补充说明。放"这个状态意味着什么"，而不是重复 label。 */
  hint?: string
}>()
</script>

<template>
  <span class="pill" :class="`pill--${tone}`" :title="hint">
    <span class="pill__dot" aria-hidden="true" />
    <span class="pill__label">{{ label }}</span>
  </span>
</template>

<style scoped>
.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px 3px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.5;
  white-space: nowrap;
  border: 1px solid transparent;
}

.pill__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  flex: none;
}

.pill__label {
  color: var(--c-text);
}

.pill--ok {
  color: var(--c-ok);
  background: var(--c-ok-bg);
  border-color: var(--c-ok-border);
}

.pill--warn {
  color: var(--c-warn);
  background: var(--c-warn-bg);
  border-color: var(--c-warn-border);
}

.pill--bad {
  color: var(--c-bad);
  background: var(--c-bad-bg);
  border-color: var(--c-bad-border);
}

.pill--idle {
  color: var(--c-muted);
  background: var(--c-idle-bg);
  border-color: var(--c-border);
}
</style>
