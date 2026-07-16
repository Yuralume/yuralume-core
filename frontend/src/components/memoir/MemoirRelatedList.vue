<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { useTimezone } from '@/composables/useTimezone'
import { todayISOForTimezone } from '@/i18n/formatters'
import type { MemoirEntry } from '@/utils/api/memoir'

const props = defineProps<{
  entries: MemoirEntry[]
  focusKey: string | null
  limit?: number
}>()

const emit = defineEmits<{
  (event: 'select', entry: MemoirEntry): void
  (event: 'view-all'): void
}>()

const { t } = useI18n()
const { timeZone } = useTimezone()

const showLimit = computed(() => props.limit ?? 6)

const visible = computed(() => {
  const sorted = [...props.entries].sort(
    (a, b) => (b.score ?? 0) - (a.score ?? 0),
  )
  return sorted.slice(0, showLimit.value)
})

const entryKey = (entry: MemoirEntry) => `${entry.kind}:${entry.entry_id}`

const dateLabel = (iso: string) => {
  try {
    const dt = new Date(iso)
    return todayISOForTimezone(timeZone.value, dt).replaceAll('-', '.')
  }
  catch {
    return iso.slice(0, 10)
  }
}

const titleOf = (entry: MemoirEntry, max = 18) => {
  const raw = entry.summary ?? ''
  const m = raw.match(/^(.+?)(?:[。！？]|\n)/)
  const candidate = (m ? m[1] : raw).trim()
  return candidate.length > max ? `${candidate.slice(0, max)}…` : candidate
}

const subtitleOf = (entry: MemoirEntry, max = 32) => {
  const raw = entry.summary ?? ''
  const m = raw.match(/^.+?(?:[。！？]|\n)/)
  const rest = m ? raw.slice(m[0].length).trim() : ''
  if (!rest) return ''
  return rest.length > max ? `${rest.slice(0, max)}…` : rest
}

const kindLabel = (kind: MemoirEntry['kind']) => t(`memoir.kind.${kind}`)
</script>

<template>
  <aside class="related">
    <header class="related__head">
      <h2 class="related__title">
        <span class="related__title-star" aria-hidden="true" />
        {{ t('memoir.related.sectionTitle') }}
      </h2>
      <span class="related__count">{{ entries.length }}</span>
    </header>

    <p v-if="!entries.length" class="related__empty">
      {{ t('memoir.related.empty') }}
    </p>

    <ul v-else class="related__list">
      <li
        v-for="entry in visible"
        :key="entryKey(entry)"
      >
        <button
          type="button"
          class="related__item"
          :class="{
            'related__item--active': entryKey(entry) === focusKey,
            'related__item--pinned': entry.pinned,
          }"
          @click="emit('select', entry)"
        >
          <div class="related__top">
            <span class="related__date">{{ dateLabel(entry.occurred_at) }}</span>
            <span class="related__score">{{ entry.score.toFixed(2) }}</span>
          </div>
          <div class="related__title-line">{{ titleOf(entry) }}</div>
          <div v-if="subtitleOf(entry)" class="related__sub">{{ subtitleOf(entry) }}</div>
          <div class="related__bottom">
            <span class="related__kind">{{ kindLabel(entry.kind) }}</span>
            <span v-if="entry.pinned" class="related__pin" aria-hidden="true" />
          </div>
        </button>
      </li>
    </ul>

    <button
      v-if="entries.length > showLimit"
      type="button"
      class="related__view-all"
      @click="emit('view-all')"
    >
      {{ t('memoir.related.viewAll', { count: entries.length }) }}
      <span aria-hidden="true" class="related__view-all-arrow">→</span>
    </button>
  </aside>
</template>

<style scoped>
.related {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border-radius: 14px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0.01));
  border: 1px solid rgba(139, 92, 246, 0.18);
  overflow-y: auto;
  max-height: 100%;
}
.related__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
  padding-bottom: 6px;
  border-bottom: 1px dashed rgba(139, 92, 246, 0.2);
}
.related__title {
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
.related__title-star {
  display: inline-block;
  width: 14px;
  height: 14px;
  background-image: url('/memoir/star_purple.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  filter: drop-shadow(0 0 4px rgba(196, 181, 253, 0.6));
}
.related__count {
  font-size: 11px;
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}
.related__empty {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
.related__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.related__item {
  width: 100%;
  text-align: left;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.025), rgba(139, 92, 246, 0.03));
  border: 1px solid rgba(139, 92, 246, 0.18);
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--color-text);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, transform 0.15s;
  position: relative;
}
.related__item:hover {
  border-color: rgba(196, 181, 253, 0.55);
  transform: translateY(-1px);
}
.related__item--active {
  border-color: rgba(196, 181, 253, 0.7);
  background:
    linear-gradient(135deg, rgba(139, 92, 246, 0.22), rgba(91, 157, 255, 0.10));
  box-shadow: 0 0 14px rgba(139, 92, 246, 0.2);
}
.related__top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 10px;
  color: var(--color-text-secondary);
  letter-spacing: 0.05em;
}
.related__date {
  font-variant-numeric: tabular-nums;
}
.related__score {
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(139, 92, 246, 0.2);
  border: 1px solid rgba(139, 92, 246, 0.4);
  color: rgba(196, 181, 253, 1);
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  font-size: 11px;
}
.related__title-line {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--color-text);
  font-family: 'Georgia', 'Noto Serif TC', 'Songti TC', serif;
}
.related__sub {
  font-size: 11px;
  line-height: 1.55;
  color: var(--color-text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.related__bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 10px;
  color: var(--color-text-secondary);
  margin-top: 2px;
  padding-top: 4px;
  border-top: 1px dashed rgba(139, 92, 246, 0.15);
}
.related__pin {
  display: inline-block;
  width: 16px;
  height: 16px;
  background-image: url('/memoir/star_gold.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  filter: drop-shadow(0 0 5px rgba(255, 215, 100, 0.7));
}
.related__view-all {
  appearance: none;
  background: transparent;
  border: 1px dashed rgba(139, 92, 246, 0.35);
  border-radius: 10px;
  padding: 8px 10px;
  color: rgba(196, 181, 253, 0.85);
  font-size: 11px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  letter-spacing: 0.04em;
}
.related__view-all:hover {
  color: var(--color-text);
  border-color: rgba(196, 181, 253, 0.6);
  background: rgba(139, 92, 246, 0.08);
}
.related__view-all-arrow {
  font-size: 13px;
}
</style>
