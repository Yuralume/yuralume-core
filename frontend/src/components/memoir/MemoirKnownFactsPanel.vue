<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  DeleteOutlined,
  PushpinFilled,
  PushpinOutlined,
} from '@ant-design/icons-vue'

import { UiButton } from '@/components/ui'
import type { MemoirEntry } from '@/utils/api/memoir'

const props = defineProps<{
  entries: MemoirEntry[]
  busyKey: string | null
}>()

const emit = defineEmits<{
  pin: [entry: MemoirEntry]
  unpin: [entry: MemoirEntry]
  forget: [entry: MemoirEntry]
}>()

const { t } = useI18n()

const entryKey = (entry: MemoirEntry) => `${entry.kind}:${entry.entry_id}`

const knownFacts = computed(() => (
  props.entries
    .filter(entry => entry.kind === 'memory' || entry.kind === 'milestone')
    .slice()
    .sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.score - a.score)
    .slice(0, 6)
))

const memoryScore = (entry: MemoirEntry) => Math.round(entry.score * 100)
</script>

<template>
  <section class="known-facts" aria-labelledby="memoir-known-facts-title">
    <div class="known-facts__header">
      <div>
        <h3 id="memoir-known-facts-title" class="known-facts__title">
          {{ t('memoir.knownFacts.title') }}
        </h3>
        <p class="known-facts__hint">{{ t('memoir.knownFacts.hint') }}</p>
      </div>
    </div>

    <div v-if="knownFacts.length === 0" class="known-facts__empty">
      {{ t('memoir.knownFacts.empty') }}
    </div>

    <ul v-else class="known-facts__list">
      <li
        v-for="entry in knownFacts"
        :key="entryKey(entry)"
        class="known-facts__item"
      >
        <p class="known-facts__summary">{{ entry.summary }}</p>
        <div class="known-facts__meta">
          <span class="known-facts__score">
            {{ t('memoir.knownFacts.score', { value: memoryScore(entry) }) }}
          </span>
          <span v-if="entry.pinned" class="known-facts__pinned">
            {{ t('memoir.knownFacts.pinned') }}
          </span>
        </div>
        <div class="known-facts__actions">
          <UiButton
            v-if="entry.pinned"
            variant="ghost"
            size="sm"
            :loading="busyKey === entryKey(entry)"
            :title="t('memoir.knownFacts.unpin')"
            :aria-label="t('memoir.knownFacts.unpin')"
            @click="emit('unpin', entry)"
          >
            <PushpinFilled aria-hidden="true" />
          </UiButton>
          <UiButton
            v-else
            variant="ghost"
            size="sm"
            :loading="busyKey === entryKey(entry)"
            :title="t('memoir.knownFacts.pin')"
            :aria-label="t('memoir.knownFacts.pin')"
            @click="emit('pin', entry)"
          >
            <PushpinOutlined aria-hidden="true" />
          </UiButton>
          <UiButton
            variant="ghost"
            size="sm"
            :loading="busyKey === entryKey(entry)"
            :title="t('memoir.knownFacts.forget')"
            :aria-label="t('memoir.knownFacts.forget')"
            @click="emit('forget', entry)"
          >
            <DeleteOutlined aria-hidden="true" />
          </UiButton>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.known-facts {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-width: 0;
  padding: var(--space-4);
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 8px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.045), rgba(255, 255, 255, 0.018)),
    rgba(15, 23, 42, 0.32);
}

.known-facts__header {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
}

.known-facts__title {
  margin: 0;
  font-size: 14px;
  color: var(--color-text);
  letter-spacing: 0;
}

.known-facts__hint {
  margin: 4px 0 0;
  font-size: var(--font-xs);
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.known-facts__empty {
  padding: var(--space-3);
  border: 1px dashed rgba(148, 163, 184, 0.24);
  border-radius: 6px;
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  text-align: center;
}

.known-facts__list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.known-facts__item {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 6px var(--space-2);
  padding: var(--space-3);
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.035);
}

.known-facts__summary {
  grid-column: 1;
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-sm);
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.known-facts__meta {
  grid-column: 1;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.known-facts__score,
.known-facts__pinned {
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(96, 165, 250, 0.12);
}

.known-facts__pinned {
  color: rgba(196, 181, 253, 0.96);
  background: rgba(139, 92, 246, 0.16);
}

.known-facts__actions {
  grid-column: 2;
  grid-row: 1 / span 2;
  display: flex;
  align-items: flex-start;
  gap: 4px;
}

@media (max-width: 720px) {
  .known-facts__item {
    grid-template-columns: 1fr;
  }

  .known-facts__actions {
    grid-column: 1;
    grid-row: auto;
    justify-content: flex-end;
  }
}
</style>
