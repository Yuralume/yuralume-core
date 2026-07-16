<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  listOpenPendingFollowUps,
  triggerPendingFollowUpTick,
  type PendingFollowUp,
} from '@/utils/api/pendingFollowUps'
import { useTimezone } from '@/composables/useTimezone'
import { formatDateTime } from '@/i18n/formatters'
import { UiButton } from '@/components/ui'

const props = defineProps<{
  characterId: string | null
}>()

const { locale, t } = useI18n()
const { timeZone } = useTimezone()

const rows = ref<PendingFollowUp[]>([])
const loading = ref(false)
const errorMsg = ref<string | null>(null)
const tickBusy = ref(false)
const tickMsg = ref<string | null>(null)

async function reload() {
  if (!props.characterId) {
    rows.value = []
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    rows.value = await listOpenPendingFollowUps(props.characterId)
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t('pendingFollowUpsPanel.errors.loadFailed')
    rows.value = []
  } finally {
    loading.value = false
  }
}

async function handleTickNow() {
  if (tickBusy.value) return
  tickBusy.value = true
  tickMsg.value = null
  try {
    const result = await triggerPendingFollowUpTick()
    tickMsg.value = result.resolved > 0
      ? t('pendingFollowUpsPanel.tick.released', { count: result.resolved })
      : t('pendingFollowUpsPanel.tick.none')
    await reload()
  } catch (err) {
    tickMsg.value = err instanceof Error
      ? t('pendingFollowUpsPanel.tick.failedWithReason', { reason: err.message })
      : t('pendingFollowUpsPanel.tick.failed')
  } finally {
    tickBusy.value = false
    // Auto-clear the toast after a few seconds so it doesn't linger.
    setTimeout(() => { tickMsg.value = null }, 5000)
  }
}

function formatRelative(iso: string): string {
  const date = new Date(iso)
  const diffMs = date.getTime() - Date.now()
  const absSec = Math.abs(diffMs) / 1000
  const future = diffMs > 0
  if (absSec < 60) return future
    ? t('pendingFollowUpsPanel.relative.imminent')
    : t('pendingFollowUpsPanel.relative.justNow')
  if (absSec < 3600) {
    const value = Math.round(absSec / 60)
    return future
      ? t('pendingFollowUpsPanel.relative.minutesAhead', { count: value })
      : t('pendingFollowUpsPanel.relative.minutesAgo', { count: value })
  }
  if (absSec < 86400) {
    const value = Math.round(absSec / 3600)
    return future
      ? t('pendingFollowUpsPanel.relative.hoursAhead', { count: value })
      : t('pendingFollowUpsPanel.relative.hoursAgo', { count: value })
  }
  const value = Math.round(absSec / 86400)
  return future
    ? t('pendingFollowUpsPanel.relative.daysAhead', { count: value })
    : t('pendingFollowUpsPanel.relative.daysAgo', { count: value })
}

function formatAbsolute(iso: string): string {
  return formatDateTime(iso, locale.value, timeZone.value)
}

const hasRows = computed(() => rows.value.length > 0)

function statusLabel(status: PendingFollowUp['status']): string {
  return t(`pendingFollowUpsPanel.status.${status}`)
}

watch(() => props.characterId, () => { void reload() }, { immediate: true })

// Light polling so the user sees status flip from queued → resolved
// without a manual refresh.
let pollTimer: ReturnType<typeof setInterval> | null = null
watch(() => props.characterId, (id) => {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  if (id) {
    pollTimer = setInterval(() => { void reload() }, 15000)
  }
}, { immediate: true })
onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})
</script>

