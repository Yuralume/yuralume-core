<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { useTimezone } from '@/composables/useTimezone'
import { todayISOForTimezone } from '@/i18n/formatters'
import type { MemoirEntry } from '@/utils/api/memoir'

const props = defineProps<{
  entries: MemoirEntry[]
  focusKey: string | null
}>()

const emit = defineEmits<{
  (event: 'select', entry: MemoirEntry): void
}>()

const { t } = useI18n()
const { timeZone } = useTimezone()

interface MonthGroup {
  key: string
  label: string
  entries: MemoirEntry[]
}

const groups = computed<MonthGroup[]>(() => {
  const buckets = new Map<string, MemoirEntry[]>()
  for (const entry of props.entries) {
    const dt = new Date(entry.occurred_at)
    const key = todayISOForTimezone(timeZone.value, dt).slice(0, 7)
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key)!.push(entry)
  }
  return [...buckets.entries()]
    .sort((a, b) => (a[0] < b[0] ? 1 : -1))
    .map(([key, entries]) => ({
      key,
      label: key.replace('-', '.'),
      entries,
    }))
})

const entryKey = (entry: MemoirEntry) => `${entry.kind}:${entry.entry_id}`

const dayLabel = (iso: string) => {
  try {
    const dt = new Date(iso)
    return todayISOForTimezone(timeZone.value, dt).slice(5).replace('-', '.')
  }
  catch {
    return iso.slice(0, 10)
  }
}

const titleOf = (entry: MemoirEntry, max = 20) => {
  const raw = entry.summary ?? ''
  // 取首句當小標題，跟焦點書頁的拆法呼應
  const m = raw.match(/^(.+?)(?:[。！？]|\n)/)
  const candidate = (m ? m[1] : raw).trim()
  return candidate.length > max ? `${candidate.slice(0, max)}…` : candidate
}

const kindIcon = (kind: MemoirEntry['kind']) => {
  if (kind === 'milestone') return '✦'
  if (kind === 'emotion') return '◈'
  return '◇'
}
</script>

<template>
  <aside class="month-timeline">
    <header class="month-timeline__head">
      <h2 class="month-timeline__title">
        <span class="month-timeline__title-star" aria-hidden="true" />
        {{ t('memoir.timeline.sectionTitle') }}
      </h2>
      <span class="month-timeline__count">{{ entries.length }}</span>
    </header>
    <p v-if="!entries.length" class="month-timeline__empty">
      {{ t('memoir.timeline.empty') }}
    </p>
    <div v-else class="month-timeline__groups">
      <section
        v-for="group in groups"
        :key="group.key"
        class="month-timeline__group"
      >
        <h3 class="month-timeline__month">
          <span class="month-timeline__month-rule" aria-hidden="true" />
          <span class="month-timeline__month-star" aria-hidden="true" />
          <span class="month-timeline__month-label">{{ group.label }}</span>
          <span class="month-timeline__month-star" aria-hidden="true" />
          <span class="month-timeline__month-rule" aria-hidden="true" />
        </h3>
        <ul class="month-timeline__list">
          <li
            v-for="entry in group.entries"
            :key="entryKey(entry)"
          >
            <button
              type="button"
              class="month-timeline__item"
              :class="{
                'month-timeline__item--active': entryKey(entry) === focusKey,
                'month-timeline__item--pinned': entry.pinned,
              }"
              @click="emit('select', entry)"
            >
              <span class="month-timeline__bullet" aria-hidden="true">
                {{ kindIcon(entry.kind) }}
              </span>
              <span class="month-timeline__lines">
                <span class="month-timeline__day">{{ dayLabel(entry.occurred_at) }}</span>
                <span class="month-timeline__summary">{{ titleOf(entry) }}</span>
              </span>
              <span v-if="entry.pinned" class="month-timeline__pin" aria-hidden="true" />
            </button>
          </li>
        </ul>
      </section>
    </div>
  </aside>
</template>

<style scoped>
.month-timeline {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-2) var(--space-3) var(--space-3);
  border-radius: 14px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0.01));
  border: 1px solid rgba(139, 92, 246, 0.18);
  overflow-y: auto;
  max-height: 100%;
}
.month-timeline__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}
.month-timeline__title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-size: 11px;
  font-weight: 600;
  color: rgba(196, 181, 253, 0.85);
  text-transform: uppercase;
  letter-spacing: 0.12em;
}
.month-timeline__title-star {
  display: inline-block;
  width: 14px;
  height: 14px;
  background-image: url('/memoir/star_purple.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  filter: drop-shadow(0 0 4px rgba(196, 181, 253, 0.6));
}
.month-timeline__count {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--color-text-secondary);
}
.month-timeline__empty {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.month-timeline__groups {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.month-timeline__group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.month-timeline__month {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.05em;
}
.month-timeline__month-label {
  font-size: 10px;
  color: rgba(196, 181, 253, 0.8);
  padding: 1px 6px;
  border-radius: 6px;
  background: rgba(139, 92, 246, 0.12);
}
.month-timeline__month-rule {
  flex: 1;
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(139, 92, 246, 0.25),
    transparent
  );
}
.month-timeline__month-star {
  display: inline-block;
  width: 10px;
  height: 10px;
  background-image: url('/memoir/star_purple.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  opacity: 0.85;
  filter: drop-shadow(0 0 3px rgba(196, 181, 253, 0.5));
}

.month-timeline__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  position: relative;
}
/* 左側時間線 */
.month-timeline__list::before {
  content: '';
  position: absolute;
  top: 6px;
  bottom: 6px;
  left: 9px;
  width: 1px;
  background: linear-gradient(
    180deg,
    rgba(139, 92, 246, 0.0),
    rgba(139, 92, 246, 0.25),
    rgba(139, 92, 246, 0.0)
  );
  pointer-events: none;
}
.month-timeline__item {
  width: 100%;
  display: grid;
  grid-template-columns: 18px 1fr auto;
  align-items: center;
  gap: 8px;
  padding: 6px 8px 6px 0;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  color: var(--color-text);
  cursor: pointer;
  text-align: left;
  font-size: var(--font-xs);
  transition: background 0.15s, border-color 0.15s, transform 0.15s;
  position: relative;
}
.month-timeline__item:hover {
  background: rgba(139, 92, 246, 0.08);
  transform: translateX(1px);
}
.month-timeline__item--active {
  background:
    linear-gradient(90deg, rgba(139, 92, 246, 0.28), rgba(91, 157, 255, 0.08));
  border-color: rgba(196, 181, 253, 0.5);
  box-shadow: 0 0 10px rgba(139, 92, 246, 0.2);
}
.month-timeline__bullet {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  font-size: 11px;
  color: rgba(196, 181, 253, 0.7);
  background: var(--color-bg, #14102a);
  border-radius: 50%;
  position: relative;
  z-index: 1;
}
.month-timeline__item--active .month-timeline__bullet {
  color: rgba(196, 181, 253, 1);
  background: rgba(139, 92, 246, 0.35);
  box-shadow: 0 0 8px rgba(139, 92, 246, 0.5);
}
.month-timeline__lines {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.month-timeline__day {
  font-variant-numeric: tabular-nums;
  color: var(--color-text-secondary);
  font-size: 10px;
  letter-spacing: 0.04em;
}
.month-timeline__item--pinned .month-timeline__day {
  color: rgba(196, 181, 253, 0.95);
}
.month-timeline__summary {
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.month-timeline__pin {
  display: inline-block;
  width: 14px;
  height: 14px;
  background-image: url('/memoir/star_gold.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  filter: drop-shadow(0 0 4px rgba(255, 215, 100, 0.6));
}
</style>
