<script setup lang="ts">
/**
 * 一张指标卡片（温度 / 湿度 / RSSI / 状态）。
 *
 * ★ `value` 收的是**已经格式化好的字符串**，不是数字。
 *
 *   让卡片负责"怎么显示"会把格式化逻辑摊到每个调用点，
 *   而"缺失值怎么显示"这件事必须统一（见 format.ts 的 EMPTY）。
 *   传字符串之后，调用方用 formatNumber / formatSigned 处理，
 *   卡片只管排版。
 */

defineProps<{
  label: string
  /** 已格式化的值；缺失时调用方传 EMPTY（'—'） */
  value: string
  unit?: string
  /** 数值的语义色：温度偏高偏红、RSSI 偏弱偏黄等 */
  tone?: 'normal' | 'warn' | 'bad'
  /** 副标题：放"这个值是什么时候的"之类的上下文 */
  caption?: string
  /** 是否处于"值缺失"状态（用于把主数字画成灰色） */
  missing?: boolean
}>()
</script>

<template>
  <div class="card" :class="[`card--${tone ?? 'normal'}`, { 'card--missing': missing }]">
    <div class="card__label">{{ label }}</div>
    <div class="card__value">
      <span class="card__number">{{ value }}</span>
      <span v-if="unit" class="card__unit">{{ unit }}</span>
    </div>
    <div v-if="caption" class="card__caption">{{ caption }}</div>
  </div>
</template>

<style scoped>
.card {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: 12px;
  padding: 16px 18px;
  /* 左侧色条表示语义，比整张卡片染色克制——四张卡片都染色会太吵 */
  border-left: 4px solid var(--c-accent);
  min-width: 0;
}

.card--normal {
  border-left-color: var(--c-accent);
}

.card--warn {
  border-left-color: var(--c-warn);
}

.card--bad {
  border-left-color: var(--c-bad);
}

.card--missing {
  border-left-color: var(--c-border-strong);
}

.card__label {
  font-size: 13px;
  color: var(--c-muted);
  font-weight: 500;
}

.card__value {
  display: flex;
  align-items: baseline;
  gap: 4px;
  margin-top: 6px;
}

.card__number {
  font-size: 30px;
  font-weight: 650;
  line-height: 1.1;
  /* 等宽数字：每 5 秒刷新一次时，宽度不变才不会左右抖动 */
  font-variant-numeric: tabular-nums;
  color: var(--c-text-strong);
}

.card--missing .card__number {
  color: var(--c-muted);
}

.card__unit {
  font-size: 14px;
  color: var(--c-muted);
  font-weight: 500;
}

.card__caption {
  margin-top: 6px;
  font-size: 12px;
  color: var(--c-muted);
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