<template>
  <section class="pending-followups-panel">
    <header class="panel-header">
      <div>
        <h3 class="section-title">{{ t('pendingFollowUpsPanel.title') }}</h3>
        <p class="panel-hint">
          {{ t('pendingFollowUpsPanel.hint') }}
        </p>
      </div>
      <div class="panel-actions">
        <UiButton
          size="sm"
          :loading="loading"
          :disabled="!characterId"
          @click="reload"
        >{{ t('pendingFollowUpsPanel.actions.refresh') }}</UiButton>
        <UiButton
          size="sm"
          :loading="tickBusy"
          :title="t('pendingFollowUpsPanel.actions.tickTitle')"
          @click="handleTickNow"
        >{{ t('pendingFollowUpsPanel.actions.tickNow') }}</UiButton>
      </div>
    </header>

    <div v-if="tickMsg" class="panel-toast">{{ tickMsg }}</div>
    <div v-if="errorMsg" class="panel-error">{{ errorMsg }}</div>

    <div v-if="!characterId" class="panel-empty">{{ t('pendingFollowUpsPanel.empty.selectCharacter') }}</div>
    <div v-else-if="loading && !hasRows" class="panel-empty">{{ t('common.state.loading') }}</div>
    <div v-else-if="!hasRows" class="panel-empty">
      {{ t('pendingFollowUpsPanel.empty.none') }}<br />
      {{ t('pendingFollowUpsPanel.empty.noneHint') }}
    </div>

    <ul v-else class="row-list">
      <li
        v-for="row in rows"
        :key="row.id"
        :class="['row-card', `status-${row.status}`]"
      >
        <div class="row-head">
          <span :class="['status-pill', `pill-${row.status}`]">
            {{ statusLabel(row.status) }}
          </span>
          <span v-if="row.defer_reason" class="reason-pill">
            {{ row.defer_reason }}
          </span>
          <span class="time-pill" :title="formatAbsolute(row.scheduled_for)">
            {{ t('pendingFollowUpsPanel.scheduledFor', { relative: formatRelative(row.scheduled_for) }) }}
          </span>
        </div>

        <div class="brief">
          <div class="brief-label">{{ t('pendingFollowUpsPanel.briefLabel') }}</div>
          <div class="brief-text">{{ row.brief_reply }}</div>
        </div>

        <div class="queued-messages">
          <div class="queued-label">
            {{ t('pendingFollowUpsPanel.queuedMessages', { count: row.messages.length }) }}
          </div>
          <ul class="msg-list">
            <li v-for="(msg, idx) in row.messages" :key="idx" class="msg-item">
              <span class="msg-bullet">·</span>
              <span class="msg-text">{{ msg.content }}</span>
              <span class="msg-time" :title="formatAbsolute(msg.queued_at)">
                {{ formatRelative(msg.queued_at) }}
              </span>
            </li>
          </ul>
        </div>

        <div v-if="row.last_error" class="row-error">
          {{ t('pendingFollowUpsPanel.lastError', { error: row.last_error }) }}
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.pending-followups-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}

.section-title {
  margin: 0 0 4px 0;
  font-size: 15px;
  font-weight: 600;
}

.panel-hint {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-secondary, #888);
  line-height: 1.5;
  max-width: 360px;
}

.panel-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.panel-toast {
  padding: 8px 12px;
  font-size: 12px;
  background: rgba(64, 158, 255, 0.08);
  color: #2c70b8;
  border-radius: 6px;
}

.panel-error {
  padding: 8px 12px;
  font-size: 12px;
  background: rgba(245, 108, 108, 0.08);
  color: #c0392b;
  border-radius: 6px;
}

.panel-empty {
  padding: 24px 12px;
  text-align: center;
  font-size: 13px;
  color: var(--color-text-secondary, #888);
  line-height: 1.6;
  background: var(--color-bg-secondary, #fafafa);
  border-radius: 8px;
}

.row-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.row-card {
  padding: 12px;
  border: 1px solid var(--color-border, #e5e5e5);
  border-radius: 8px;
  background: var(--color-bg, #fff);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.row-card.status-resolving {
  border-color: rgba(64, 158, 255, 0.4);
  background: rgba(64, 158, 255, 0.04);
}
.row-card.status-resolved {
  opacity: 0.7;
}

.row-head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.status-pill,
.reason-pill,
.time-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  line-height: 1.6;
}

.pill-queued { background: #fff3cd; color: #856404; }
.pill-resolving { background: #d1ecf1; color: #0c5460; }
.pill-resolved { background: #d4edda; color: #155724; }
.pill-cancelled { background: #f8d7da; color: #721c24; }

.reason-pill { background: rgba(0, 0, 0, 0.05); color: #555; }
.time-pill { background: rgba(0, 0, 0, 0.05); color: #555; }

.brief { display: flex; flex-direction: column; gap: 2px; }
.brief-label,
.queued-label {
  font-size: 11px;
  color: var(--color-text-secondary, #888);
}
.brief-text {
  font-size: 13px;
  padding: 6px 10px;
  background: var(--color-bg-secondary, #fafafa);
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-word;
}

.queued-messages {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.msg-list { list-style: none; padding: 0; margin: 0; }
.msg-item {
  display: flex;
  gap: 6px;
  align-items: baseline;
  font-size: 13px;
  line-height: 1.5;
  padding: 2px 0;
}
.msg-bullet { color: var(--color-text-secondary, #888); }
.msg-text {
  flex: 1;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg-time {
  font-size: 11px;
  color: var(--color-text-secondary, #888);
  flex-shrink: 0;
}

.row-error {
  padding: 6px 10px;
  font-size: 11px;
  background: rgba(245, 108, 108, 0.08);
  color: #c0392b;
  border-radius: 4px;
}
</style>
