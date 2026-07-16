<script setup lang="ts">
import axios from 'axios'
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'
import { useTimezone } from '@/composables/useTimezone'
import { formatDateTime } from '@/i18n/formatters'
import type { ProactiveAttempt } from '@/types/proactive'
import {
  evaluateProactiveNow,
  listProactiveAttempts,
} from '@/utils/api/proactive'

const props = defineProps<{ characterId: string | null }>()

const { locale, t } = useI18n()
const { timeZone } = useTimezone()

const open = ref(false)
const loaded = ref(false)
const proactiveAttempts = ref<ProactiveAttempt[]>([])
const loadingAttempts = ref(false)
const attemptsError = ref<string | null>(null)
const evaluating = ref(false)

watch(() => props.characterId, () => {
  proactiveAttempts.value = []
  loaded.value = false
  attemptsError.value = null
  if (open.value) {
    void refreshProactiveAttempts()
  }
})

async function handleToggle(event: Event) {
  open.value = (event.target as HTMLDetailsElement).open
  if (open.value && !loaded.value) {
    await refreshProactiveAttempts()
  }
}

async function refreshProactiveAttempts() {
  if (!props.characterId) return
  loadingAttempts.value = true
  attemptsError.value = null
  try {
    proactiveAttempts.value = await listProactiveAttempts(props.characterId, 20)
    loaded.value = true
  } catch (err) {
    attemptsError.value = readError(err, t('channelBindingsPanel.errors.loadAttemptsFailed'))
  } finally {
    loadingAttempts.value = false
  }
}

async function handleEvaluateNow() {
  if (!props.characterId) return
  evaluating.value = true
  attemptsError.value = null
  try {
    await evaluateProactiveNow(props.characterId)
    await refreshProactiveAttempts()
  } catch (err) {
    attemptsError.value = readError(err, t('channelBindingsPanel.errors.evaluateFailed'))
  } finally {
    evaluating.value = false
  }
}

const outcomeLabelKeys: Record<string, string> = {
  disabled: 'disabled',
  gate_blocked: 'gateBlocked',
  no_binding: 'noBinding',
  decider_skipped: 'deciderSkipped',
  sent: 'sent',
  errored: 'errored',
}

function outcomeLabel(outcome: string): string {
  const key = outcomeLabelKeys[outcome]
  return key ? t(`channelBindingsPanel.outcome.${key}`) : outcome
}

function formatDecidedAt(iso: string): string {
  return formatDateTime(iso, locale.value, timeZone.value)
}

function readError(err: unknown, fallback: string): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return fallback
}
</script>

<template>
  <details class="proactive-log" @toggle="handleToggle">
    <summary class="proactive-log-summary">
      <span class="proactive-log-summary__title">
        {{ t('channelBindingsPanel.attempts.advancedTitle') }}
      </span>
      <span class="proactive-log-summary__hint">
        {{ t('channelBindingsPanel.attempts.collapsedHint') }}
      </span>
    </summary>

    <div class="proactive-log-body">
      <div class="proactive-log-header">
        <h3 class="section-title">{{ t('channelBindingsPanel.attempts.title') }}</h3>
        <div class="proactive-log-actions">
          <UiButton
            size="sm"
            :disabled="loadingAttempts || evaluating"
            @click="refreshProactiveAttempts"
          >{{ t('channelBindingsPanel.actions.refresh') }}</UiButton>
          <UiButton
            variant="primary"
            size="sm"
            :loading="evaluating"
            :disabled="loadingAttempts"
            @click="handleEvaluateNow"
          >{{ t('channelBindingsPanel.actions.evaluateNow') }}</UiButton>
        </div>
      </div>
      <p class="channels-hint">
        {{ t('channelBindingsPanel.attempts.hint') }}
      </p>
      <div v-if="attemptsError" class="channels-error">{{ attemptsError }}</div>
      <div v-if="loadingAttempts" class="channels-empty">{{ t('common.state.loading') }}</div>
      <div
        v-else-if="proactiveAttempts.length === 0"
        class="channels-empty"
      >{{ t('channelBindingsPanel.empty.noAttempts') }}</div>
      <div v-else class="attempts-list">
        <div
          v-for="attempt in proactiveAttempts"
          :key="attempt.id"
          :class="['attempt-row', `outcome-${attempt.outcome}`]"
        >
          <div class="attempt-head">
            <span class="attempt-outcome">
              {{ outcomeLabel(attempt.outcome) }}
            </span>
            <span class="attempt-time">{{ formatDecidedAt(attempt.decided_at) }}</span>
          </div>
          <div class="attempt-meta">
            <span class="attempt-trigger">
              {{ t('channelBindingsPanel.attempts.triggerPrefix') }}{{ attempt.trigger }}
            </span>
            <span v-if="attempt.reason" class="attempt-reason">{{ attempt.reason }}</span>
          </div>
          <div v-if="attempt.message" class="attempt-message">
            {{ t('channelBindingsPanel.attempts.messagePrefix') }}{{ attempt.message }}
          </div>
        </div>
      </div>
    </div>
  </details>
</template>

<style scoped>
.proactive-log {
  margin-top: 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
}

.proactive-log-summary {
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px;
  list-style: none;
}

.proactive-log-summary::-webkit-details-marker {
  display: none;
}

.proactive-log-summary__title {
  font-size: 12px;
  font-weight: 650;
  color: var(--color-text);
}

.proactive-log-summary__hint,
.channels-hint,
.channels-empty {
  font-size: 12px;
  color: var(--text-secondary, #888);
  line-height: 1.6;
}

.proactive-log-body {
  border-top: 1px solid var(--color-border);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.section-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.channels-error {
  background: rgba(255, 77, 79, 0.12);
  border: 1px solid rgba(255, 77, 79, 0.5);
  color: #ff4d4f;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 12px;
}

.proactive-log-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
}

.proactive-log-actions {
  display: flex;
  gap: 6px;
}

.attempts-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 260px;
  overflow-y: auto;
}

.attempt-row {
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.02);
  font-size: 11px;
  line-height: 1.5;
}

.attempt-row.outcome-sent { border-color: rgba(82, 196, 26, 0.5); }
.attempt-row.outcome-gate_blocked { border-color: rgba(128, 128, 128, 0.4); }
.attempt-row.outcome-decider_skipped { border-color: rgba(128, 128, 128, 0.4); }
.attempt-row.outcome-errored { border-color: rgba(255, 77, 79, 0.5); }

.attempt-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
}

.attempt-outcome {
  font-weight: 600;
}

.attempt-time {
  color: var(--color-text-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.attempt-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--color-text-secondary);
}

.attempt-trigger {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.attempt-message {
  margin-top: 4px;
  padding: 4px 6px;
  background: rgba(128, 128, 128, 0.1);
  border-radius: 3px;
  font-size: 12px;
  color: var(--color-text);
}
</style>
