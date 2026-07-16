<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { listRecentWorldEvents, triggerWorldEventIngest } from '../utils/api/worldEvents'
import type { IngestRunResult, WorldEvent } from '../types/worldEvent'
import { UiButton } from './ui'
import { useTimezone } from '@/composables/useTimezone'
import { formatDateTime } from '@/i18n/formatters'

const props = withDefaults(defineProps<{
  modelEnabled: boolean
  modelTopics: string[]
  copyNamespace?: string
  showEnabledToggle?: boolean
  showPreview?: boolean
}>(), {
  copyNamespace: 'worldAwarenessPanel',
  showEnabledToggle: true,
  showPreview: true,
})

const emit = defineEmits<{
  (e: 'update:modelEnabled', v: boolean): void
  (e: 'update:modelTopics', v: string[]): void
}>()

const { locale, t } = useI18n()
const { timeZone } = useTimezone()

const topicDraft = ref('')
const recentEvents = ref<WorldEvent[]>([])
const previewLoading = ref(false)
const previewError = ref<string | null>(null)
const ingesting = ref(false)
const ingestResult = ref<IngestRunResult | null>(null)
const ingestError = ref<string | null>(null)

function toggleEnabled(event: Event) {
  const el = event.target as HTMLInputElement
  emit('update:modelEnabled', el.checked)
}

function addTopic() {
  const trimmed = topicDraft.value.trim()
  if (!trimmed) return
  if (props.modelTopics.includes(trimmed)) {
    topicDraft.value = ''
    return
  }
  emit('update:modelTopics', [...props.modelTopics, trimmed])
  topicDraft.value = ''
}

function removeTopic(topic: string) {
  emit(
    'update:modelTopics',
    props.modelTopics.filter((t) => t !== topic),
  )
}

async function loadPreview() {
  previewLoading.value = true
  previewError.value = null
  try {
    recentEvents.value = await listRecentWorldEvents({ limit: 8, maxAgeDays: 7 })
  } catch (err) {
    previewError.value = err instanceof Error ? err.message : String(err)
  } finally {
    previewLoading.value = false
  }
}

async function runIngest() {
  ingesting.value = true
  ingestError.value = null
  ingestResult.value = null
  try {
    ingestResult.value = await triggerWorldEventIngest()
    await loadPreview()
  } catch (err) {
    ingestError.value = err instanceof Error ? err.message : String(err)
  } finally {
    ingesting.value = false
  }
}

function formatDate(iso: string): string {
  return formatDateTime(iso, locale.value, timeZone.value)
}

onMounted(() => {
  if (props.showPreview) void loadPreview()
})
</script>

