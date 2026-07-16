<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const HEART_COUNT = 5

const props = withDefaults(defineProps<{
  emotion?: string | null
  affection?: number | null
  energy?: number | null
  variant?: 'card' | 'header'
  showMetrics?: boolean
}>(), {
  variant: 'card',
  showMetrics: true,
})

const { t } = useI18n()

function clampScore(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 0
  return Math.min(100, Math.max(0, Math.round(value)))
}

const emotionText = computed(() => props.emotion?.trim() ?? '')
const affectionScore = computed(() => clampScore(props.affection))
const energyScore = computed(() => clampScore(props.energy))
const affectionLevel = computed(() => Math.ceil((affectionScore.value / 100) * HEART_COUNT))
const energyFillStyle = computed(() => ({ width: `${energyScore.value}%` }))
const rawTitle = computed(() => t('relationshipMood.rawTitle', {
  affection: affectionScore.value,
  energy: energyScore.value,
}))
const affectionLabel = computed(() => t('relationshipMood.affectionAria', {
  level: affectionLevel.value,
  max: HEART_COUNT,
}))
const energyLabel = computed(() => t('relationshipMood.energyAria', {
  percent: energyScore.value,
}))
const emotionLabel = computed(() => t('relationshipMood.emotionAria', {
  emotion: emotionText.value,
}))
</script>

<template>
  <div
    :class="['relationship-mood', `relationship-mood--${variant}`]"
    :title="showMetrics ? rawTitle : undefined"
  >
    <span
      v-if="emotionText"
      class="relationship-mood__emotion"
      :aria-label="emotionLabel"
    >
      {{ emotionText }}
    </span>

    <span
      v-if="showMetrics"
      class="relationship-mood__metric relationship-mood__hearts"
      :aria-label="affectionLabel"
    >
      <span
        v-for="heart in HEART_COUNT"
        :key="heart"
        :class="['relationship-mood__heart', { 'is-filled': heart <= affectionLevel }]"
        aria-hidden="true"
      >&#9829;</span>
    </span>

    <span
      v-if="showMetrics"
      class="relationship-mood__metric relationship-mood__energy"
      :aria-label="energyLabel"
    >
      <span class="relationship-mood__energy-icon" aria-hidden="true">&#9889;</span>
      <span class="relationship-mood__energy-track" aria-hidden="true">
        <span class="relationship-mood__energy-fill" :style="energyFillStyle"></span>
      </span>
    </span>
  </div>
</template>

<style scoped>
.relationship-mood {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: var(--color-text-secondary);
}

.relationship-mood--header {
  gap: 8px;
}

.relationship-mood__emotion {
  min-width: 0;
  max-width: 112px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  line-height: 1.4;
  color: var(--color-text-secondary);
}

.relationship-mood--card .relationship-mood__emotion {
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.06);
}

.relationship-mood--header .relationship-mood__emotion {
  max-width: 140px;
  padding-left: 8px;
  border-left: 1px solid rgba(255, 255, 255, 0.16);
}

.relationship-mood__metric {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
}

.relationship-mood__hearts {
  gap: 1px;
  font-size: 10px;
  letter-spacing: 0;
  color: rgba(255, 255, 255, 0.2);
}

.relationship-mood__heart.is-filled {
  color: #ff7aa2;
  text-shadow: 0 0 8px rgba(255, 122, 162, 0.35);
}

.relationship-mood__energy {
  gap: 4px;
}

.relationship-mood__energy-icon {
  font-size: 10px;
  line-height: 1;
  color: #e9cf78;
  opacity: 0.86;
}

.relationship-mood__energy-track {
  width: 36px;
  height: 4px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.13);
}

.relationship-mood__energy-fill {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #a7d7c5, #e9cf78);
}
</style>