<template>
  <div class="world-awareness-panel">
    <p class="field-hint">
      {{ t(`${copyNamespace}.hint`) }}
    </p>

    <label v-if="showEnabledToggle" class="field-label toggle-row">
      <input type="checkbox" :checked="modelEnabled" @change="toggleEnabled" />
      <span>{{ t(`${copyNamespace}.enabled`) }}</span>
    </label>

    <div class="topics-block">
      <label class="field-label">{{ t(`${copyNamespace}.topicFilter`) }}</label>
      <div class="topics-list">
        <span v-for="topic in modelTopics" :key="topic" class="topic-chip">
          {{ topic }}
          <button
            type="button"
            class="chip-remove"
            :aria-label="t(`${copyNamespace}.removeTopic`, { topic })"
            @click="removeTopic(topic)"
          >×</button>
        </span>
        <span v-if="modelTopics.length === 0" class="topic-hint">
          {{ t(`${copyNamespace}.noTopics`) }}
        </span>
      </div>
      <div class="topic-add">
        <input
          v-model="topicDraft"
          type="text"
          class="field-input"
          :placeholder="t(`${copyNamespace}.topicPlaceholder`)"
          @keydown.enter.prevent="addTopic"
        />
        <UiButton type="button" @click="addTopic">{{ t(`${copyNamespace}.addTopic`) }}</UiButton>
      </div>
    </div>

    <div v-if="showPreview" class="preview-block">
      <div class="preview-header">
        <h4 class="preview-title">{{ t(`${copyNamespace}.previewTitle`) }}</h4>
        <div class="preview-actions">
          <UiButton type="button" :loading="previewLoading" @click="loadPreview">
            {{ previewLoading ? t('common.state.loading') : t(`${copyNamespace}.reload`) }}
          </UiButton>
          <UiButton type="button" variant="primary" :loading="ingesting" @click="runIngest">
            {{ ingesting ? t(`${copyNamespace}.ingesting`) : t(`${copyNamespace}.ingestNow`) }}
          </UiButton>
        </div>
      </div>

      <div v-if="ingestResult" class="ingest-result">
        {{ t(`${copyNamespace}.ingestResult`, {
          fetched: ingestResult.fetched,
          new: ingestResult.new,
          embedded: ingestResult.embedded,
          evicted: ingestResult.evicted,
        }) }}
        <div v-if="ingestResult.errors.length" class="ingest-warnings">
          <div v-for="err in ingestResult.errors" :key="err">⚠ {{ err }}</div>
        </div>
      </div>
      <div v-if="ingestError" class="ingest-error">
        {{ t(`${copyNamespace}.ingestFailed`, { reason: ingestError }) }}
      </div>

      <div v-if="previewError" class="ingest-error">{{ previewError }}</div>
      <div v-else-if="recentEvents.length === 0 && !previewLoading" class="preview-empty">
        {{ t(`${copyNamespace}.emptyPreview`) }}
      </div>
      <ul v-else class="event-list">
        <li v-for="event in recentEvents" :key="event.id" class="event-item">
          <div class="event-title">
            <a :href="event.url" target="_blank" rel="noreferrer">{{ event.title }}</a>
            <span v-if="!event.has_embedding" class="event-flag">
              {{ t(`${copyNamespace}.noEmbedding`) }}
            </span>
          </div>
          <div class="event-meta">
            {{ event.source }} · {{ formatDate(event.published_at) }}
            <span v-if="event.topic_tags.length"> · {{ event.topic_tags.join(', ') }}</span>
          </div>
          <div v-if="event.summary" class="event-summary">{{ event.summary }}</div>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.world-awareness-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.toggle-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.topics-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.topics-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 28px;
  align-items: center;
}
.topic-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--color-surface-2, rgba(255, 255, 255, 0.06));
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 13px;
}
.chip-remove {
  background: transparent;
  border: none;
  color: inherit;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
}
.topic-hint {
  color: var(--color-muted, #888);
  font-size: 13px;
}
.topic-add {
  display: flex;
  gap: 6px;
}
/* 共用欄位樣式在 global style.css */
.topic-add .field-input {
  flex: 1;
}
.preview-block {
  border-top: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.preview-title {
  margin: 0;
  font-size: 14px;
}
.preview-actions {
  display: flex;
  gap: 6px;
}
.preview-empty {
  color: var(--color-muted, #888);
  font-size: 13px;
}
.event-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 320px;
  overflow-y: auto;
}
.event-item {
  background: var(--color-surface-2, rgba(255, 255, 255, 0.04));
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 13px;
}
.event-title {
  display: flex;
  gap: 6px;
  align-items: baseline;
  font-weight: 600;
}
.event-title a {
  color: inherit;
  text-decoration: none;
}
.event-title a:hover {
  text-decoration: underline;
}
.event-flag {
  font-size: 11px;
  background: rgba(255, 180, 0, 0.2);
  color: #ffb400;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: normal;
}
.event-meta {
  color: var(--color-muted, #888);
  font-size: 12px;
  margin-top: 2px;
}
.event-summary {
  margin-top: 4px;
  line-height: 1.4;
  color: var(--color-text-secondary, #ccc);
}
.ingest-result {
  font-size: 13px;
  color: var(--color-success, #6dd58c);
}
.ingest-warnings {
  color: var(--color-warning, #f2b94a);
  font-size: 12px;
  margin-top: 4px;
}
.ingest-error {
  color: var(--color-danger, #e07070);
  font-size: 13px;
}
</style>
