<script setup lang="ts">
// Observability dashboard panel — backs the admin endpoints exposed
// by ``/api/v1/admin/observability/*``. Three sub-tabs sharing one
// character filter:
//
//   * Turns: most recent LLM calls + latency histogram (bar chart in
//     pure CSS — no charting library so the dev bundle stays small;
//     swap to chart.js if histograms grow beyond 8-10 buckets).
//   * Proactive funnel: outcome breakdown for the selected character.
//   * Emotion events: 24h emotion log driving the prompt-side summary.
//
// All three lazy-load on tab switch so opening the panel doesn't fire
// three parallel requests when the user only wanted one.
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  type EmotionEventRow,
  type HumanizationFlags,
  type LatencyBucket,
  type LatencyReport,
  type OperatorFeedbackKind,
  type PersonaCuriosityAttempt,
  type PersonaCuriosityFlags,
  type PersonaCuriosityMetrics,
  type ProactiveFunnel,
  type QuietHours,
  type SubsystemHealthMetrics,
  type TurnRecordDetail,
  type TurnRecordSummary,
  type UsageCapability,
  type UsageCharacterBucket,
  type UsageEventRow,
  type UsageFeatureBucket,
  type UsageModelBucket,
  type UsageSummary,
  exportUsageEventsCsv,
  getHumanizationFlags,
  getPersonaCuriosityFlags,
  getQuietHours,
  getTurn,
  latencyHistogram,
  latencyReport,
  listUsageEvents,
  listPersonaCuriosityAttempts,
  listEmotionEvents,
  listTurns,
  personaCuriosityMetrics,
  proactiveFunnel,
  setQuietHours,
  subsystemHealthMetrics,
  usageByCharacter,
  usageByFeature,
  usageByModel,
  usageSummary,
  updateTurnOperatorFeedback,
} from '@/utils/api/observability'
import { UiButton } from '@/components/ui'
import UsageCostCalculator from '@/components/observability/UsageCostCalculator.vue'
// Lazy-loaded so its cost-modeling math + config fetches only ship when the
// operator actually opens the tab.
const CostModelingPanel = defineAsyncComponent(
  () => import('@/components/observability/CostModelingPanel.vue'),
)
import { useAuth } from '@/composables/useAuth'
import { useLocale } from '@/composables/useLocale'
import { formatRelativeTime } from '@/i18n/formatters'

const props = withDefaults(
  defineProps<{
    characterId: string | null
    characters?: { id: string; name: string }[]
  }>(),
  { characters: () => [] },
)

const { t } = useI18n()
const { locale } = useLocale()
const { isAdmin } = useAuth()

type SubTab = 'turns' | 'usage' | 'costModeling' | 'funnel' | 'curiosity' | 'emotions' | 'subsystemHealth' | 'latency' | 'settings'

const subTab = ref<SubTab>('turns')

// turns
const turns = ref<TurnRecordSummary[]>([])
const turnsLoading = ref(false)
const turnsError = ref<string | null>(null)
const turnKindFilter = ref<string>('')
const turnFeedbackFilter = ref<string>('')
const histogram = ref<LatencyBucket[]>([])
const selectedTurn = ref<TurnRecordDetail | null>(null)
const feedbackSaving = ref<OperatorFeedbackKind | null>(null)

// usage ledger
const usageSummaryData = ref<UsageSummary | null>(null)
const usageByFeatureRows = ref<UsageFeatureBucket[]>([])
const usageByModelRows = ref<UsageModelBucket[]>([])
const usageByCharacterRows = ref<UsageCharacterBucket[]>([])
const usageScopeCurrentCharacter = ref(false)
const usageEvents = ref<UsageEventRow[]>([])
const usageLoading = ref(false)
const usageError = ref<string | null>(null)
const usageExporting = ref(false)
const usageCapability = ref<UsageCapability>('')
// Default to the last 30 days so the ledger query is bounded and hits the
// ``(…, created_at)`` indexes — an unbounded scan of a large usage table
// is the main reason the report felt slow. Operators can widen or clear
// the range to query all-time.
function usageDayOffset(days: number): string {
  const day = new Date()
  day.setUTCDate(day.getUTCDate() + days)
  return day.toISOString().slice(0, 10)
}
const usageToDate = ref(usageDayOffset(0))
const usageFromDate = ref(usageDayOffset(-30))
const usageCapabilities: UsageCapability[] = ['', 'llm', 'image', 'video', 'tts']

// funnel
const funnel = ref<ProactiveFunnel | null>(null)
const funnelLoading = ref(false)
const funnelError = ref<string | null>(null)

// persona curiosity
const curiosityAttempts = ref<PersonaCuriosityAttempt[]>([])
const curiosityMetrics = ref<PersonaCuriosityMetrics | null>(null)
const curiosityLoading = ref(false)
const curiosityError = ref<string | null>(null)

// emotions
const emotions = ref<EmotionEventRow[]>([])
const emotionsLoading = ref(false)
const emotionsError = ref<string | null>(null)

// subsystem health
const subsystemHealth = ref<SubsystemHealthMetrics | null>(null)
const subsystemHealthLoading = ref(false)
const subsystemHealthError = ref<string | null>(null)

// latency report (descriptive only, no SLO)
const latency = ref<LatencyReport | null>(null)
const latencyLoading = ref(false)
const latencyError = ref<string | null>(null)
const latencySinceHours = ref<number>(24)

// quiet hours settings
const quietHours = ref<QuietHours | null>(null)
const quietHoursLoading = ref(false)
const quietHoursError = ref<string | null>(null)
const quietHoursDraft = ref<QuietHours>({ start: 2, end: 6 })
const quietHoursSaving = ref(false)

// humanization flags
const humanizationFlags = ref<HumanizationFlags | null>(null)
const humanizationFlagsError = ref<string | null>(null)
const personaCuriosityFlags = ref<PersonaCuriosityFlags | null>(null)
const personaCuriosityFlagsError = ref<string | null>(null)

const histogramMax = computed(() => {
  let max = 0
  for (const b of histogram.value) if (b.count > max) max = b.count
  return Math.max(1, max)
})

const funnelOrdered = computed(() => {
  if (!funnel.value) return []
  const f = funnel.value
  return [
    { label: t('observabilityPanel.funnel.sent'), count: f.sent, kind: 'pass' },
    { label: t('observabilityPanel.funnel.deciderSkipped'), count: f.decider_skipped, kind: 'skip' },
    { label: t('observabilityPanel.funnel.intentionSkipped'), count: f.intention_skipped, kind: 'skip' },
    { label: t('observabilityPanel.funnel.gateBlocked'), count: f.gate_blocked, kind: 'block' },
    { label: t('observabilityPanel.funnel.errored'), count: f.errored, kind: 'error' },
    { label: t('observabilityPanel.funnel.noBinding'), count: f.no_binding, kind: 'block' },
    { label: t('observabilityPanel.funnel.disabled'), count: f.disabled, kind: 'block' },
  ]
})

const funnelMax = computed(() => {
  let max = 0
  for (const row of funnelOrdered.value) if (row.count > max) max = row.count
  return Math.max(1, max)
})

const usageQuery = computed(() => ({
  from: usageFromDate.value ? `${usageFromDate.value}T00:00:00Z` : null,
  to: usageToDate.value ? `${usageToDate.value}T23:59:59Z` : null,
  capability: usageCapability.value,
  characterId: usageScopeCurrentCharacter.value ? props.characterId : null,
}))

const characterNameById = computed(() => {
  const map: Record<string, string> = {}
  for (const character of props.characters) map[character.id] = character.name
  return map
})

function characterLabel(id: string | null): string {
  if (!id) return t('observabilityPanel.usage.byCharacter.unattributed')
  return characterNameById.value[id] ?? id
}

const usageCachedBreakdown = computed(() => {
  const total = usageSummaryData.value?.request_count ?? 0
  const cached = usageSummaryData.value?.cached_count ?? 0
  return {
    cached,
    nonCached: Math.max(0, total - cached),
  }
})

async function loadTurns() {
  turnsLoading.value = true
  turnsError.value = null
  try {
    const [t, h] = await Promise.all([
      listTurns({
        characterId: props.characterId,
        kind: turnKindFilter.value || null,
        feedbackKind: turnFeedbackFilter.value || null,
        limit: 50,
      }),
      latencyHistogram({
        characterId: props.characterId,
        kind: turnKindFilter.value || null,
      }),
    ])
    turns.value = t
    histogram.value = h
  } catch (err) {
    turnsError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    turnsLoading.value = false
  }
}

async function loadFunnel() {
  if (!props.characterId) {
    funnel.value = null
    return
  }
  funnelLoading.value = true
  funnelError.value = null
  try {
    funnel.value = await proactiveFunnel({
      characterId: props.characterId,
      sinceHours: 24,
    })
  } catch (err) {
    funnelError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    funnelLoading.value = false
  }
}

async function loadUsage() {
  usageLoading.value = true
  usageError.value = null
  try {
    const params = usageQuery.value
    const [summary, featureRows, modelRows, characterRows, events] = await Promise.all([
      usageSummary(params),
      usageByFeature(params),
      usageByModel(params),
      usageByCharacter(params),
      listUsageEvents({ ...params, limit: 50 }),
    ])
    usageSummaryData.value = summary
    usageByFeatureRows.value = featureRows
    usageByModelRows.value = modelRows
    usageByCharacterRows.value = characterRows
    usageEvents.value = events
  } catch (err) {
    usageError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    usageLoading.value = false
  }
}

async function loadCuriosity() {
  if (!props.characterId) {
    curiosityAttempts.value = []
    curiosityMetrics.value = null
    return
  }
  curiosityLoading.value = true
  curiosityError.value = null
  try {
    const [attempts, metrics] = await Promise.all([
      listPersonaCuriosityAttempts({
        characterId: props.characterId,
        limit: 50,
      }),
      personaCuriosityMetrics({
        characterId: props.characterId,
        sinceHours: 72,
      }),
    ])
    curiosityAttempts.value = attempts
    curiosityMetrics.value = metrics
  } catch (err) {
    curiosityError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    curiosityLoading.value = false
  }
}

async function loadEmotions() {
  if (!props.characterId) {
    emotions.value = []
    return
  }
  emotionsLoading.value = true
  emotionsError.value = null
  try {
    emotions.value = await listEmotionEvents({
      characterId: props.characterId,
      sinceHours: 24,
      limit: 100,
    })
  } catch (err) {
    emotionsError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    emotionsLoading.value = false
  }
}

async function loadSubsystemHealth() {
  if (!props.characterId) {
    subsystemHealth.value = null
    return
  }
  subsystemHealthLoading.value = true
  subsystemHealthError.value = null
  try {
    subsystemHealth.value = await subsystemHealthMetrics({
      characterId: props.characterId,
      sinceHours: 72,
    })
  } catch (err) {
    subsystemHealthError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    subsystemHealthLoading.value = false
  }
}

async function loadLatency() {
  latencyLoading.value = true
  latencyError.value = null
  try {
    latency.value = await latencyReport({ sinceHours: latencySinceHours.value })
  } catch (err) {
    latencyError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    latencyLoading.value = false
  }
}

async function loadQuietHours() {
  quietHoursLoading.value = true
  quietHoursError.value = null
  try {
    quietHours.value = await getQuietHours()
    quietHoursDraft.value = { ...quietHours.value }
  } catch (err) {
    quietHoursError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    quietHoursLoading.value = false
  }
  humanizationFlagsError.value = null
  try {
    humanizationFlags.value = await getHumanizationFlags()
  } catch (err) {
    humanizationFlagsError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  }
  personaCuriosityFlagsError.value = null
  try {
    personaCuriosityFlags.value = await getPersonaCuriosityFlags()
  } catch (err) {
    personaCuriosityFlagsError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  }
}

async function saveQuietHours() {
  quietHoursSaving.value = true
  quietHoursError.value = null
  try {
    quietHours.value = await setQuietHours(quietHoursDraft.value)
    quietHoursDraft.value = { ...quietHours.value }
  } catch (err) {
    quietHoursError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.saveFailed')
  } finally {
    quietHoursSaving.value = false
  }
}

async function refresh() {
  if (subTab.value === 'turns') await loadTurns()
  else if (subTab.value === 'usage') await loadUsage()
  // costModeling self-loads on mount and has its own reload controls.
  else if (subTab.value === 'costModeling') { /* handled by the panel */ }
  else if (subTab.value === 'funnel') await loadFunnel()
  else if (subTab.value === 'curiosity') await loadCuriosity()
  else if (subTab.value === 'emotions') await loadEmotions()
  else if (subTab.value === 'subsystemHealth') await loadSubsystemHealth()
  else if (subTab.value === 'latency') await loadLatency()
  else if (subTab.value === 'settings') await loadQuietHours()
}

async function exportUsageCsv() {
  usageExporting.value = true
  usageError.value = null
  try {
    const blob = await exportUsageEventsCsv({ ...usageQuery.value, limit: 500 })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `usage-events-${usageFromDate.value || 'from'}-${usageToDate.value || 'to'}.csv`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
  } catch (err) {
    usageError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.exportFailed')
  } finally {
    usageExporting.value = false
  }
}

async function openTurn(id: string) {
  try {
    selectedTurn.value = await getTurn(id)
  } catch (err) {
    selectedTurn.value = null
    turnsError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.detailFailed')
  }
}

async function markSelectedTurnFeedback(kind: OperatorFeedbackKind) {
  if (!selectedTurn.value || feedbackSaving.value) return
  feedbackSaving.value = kind
  try {
    const updated = await updateTurnOperatorFeedback(selectedTurn.value.id, { kind })
    selectedTurn.value = updated
    turns.value = turns.value.map((turn) =>
      turn.id === updated.id
        ? { ...turn, operator_feedback: updated.operator_feedback }
        : turn,
    )
  } catch (err) {
    turnsError.value = err instanceof Error ? err.message : t('observabilityPanel.errors.saveFailed')
  } finally {
    feedbackSaving.value = null
  }
}

function exportSelectedTurnJson() {
  const turn = selectedTurn.value
  if (!turn) return
  const blob = new Blob([JSON.stringify(turn, null, 2)], {
    type: 'application/json',
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `turn-record-${safeFileSegment(turn.id)}.json`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function safeFileSegment(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'turn'
}

function closeDetail() {
  selectedTurn.value = null
}

function formatRelative(iso: string): string {
  return formatRelativeTime(iso, locale.value)
}

function formatUtcInstant(iso: string): string {
  const value = new Date(iso)
  if (Number.isNaN(value.getTime())) return iso
  const formatted = new Intl.DateTimeFormat(locale.value, {
    dateStyle: 'medium',
    timeStyle: 'medium',
    timeZone: 'UTC',
  }).format(value)
  return `${formatted} UTC`
}

function formatTechnicalTime(iso: string): string {
  return t('observabilityPanel.time.relativeWithUtc', {
    relative: formatRelative(iso),
    utc: formatUtcInstant(iso),
  })
}

function formatBucket(b: LatencyBucket): string {
  if (b.upper_ms === null) return `${b.lower_ms}ms+`
  return `${b.lower_ms}–${b.upper_ms}ms`
}

function formatRatio(value: number): string {
  return `${(value * 100).toFixed(0)}%`
}

function formatCost(amount: string | number, currency = 'USD'): string {
  const numeric = Number(amount)
  if (!Number.isFinite(numeric)) return `${currency} ${amount}`
  return new Intl.NumberFormat(locale.value, {
    style: 'currency',
    currency,
    maximumFractionDigits: 6,
  }).format(numeric)
}

function formatQuantity(value: number, unit = ''): string {
  const formatted = new Intl.NumberFormat(locale.value).format(value)
  return unit ? `${formatted} ${unit}` : formatted
}

function describeEmotionCause(kind: string): string {
  const map: Record<string, string> = {
    turn: t('observabilityPanel.emotionCause.turn'),
    idle_drift: t('observabilityPanel.emotionCause.idleDrift'),
    rest_recovery: t('observabilityPanel.emotionCause.restRecovery'),
    proactive_attempt: t('observabilityPanel.emotionCause.proactiveAttempt'),
    world_event: t('observabilityPanel.emotionCause.worldEvent'),
    dream: t('observabilityPanel.emotionCause.dream'),
  }
  return map[kind] ?? kind
}

watch(subTab, () => { refresh() })
watch(() => props.characterId, () => { refresh() })
watch(turnKindFilter, () => {
  if (subTab.value === 'turns') loadTurns()
})
watch(turnFeedbackFilter, () => {
  if (subTab.value === 'turns') loadTurns()
})
watch([usageFromDate, usageToDate, usageCapability, usageScopeCurrentCharacter], () => {
  if (subTab.value === 'usage') loadUsage()
})

// Initial load
refresh()
</script>

<template>
  <div class="observability-panel">
    <div class="subtabs">
      <button
        :class="['subtab', { active: subTab === 'turns' }]"
        @click="subTab = 'turns'"
      >{{ t('observabilityPanel.tabs.turns') }}</button>
      <button
        :class="['subtab', { active: subTab === 'usage' }]"
        @click="subTab = 'usage'"
      >{{ t('observabilityPanel.tabs.usage') }}</button>
      <button
        :class="['subtab', { active: subTab === 'costModeling' }]"
        @click="subTab = 'costModeling'"
      >{{ t('observabilityPanel.tabs.costModeling') }}</button>
      <button
        :class="['subtab', { active: subTab === 'funnel' }]"
        @click="subTab = 'funnel'"
      >{{ t('observabilityPanel.tabs.funnel') }}</button>
      <button
        :class="['subtab', { active: subTab === 'curiosity' }]"
        @click="subTab = 'curiosity'"
      >{{ t('observabilityPanel.tabs.curiosity') }}</button>
      <button
        :class="['subtab', { active: subTab === 'emotions' }]"
        @click="subTab = 'emotions'"
      >{{ t('observabilityPanel.tabs.emotions') }}</button>
      <button
        :class="['subtab', { active: subTab === 'subsystemHealth' }]"
        @click="subTab = 'subsystemHealth'"
      >{{ t('observabilityPanel.tabs.subsystemHealth') }}</button>
      <button
        :class="['subtab', { active: subTab === 'latency' }]"
        @click="subTab = 'latency'"
      >{{ t('observabilityPanel.tabs.latency') }}</button>
      <button
        :class="['subtab', { active: subTab === 'settings' }]"
        @click="subTab = 'settings'"
      >{{ t('observabilityPanel.tabs.settings') }}</button>
      <UiButton
        size="sm"
        class="refresh-btn"
        :loading="turnsLoading || usageLoading || funnelLoading || curiosityLoading || emotionsLoading || subsystemHealthLoading || latencyLoading || quietHoursLoading"
        @click="refresh"
      >{{ t('common.actions.refresh') }}</UiButton>
    </div>

    <!-- Turns -->
    <section v-if="subTab === 'turns'" class="sub-section">
      <p class="technical-time-hint">
        {{ t('observabilityPanel.time.technicalUtcHint') }}
      </p>
      <div class="filter-row">
        <label class="field-label">{{ t('observabilityPanel.filters.kind') }}</label>
        <select v-model="turnKindFilter" class="field-select">
          <option value="">{{ t('observabilityPanel.filters.all') }}</option>
          <option value="chat">chat</option>
          <option value="proactive">proactive</option>
          <option value="post_turn_processor">post_turn_processor</option>
        </select>
        <label class="field-label">{{ t('observabilityPanel.filters.feedback') }}</label>
        <select v-model="turnFeedbackFilter" class="field-select">
          <option value="">{{ t('observabilityPanel.filters.all') }}</option>
          <option value="out_of_character">{{ t('observabilityPanel.feedback.outOfCharacter') }}</option>
          <option value="felt_human">{{ t('observabilityPanel.feedback.feltHuman') }}</option>
        </select>
      </div>

      <p v-if="turnsError" class="error">{{ turnsError }}</p>

      <div v-if="histogram.length > 0" class="histogram">
        <h4>{{ t('observabilityPanel.turns.latencyHistogram') }}</h4>
        <div class="bars">
          <div
            v-for="b in histogram"
            :key="`${b.lower_ms}-${b.upper_ms ?? 'inf'}`"
            class="bar-row"
          >
            <span class="bar-label">{{ formatBucket(b) }}</span>
            <div class="bar-track">
              <div
                class="bar-fill"
                :style="{ width: `${(b.count / histogramMax) * 100}%` }"
              ></div>
            </div>
            <span class="bar-count">{{ b.count }}</span>
          </div>
        </div>
      </div>

      <div v-if="turnsLoading" class="loading">{{ t('common.state.loading') }}</div>
      <ul v-else class="turn-list">
        <li
          v-for="turn in turns"
          :key="turn.id"
          class="turn-row"
          @click="openTurn(turn.id)"
        >
          <div class="turn-row-head">
            <span class="turn-kind">{{ turn.kind }}</span>
            <span class="turn-when">{{ formatTechnicalTime(turn.created_at) }}</span>
            <span v-if="turn.latency_ms !== null" class="turn-lat">
              {{ turn.latency_ms }}ms
            </span>
            <span v-if="turn.completion_tokens !== null" class="turn-tok">
              ~{{ turn.completion_tokens }} tok
            </span>
            <span v-if="turn.operator_feedback?.kind" class="turn-feedback">
              {{ turn.operator_feedback.kind }}
            </span>
          </div>
          <div class="turn-excerpt">
            {{ turn.response_excerpt || t('observabilityPanel.fallback.noResponse') }}
          </div>
          <div v-if="turn.error" class="turn-error">⚠ {{ turn.error }}</div>
        </li>
        <li v-if="!turnsLoading && turns.length === 0" class="empty">
          {{ t('observabilityPanel.empty.noTurns') }}
        </li>
      </ul>
    </section>

    <!-- Usage ledger -->
    <section v-if="subTab === 'usage'" class="sub-section">
      <p class="sub-hint">
        {{ t('observabilityPanel.usage.hint') }}
      </p>
      <div class="filter-row usage-filter-row">
        <label class="field-label">{{ t('observabilityPanel.usage.from') }}</label>
        <input v-model="usageFromDate" type="date" class="field-input usage-date-input" />
        <label class="field-label">{{ t('observabilityPanel.usage.to') }}</label>
        <input v-model="usageToDate" type="date" class="field-input usage-date-input" />
        <div class="usage-capabilities" role="group" :aria-label="t('observabilityPanel.usage.capability')">
          <button
            v-for="cap in usageCapabilities"
            :key="cap || 'all'"
            type="button"
            :class="['usage-capability', { active: usageCapability === cap }]"
            @click="usageCapability = cap"
          >
            {{ cap || t('observabilityPanel.filters.all') }}
          </button>
        </div>
        <label
          class="usage-scope-toggle"
          :class="{ 'is-disabled': !characterId }"
          :title="!characterId ? t('observabilityPanel.usage.scopeNeedsCharacter') : ''"
        >
          <input
            v-model="usageScopeCurrentCharacter"
            type="checkbox"
            :disabled="!characterId"
          />
          {{ t('observabilityPanel.usage.scopeCurrentCharacter') }}
        </label>
        <UiButton
          size="sm"
          :loading="usageExporting"
          @click="exportUsageCsv"
        >{{ t('observabilityPanel.usage.exportCsv') }}</UiButton>
      </div>

      <p v-if="usageError" class="error">{{ usageError }}</p>

      <div v-if="usageSummaryData" class="usage-summary-grid">
        <div class="usage-summary-card">
          <span class="metric-label">{{ t('observabilityPanel.usage.requests') }}</span>
          <strong>{{ formatQuantity(usageSummaryData.request_count) }}</strong>
          <small>{{ usageSummaryData.succeeded_count }} ok / {{ usageSummaryData.failed_count }} failed</small>
        </div>
        <div class="usage-summary-card">
          <span class="metric-label">{{ t('observabilityPanel.usage.billable') }}</span>
          <strong>{{ formatQuantity(usageSummaryData.total_billable_quantity) }}</strong>
          <small>{{ t('observabilityPanel.usage.inputOutput', {
            input: usageSummaryData.total_input_quantity,
            output: usageSummaryData.total_output_quantity,
          }) }}</small>
        </div>
        <div class="usage-summary-card">
          <span class="metric-label">{{ t('observabilityPanel.usage.cost') }}</span>
          <strong>{{ formatCost(usageSummaryData.total_cost_amount, usageSummaryData.cost_currency) }}</strong>
          <small>{{ usageSummaryData.estimated_cost_count }} {{ t('observabilityPanel.usage.estimated') }}</small>
        </div>
        <div class="usage-summary-card">
          <span class="metric-label">{{ t('observabilityPanel.usage.cache') }}</span>
          <strong>{{ usageCachedBreakdown.cached }} / {{ usageCachedBreakdown.nonCached }}</strong>
          <small>{{ t('observabilityPanel.usage.cachedNonCached') }}</small>
        </div>
      </div>

      <div class="usage-table-grid">
        <section class="usage-table-section">
          <h4>{{ t('observabilityPanel.usage.byFeature') }}</h4>
          <table class="usage-table">
            <thead>
              <tr>
                <th>{{ t('observabilityPanel.usage.feature') }}</th>
                <th>{{ t('observabilityPanel.usage.capability') }}</th>
                <th class="num">N</th>
                <th class="num">{{ t('observabilityPanel.usage.billable') }}</th>
                <th class="num">{{ t('observabilityPanel.usage.cost') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in usageByFeatureRows" :key="`${row.capability}:${row.feature_key}`">
                <td>{{ row.feature_key || '-' }}</td>
                <td>{{ row.capability }}</td>
                <td class="num">{{ row.request_count }}</td>
                <td class="num">{{ formatQuantity(row.total_billable_quantity) }}</td>
                <td class="num">{{ formatCost(row.total_cost_amount, usageSummaryData?.cost_currency ?? 'USD') }}</td>
              </tr>
              <tr v-if="!usageLoading && usageByFeatureRows.length === 0">
                <td colspan="5" class="empty">{{ t('observabilityPanel.usage.noUsage') }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section class="usage-table-section">
          <h4>{{ t('observabilityPanel.usage.byProviderModel') }}</h4>
          <table class="usage-table">
            <thead>
              <tr>
                <th>{{ t('observabilityPanel.usage.provider') }}</th>
                <th>{{ t('observabilityPanel.usage.model') }}</th>
                <th>{{ t('observabilityPanel.usage.capability') }}</th>
                <th class="num">N</th>
                <th class="num">{{ t('observabilityPanel.usage.cost') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in usageByModelRows" :key="`${row.capability}:${row.provider_id}:${row.model_id}`">
                <td>{{ row.provider_id || '-' }}</td>
                <td>{{ row.model_id || '-' }}</td>
                <td>{{ row.capability }}</td>
                <td class="num">{{ row.request_count }}</td>
                <td class="num">{{ formatCost(row.total_cost_amount, usageSummaryData?.cost_currency ?? 'USD') }}</td>
              </tr>
              <tr v-if="!usageLoading && usageByModelRows.length === 0">
                <td colspan="5" class="empty">{{ t('observabilityPanel.usage.noUsage') }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>

      <section class="usage-table-section">
        <h4>{{ t('observabilityPanel.usage.byCharacter.title') }}</h4>
        <table class="usage-table">
          <thead>
            <tr>
              <th>{{ t('observabilityPanel.usage.byCharacter.character') }}</th>
              <th class="num">N</th>
              <th class="num">{{ t('observabilityPanel.usage.priceCalc.inputTokens') }}</th>
              <th class="num">{{ t('observabilityPanel.usage.priceCalc.outputTokens') }}</th>
              <th class="num">{{ t('observabilityPanel.usage.cost') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in usageByCharacterRows"
              :key="row.character_id ?? '__unattributed__'"
            >
              <td>{{ characterLabel(row.character_id) }}</td>
              <td class="num">{{ row.request_count }}</td>
              <td class="num">{{ formatQuantity(row.total_input_quantity) }}</td>
              <td class="num">{{ formatQuantity(row.total_output_quantity) }}</td>
              <td class="num">{{ formatCost(row.total_cost_amount, usageSummaryData?.cost_currency ?? 'USD') }}</td>
            </tr>
            <tr v-if="!usageLoading && usageByCharacterRows.length === 0">
              <td colspan="5" class="empty">{{ t('observabilityPanel.usage.noUsage') }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <UsageCostCalculator
        :buckets="usageByModelRows"
        :currency="usageSummaryData?.cost_currency ?? 'USD'"
      />

      <section class="usage-table-section">
        <h4>{{ t('observabilityPanel.usage.recentEvents') }}</h4>
        <table class="usage-table usage-events-table">
          <thead>
            <tr>
              <th>{{ t('observabilityPanel.curiosity.columns.when') }}</th>
              <th>{{ t('observabilityPanel.usage.feature') }}</th>
              <th>{{ t('observabilityPanel.usage.provider') }}</th>
              <th>{{ t('observabilityPanel.usage.request') }}</th>
              <th class="num">{{ t('observabilityPanel.usage.billable') }}</th>
              <th class="num">{{ t('observabilityPanel.usage.cost') }}</th>
              <th>{{ t('observabilityPanel.usage.flags') }}</th>
              <th>{{ t('observabilityPanel.usage.turn') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="event in usageEvents" :key="event.id">
              <td>{{ formatTechnicalTime(event.created_at) }}</td>
              <td>
                <span class="usage-feature">{{ event.capability }}</span>
                {{ event.feature_key || '-' }}
              </td>
              <td>{{ event.provider_id || event.profile_id || event.voice_id || '-' }}</td>
              <td class="usage-request-ids">
                <code>{{ event.request_id.slice(0, 12) }}</code>
                <small v-if="event.upstream_request_id">
                  {{ t('observabilityPanel.usage.upstream') }} {{ event.upstream_request_id.slice(0, 12) }}
                </small>
              </td>
              <td class="num">{{ formatQuantity(event.billable_quantity, event.usage_unit) }}</td>
              <td class="num">{{ formatCost(event.cost_amount, event.cost_currency) }}</td>
              <td>
                <span :class="['usage-badge', event.cached ? 'cached' : 'live']">
                  {{ event.cached ? t('observabilityPanel.usage.cached') : t('observabilityPanel.usage.live') }}
                </span>
                <span :class="['usage-badge', event.usage_is_estimated ? 'estimated' : 'actual']">
                  {{ event.usage_is_estimated ? t('observabilityPanel.usage.estimated') : t('observabilityPanel.usage.actual') }}
                </span>
                <span :class="['usage-badge', event.cost_is_estimated ? 'estimated' : 'actual']">
                  {{ event.cost_is_estimated ? t('observabilityPanel.usage.estimatedCost') : t('observabilityPanel.usage.actualCost') }}
                </span>
              </td>
              <td>
                <button
                  v-if="event.turn_record_id && isAdmin"
                  type="button"
                  class="turn-link-button"
                  @click="openTurn(event.turn_record_id)"
                >
                  {{ event.turn_record_id.slice(0, 8) }}
                </button>
                <span v-else>-</span>
              </td>
            </tr>
            <tr v-if="!usageLoading && usageEvents.length === 0">
              <td colspan="8" class="empty">{{ t('observabilityPanel.usage.noUsage') }}</td>
            </tr>
          </tbody>
        </table>
      </section>
      <div v-if="usageLoading" class="loading">{{ t('common.state.loading') }}</div>
    </section>

    <!-- Cost modeling (LLM routing what-if) -->
    <CostModelingPanel
      v-if="subTab === 'costModeling'"
      class="sub-section"
      :characters="characters"
    />

    <!-- Funnel -->
    <section v-if="subTab === 'funnel'" class="sub-section">
      <p v-if="!characterId" class="empty">
        {{ t('observabilityPanel.empty.selectCharacterForFunnel') }}
      </p>
      <p v-else-if="funnelError" class="error">{{ funnelError }}</p>
      <div v-else-if="funnel" class="funnel">
        <div class="funnel-summary">
          {{ t('observabilityPanel.funnel.summary24h', { total: funnel.total }) }}
        </div>
        <div class="bars">
          <div
            v-for="row in funnelOrdered"
            :key="row.label"
            class="bar-row"
          >
            <span class="bar-label">{{ row.label }}</span>
            <div class="bar-track">
              <div
                class="bar-fill"
                :class="`fill-${row.kind}`"
                :style="{ width: `${(row.count / funnelMax) * 100}%` }"
              ></div>
            </div>
            <span class="bar-count">{{ row.count }}</span>
          </div>
        </div>
      </div>
      <div v-else-if="funnelLoading" class="loading">{{ t('common.state.loading') }}</div>
    </section>

    <!-- Persona curiosity -->
    <section v-if="subTab === 'curiosity'" class="sub-section">
      <p class="sub-hint">
        {{ t('observabilityPanel.curiosity.hint') }}
      </p>
      <p v-if="!characterId" class="empty">
        {{ t('observabilityPanel.empty.selectCharacter') }}
      </p>
      <p v-else-if="curiosityError" class="error">{{ curiosityError }}</p>
      <div v-else>
        <div v-if="curiosityMetrics" class="curiosity-metrics">
          <div class="curiosity-card">
            <span class="metric-label">{{ t('observabilityPanel.curiosity.planCount') }}</span>
            <strong>{{ curiosityMetrics.plan_count }}</strong>
            <small>{{ t('observabilityPanel.curiosity.askNoAsk', {
              ask: curiosityMetrics.ask_plan_count,
              noAsk: curiosityMetrics.no_ask_plan_count,
            }) }}</small>
          </div>
          <div class="curiosity-card">
            <span class="metric-label">{{ t('observabilityPanel.curiosity.askedCount') }}</span>
            <strong>{{ curiosityMetrics.asked_count }}</strong>
            <small>{{ t('observabilityPanel.curiosity.windowHours', { hours: curiosityMetrics.window_hours }) }}</small>
          </div>
          <div class="curiosity-card">
            <span class="metric-label">{{ t('observabilityPanel.curiosity.answerRatio') }}</span>
            <strong>{{ formatRatio(curiosityMetrics.answered_ratio) }}</strong>
            <small>
              {{ t('observabilityPanel.curiosity.terminalBreakdown', {
                deflected: formatRatio(curiosityMetrics.deflected_ratio),
                ignored: formatRatio(curiosityMetrics.ignored_ratio),
              }) }}
            </small>
          </div>
          <div class="curiosity-card">
            <span class="metric-label">{{ t('observabilityPanel.curiosity.guardIncidents') }}</span>
            <strong>{{ curiosityMetrics.repeated_question_guard_incidents }}</strong>
            <small>{{ t('observabilityPanel.curiosity.candidateFacts', {
              count: curiosityMetrics.persona_candidate_facts_after_curiosity,
            }) }}</small>
          </div>
        </div>

        <table v-if="curiosityAttempts.length > 0" class="curiosity-table">
          <thead>
            <tr>
              <th>{{ t('observabilityPanel.curiosity.columns.when') }}</th>
              <th>{{ t('observabilityPanel.curiosity.columns.surface') }}</th>
              <th>{{ t('observabilityPanel.curiosity.columns.status') }}</th>
              <th>{{ t('observabilityPanel.curiosity.columns.target') }}</th>
              <th>{{ t('observabilityPanel.curiosity.columns.intent') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="attempt in curiosityAttempts" :key="attempt.id">
              <td>{{ formatTechnicalTime(attempt.created_at) }}</td>
              <td>{{ attempt.surface }}</td>
              <td>
                <span :class="['curiosity-status', `status-${attempt.status}`]">
                  {{ attempt.status }}
                </span>
              </td>
              <td>Layer {{ attempt.target_layer }} · {{ attempt.target_topic }}</td>
              <td>{{ attempt.question_intent }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else-if="!curiosityLoading && characterId" class="empty">
          {{ t('observabilityPanel.empty.noCuriosityAttempts') }}
        </p>
      </div>
      <div v-if="curiosityLoading" class="loading">{{ t('common.state.loading') }}</div>
    </section>

    <!-- Emotion events -->
    <section v-if="subTab === 'emotions'" class="sub-section">
      <p class="technical-time-hint">
        {{ t('observabilityPanel.time.technicalUtcHint') }}
      </p>
      <p v-if="!characterId" class="empty">
        {{ t('observabilityPanel.empty.selectCharacterForEmotions') }}
      </p>
      <p v-else-if="emotionsError" class="error">{{ emotionsError }}</p>
      <ul v-else class="emotion-list">
        <li
          v-for="evt in emotions"
          :key="evt.id"
          class="emotion-row"
        >
          <div class="emotion-row-head">
            <span class="emotion-when">{{ formatTechnicalTime(evt.created_at) }}</span>
            <span class="emotion-cause">{{ describeEmotionCause(evt.cause_ref_kind) }}</span>
            <span class="emotion-label">
              {{ evt.emotion_label || t('observabilityPanel.fallback.unnamed') }}
            </span>
            <span class="emotion-intensity">
              {{ t('observabilityPanel.emotions.intensity', { value: (evt.intensity * 100).toFixed(0) }) }}
            </span>
          </div>
          <div v-if="evt.evidence_quote" class="emotion-quote">
            {{ t('common.quoted', { text: evt.evidence_quote }) }}
          </div>
          <div class="emotion-deltas">
            <span v-if="evt.affection_delta !== 0">
              {{ t('observabilityPanel.emotions.affection', { value: `${evt.affection_delta > 0 ? '+' : ''}${evt.affection_delta}` }) }}
            </span>
            <span v-if="evt.fatigue_delta !== 0">
              {{ t('observabilityPanel.emotions.fatigue', { value: `${evt.fatigue_delta > 0 ? '+' : ''}${evt.fatigue_delta}` }) }}
            </span>
            <span v-if="evt.trust_delta !== 0">
              {{ t('observabilityPanel.emotions.trust', { value: `${evt.trust_delta > 0 ? '+' : ''}${evt.trust_delta}` }) }}
            </span>
            <span v-if="evt.energy_delta !== 0">
              {{ t('observabilityPanel.emotions.energy', { value: `${evt.energy_delta > 0 ? '+' : ''}${evt.energy_delta}` }) }}
            </span>
          </div>
        </li>
        <li v-if="!emotionsLoading && emotions.length === 0" class="empty">
          {{ t('observabilityPanel.empty.noEmotions24h') }}
        </li>
      </ul>
      <div v-if="emotionsLoading" class="loading">{{ t('common.state.loading') }}</div>
    </section>

    <!-- Subsystem health metrics -->
    <section v-if="subTab === 'subsystemHealth'" class="sub-section">
      <p v-if="!props.characterId" class="empty">
        {{ t('observabilityPanel.empty.selectCharacter') }}
      </p>
      <p v-else-if="subsystemHealthError" class="error">{{ subsystemHealthError }}</p>
      <div v-else-if="subsystemHealth" class="subsystem-health-grid">
        <div class="subsystem-health-card">
          <h4>{{ t('observabilityPanel.subsystemHealth.emotionCausalityTitle') }}</h4>
          <p class="subsystem-health-ratio">{{ (subsystemHealth.emotion_causality_ratio * 100).toFixed(0) }}%</p>
          <p class="subsystem-health-hint">
            {{ t('observabilityPanel.subsystemHealth.emotionCausalityHint', { hours: subsystemHealth.window_hours }) }}
          </p>
          <ul v-if="Object.keys(subsystemHealth.emotion_causality_by_kind).length > 0" class="subsystem-health-by-kind">
            <li v-for="(count, kind) in subsystemHealth.emotion_causality_by_kind" :key="kind">
              {{ t('common.labelWithValue', { label: describeEmotionCause(String(kind)), value: count }) }}
            </li>
          </ul>
        </div>

        <div class="subsystem-health-card">
          <h4>{{ t('observabilityPanel.subsystemHealth.proactiveRhythmTitle') }}</h4>
          <p class="subsystem-health-ratio">
            {{ t('observabilityPanel.subsystemHealth.sentRatio', { value: (subsystemHealth.proactive_send_ratio * 100).toFixed(0) }) }}
          </p>
          <p class="subsystem-health-hint">
            {{ t('observabilityPanel.subsystemHealth.proactiveRhythmHint') }}
          </p>
          <ul class="subsystem-health-by-kind">
            <li>
              {{ t('observabilityPanel.subsystemHealth.intentionSkipped', { value: (subsystemHealth.proactive_intention_skipped_ratio * 100).toFixed(0) }) }}
            </li>
            <li>
              {{ t('observabilityPanel.subsystemHealth.gateBlocked', { value: (subsystemHealth.proactive_gate_blocked_ratio * 100).toFixed(0) }) }}
            </li>
          </ul>
        </div>

        <div class="subsystem-health-card">
          <h4>{{ t('observabilityPanel.subsystemHealth.emotionFollowupTitle') }}</h4>
          <p class="subsystem-health-ratio">{{ (subsystemHealth.emotion_followup_ratio * 100).toFixed(0) }}%</p>
          <p class="subsystem-health-hint">
            {{ t('observabilityPanel.subsystemHealth.emotionFollowupHint', { hours: subsystemHealth.emotion_followup_window_hours }) }}
          </p>
          <ul class="subsystem-health-by-kind">
            <li>
              {{ t('observabilityPanel.subsystemHealth.highIntensityTotal', { count: subsystemHealth.emotion_high_intensity_total }) }}
            </li>
            <li>
              {{ t('observabilityPanel.subsystemHealth.followupCount', { count: subsystemHealth.emotion_followup_count }) }}
            </li>
          </ul>
        </div>

      </div>
      <div v-if="subsystemHealthLoading" class="loading">{{ t('common.state.loading') }}</div>
    </section>

    <!-- Latency report -->
    <section v-if="subTab === 'latency'" class="sub-section">
      <p class="sub-hint">
        {{ t('observabilityPanel.latency.hint') }}
      </p>
      <div class="filter-row">
        <label class="field-label">{{ t('observabilityPanel.latency.windowLabel') }}</label>
        <select v-model.number="latencySinceHours" class="field-select" @change="loadLatency">
          <option :value="6">{{ t('observabilityPanel.latency.window6h') }}</option>
          <option :value="24">{{ t('observabilityPanel.latency.window24h') }}</option>
          <option :value="72">{{ t('observabilityPanel.latency.window3d') }}</option>
          <option :value="168">{{ t('observabilityPanel.latency.window7d') }}</option>
        </select>
      </div>
      <p v-if="latencyError" class="error">{{ latencyError }}</p>
      <p v-else-if="latency && latency.overall_count === 0" class="empty">
        {{ t('observabilityPanel.empty.noLatency', { hours: latency.window_hours }) }}
      </p>
      <table v-else-if="latency && latency.per_kind.length > 0" class="latency-table">
        <thead>
          <tr>
            <th>{{ t('observabilityPanel.latency.kindColumn') }}</th>
            <th class="num">N</th>
            <th class="num">p50</th>
            <th class="num">p90</th>
            <th class="num">p95</th>
            <th class="num">p99</th>
            <th class="num">max</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in latency.per_kind" :key="row.kind">
            <td>{{ row.kind }}</td>
            <td class="num">{{ row.count }}</td>
            <td class="num">{{ row.p50_ms ?? '—' }}</td>
            <td class="num">{{ row.p90_ms ?? '—' }}</td>
            <td class="num">{{ row.p95_ms ?? '—' }}</td>
            <td class="num">{{ row.p99_ms ?? '—' }}</td>
            <td class="num">{{ row.max_ms ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="latencyLoading" class="loading">{{ t('common.state.loading') }}</div>
    </section>

    <!-- Settings: quiet hours -->
    <section v-if="subTab === 'settings'" class="sub-section">
      <p class="sub-hint">
        {{ t('observabilityPanel.settings.quietHoursHint') }}
      </p>
      <p v-if="quietHoursError" class="error">{{ quietHoursError }}</p>
      <div class="settings-row">
        <div class="setting-field">
          <label class="field-label">{{ t('observabilityPanel.settings.startHour') }}</label>
          <input
            v-model.number="quietHoursDraft.start"
            type="number"
            min="0"
            max="23"
            class="field-input"
          />
        </div>
        <div class="setting-field">
          <label class="field-label">{{ t('observabilityPanel.settings.endHour') }}</label>
          <input
            v-model.number="quietHoursDraft.end"
            type="number"
            min="0"
            max="23"
            class="field-input"
          />
        </div>
        <UiButton
          variant="primary"
          :loading="quietHoursSaving"
          @click="saveQuietHours"
        >{{ t('common.actions.save') }}</UiButton>
      </div>
      <p v-if="quietHours" class="settings-current">
        {{ t('observabilityPanel.settings.currentRange', {
          start: String(quietHours.start).padStart(2, '0'),
          end: String(quietHours.end).padStart(2, '0'),
        }) }}
        <span v-if="quietHours.start > quietHours.end" class="settings-note">
          {{ t('observabilityPanel.settings.crossMidnight') }}
        </span>
      </p>
      <div v-if="quietHoursLoading" class="loading">{{ t('common.state.loading') }}</div>

      <hr class="settings-divider" />

      <h4 class="settings-section-title">{{ t('observabilityPanel.settings.flagsTitle') }}</h4>
      <p class="sub-hint">
        {{ t('observabilityPanel.settings.flagsHintPrefix') }} <code>.env</code>
        {{ t('observabilityPanel.settings.flagsHintMiddle') }}
        <code>{{ humanizationFlags?.env_prefix ?? 'KOKORO_HUMANIZATION_' }}*_ENABLED</code>
        {{ t('observabilityPanel.settings.flagsHintSuffix') }}
      </p>
      <p v-if="humanizationFlagsError" class="error">{{ humanizationFlagsError }}</p>
      <ul v-if="humanizationFlags" class="flag-list">
        <li class="flag-row">
          <span class="flag-name">relationship_milestone</span>
          <span :class="['flag-state', humanizationFlags.relationship_milestone_enabled ? 'on' : 'off']">
            {{ humanizationFlags.relationship_milestone_enabled ? 'on' : 'off' }}
          </span>
        </li>
        <li class="flag-row">
          <span class="flag-name">disposition_drift</span>
          <span :class="['flag-state', humanizationFlags.disposition_drift_enabled ? 'on' : 'off']">
            {{ humanizationFlags.disposition_drift_enabled ? 'on' : 'off' }}
          </span>
        </li>
        <li class="flag-row">
          <span class="flag-name">self_reflection</span>
          <span :class="['flag-state', humanizationFlags.self_reflection_enabled ? 'on' : 'off']">
            {{ humanizationFlags.self_reflection_enabled ? 'on' : 'off' }}
          </span>
        </li>
        <li class="flag-row">
          <span class="flag-name">behavioral_pattern</span>
          <span :class="['flag-state', humanizationFlags.behavioral_pattern_enabled ? 'on' : 'off']">
            {{ humanizationFlags.behavioral_pattern_enabled ? 'on' : 'off' }}
          </span>
        </li>
        <li class="flag-row">
          <span class="flag-name">deferred_intent</span>
          <span :class="['flag-state', humanizationFlags.deferred_intent_enabled ? 'on' : 'off']">
            {{ humanizationFlags.deferred_intent_enabled ? 'on' : 'off' }}
          </span>
        </li>
        <li class="flag-row">
          <span class="flag-name">body_state</span>
          <span :class="['flag-state', humanizationFlags.body_state_enabled ? 'on' : 'off']">
            {{ humanizationFlags.body_state_enabled ? 'on' : 'off' }}
          </span>
        </li>
        <li class="flag-row">
          <span class="flag-name">subjective_time</span>
          <span :class="['flag-state', humanizationFlags.subjective_time_enabled ? 'on' : 'off']">
            {{ humanizationFlags.subjective_time_enabled ? 'on' : 'off' }}
          </span>
        </li>
        <li class="flag-row">
          <span class="flag-name">address_preference</span>
          <span :class="['flag-state', humanizationFlags.address_preference_enabled ? 'on' : 'off']">
            {{ humanizationFlags.address_preference_enabled ? 'on' : 'off' }}
          </span>
        </li>
        <li class="flag-row">
          <span class="flag-name">route_b</span>
          <span :class="['flag-state', humanizationFlags.route_b_enabled ? 'on' : 'off']">
            {{ humanizationFlags.route_b_enabled ? 'on' : 'off' }}
          </span>
          <span class="flag-section">{{ t('observabilityPanel.settings.routeBSection') }}</span>
        </li>
      </ul>

      <hr class="settings-divider" />

      <h4 class="settings-section-title">{{ t('observabilityPanel.settings.personaCuriosityFlagsTitle') }}</h4>
      <p class="sub-hint">
        {{ t('observabilityPanel.settings.personaCuriosityFlagsHint') }}
      </p>
      <p v-if="personaCuriosityFlagsError" class="error">{{ personaCuriosityFlagsError }}</p>
      <ul v-if="personaCuriosityFlags" class="flag-list">
        <li class="flag-row">
          <span class="flag-name">{{ personaCuriosityFlags.env_names.enabled }}</span>
          <span :class="['flag-state', personaCuriosityFlags.enabled ? 'on' : 'off']">
            {{ personaCuriosityFlags.enabled ? 'on' : 'off' }}
          </span>
        </li>
        <li class="flag-row">
          <span class="flag-name">{{ personaCuriosityFlags.env_names.proactive_enabled }}</span>
          <span :class="['flag-state', personaCuriosityFlags.proactive_enabled ? 'on' : 'off']">
            {{ personaCuriosityFlags.proactive_enabled ? 'on' : 'off' }}
          </span>
        </li>
      </ul>
    </section>

    <!-- Turn detail modal -->
    <div v-if="selectedTurn" class="modal-backdrop" @click.self="closeDetail">
      <div class="modal-body">
        <div class="modal-head">
          <h3>{{ selectedTurn.kind }} · {{ formatTechnicalTime(selectedTurn.created_at) }}</h3>
          <div class="modal-head-actions">
            <UiButton size="sm" @click="exportSelectedTurnJson">
              {{ t('observabilityPanel.actions.exportTurnJson') }}
            </UiButton>
            <UiButton size="sm" @click="closeDetail">{{ t('common.actions.close') }}</UiButton>
          </div>
        </div>
        <div class="modal-meta">
          <span v-if="selectedTurn.model_id">model: {{ selectedTurn.model_id }}</span>
          <span v-if="selectedTurn.prompt_pack_hash">prompt pack: {{ selectedTurn.prompt_pack_hash.slice(0, 12) }}</span>
          <span v-if="selectedTurn.latency_ms !== null">latency: {{ selectedTurn.latency_ms }}ms</span>
          <span v-if="selectedTurn.prompt_tokens !== null">prompt: ~{{ selectedTurn.prompt_tokens }} tok</span>
          <span v-if="selectedTurn.completion_tokens !== null">completion: ~{{ selectedTurn.completion_tokens }} tok</span>
          <span v-if="selectedTurn.operator_feedback?.kind">
            feedback: {{ selectedTurn.operator_feedback.kind }}
          </span>
        </div>
        <div class="modal-feedback-actions">
          <UiButton
            size="sm"
            :loading="feedbackSaving === 'out_of_character'"
            @click="markSelectedTurnFeedback('out_of_character')"
          >{{ t('observabilityPanel.feedback.outOfCharacter') }}</UiButton>
          <UiButton
            size="sm"
            :loading="feedbackSaving === 'felt_human'"
            @click="markSelectedTurnFeedback('felt_human')"
          >{{ t('observabilityPanel.feedback.feltHuman') }}</UiButton>
        </div>
        <div v-if="selectedTurn.error" class="error">⚠ {{ selectedTurn.error }}</div>
        <div class="modal-section">
          <h4>{{ t('observabilityPanel.turnDetail.prompt') }}</h4>
          <pre class="code-block">{{ selectedTurn.prompt_assembled || t('observabilityPanel.fallback.noPrompt') }}</pre>
        </div>
        <div class="modal-section">
          <h4>{{ t('observabilityPanel.turnDetail.response') }}</h4>
          <pre class="code-block">{{ selectedTurn.response_text || t('observabilityPanel.fallback.noResponse') }}</pre>
        </div>
        <div v-if="selectedTurn.response_json" class="modal-section">
          <h4>{{ t('observabilityPanel.turnDetail.responseJson') }}</h4>
          <pre class="code-block">{{ JSON.stringify(selectedTurn.response_json, null, 2) }}</pre>
        </div>
        <div v-if="Object.keys(selectedTurn.post_turn_refs).length > 0" class="modal-section">
          <h4>{{ t('observabilityPanel.turnDetail.refs') }}</h4>
          <pre class="code-block">{{ JSON.stringify(selectedTurn.post_turn_refs, null, 2) }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.observability-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.subtabs {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.subtab {
  padding: 6px 12px;
  border: 1px solid rgba(255, 255, 255, 0.15);
  background: rgba(255, 255, 255, 0.05);
  color: inherit;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
}
.subtab.active {
  background: var(--color-primary, #7c3aed);
  border-color: var(--color-primary, #7c3aed);
  color: white;
}
/* `.btn` / `.btn-secondary` are repeated per-panel in this codebase
   (see PendingFollowUpsPanel.vue, OperatorPersonaPanel.vue, etc.) —
   they aren't defined globally in `style.css`. Keep them in sync with
   those siblings so the refresh button + modal close button render
   readable text. */
.refresh-btn {
  margin-left: auto;
}
.sub-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.technical-time-hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.filter-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.histogram h4 {
  margin: 0 0 6px;
  font-size: 13px;
  opacity: 0.8;
}
.bars {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.bar-row {
  display: grid;
  grid-template-columns: 110px 1fr 40px;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.bar-label {
  opacity: 0.8;
}
.bar-track {
  height: 14px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 2px;
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  background: var(--color-primary, #7c3aed);
  transition: width 0.2s ease;
}
.bar-fill.fill-pass { background: #16a34a; }
.bar-fill.fill-skip { background: #f59e0b; }
.bar-fill.fill-block { background: #6b7280; }
.bar-fill.fill-error { background: #dc2626; }
.bar-count {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.turn-list, .emotion-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 480px;
  overflow-y: auto;
}
.turn-row, .emotion-row {
  padding: 8px 10px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.emotion-row {
  cursor: default;
}
.turn-row:hover {
  background: rgba(255, 255, 255, 0.07);
}
.turn-row-head, .emotion-row-head {
  display: flex;
  gap: 10px;
  font-weight: 500;
  margin-bottom: 4px;
  flex-wrap: wrap;
  align-items: center;
}
.turn-kind, .emotion-cause {
  padding: 1px 6px;
  background: rgba(124, 58, 237, 0.2);
  border-radius: 2px;
  font-size: 11px;
}
.turn-when, .emotion-when {
  opacity: 0.6;
  font-size: 11px;
}
.turn-lat, .turn-tok, .emotion-intensity {
  font-variant-numeric: tabular-nums;
  font-size: 11px;
  opacity: 0.8;
}
.turn-feedback {
  padding: 1px 6px;
  border-radius: 2px;
  font-size: 11px;
  color: #4ade80;
  background: rgba(74, 222, 128, 0.12);
}
.turn-excerpt {
  opacity: 0.85;
  white-space: nowrap;
  text-overflow: ellipsis;
  overflow: hidden;
}
.emotion-label {
  font-weight: 600;
}
.emotion-quote {
  font-style: italic;
  opacity: 0.7;
  margin: 4px 0;
}
.emotion-deltas {
  display: flex;
  gap: 10px;
  opacity: 0.7;
  font-variant-numeric: tabular-nums;
  font-size: 11px;
}
.turn-error {
  color: #f87171;
  margin-top: 4px;
}
.error {
  color: #f87171;
  font-size: 13px;
}
.empty {
  opacity: 0.6;
  font-size: 13px;
}
.loading {
  opacity: 0.6;
  font-size: 13px;
}
.sub-hint {
  opacity: 0.75;
  font-size: 12px;
  line-height: 1.6;
  margin: 0;
}
.latency-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.curiosity-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.curiosity-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 86px;
  padding: 10px;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.03);
}
.curiosity-card strong {
  font-size: 22px;
  font-weight: 600;
  line-height: 1.1;
}
.curiosity-card small,
.metric-label {
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.4;
}
.usage-filter-row {
  flex-wrap: wrap;
}
.usage-date-input {
  width: 150px;
}
.usage-capabilities {
  display: inline-flex;
  gap: 4px;
  padding: 2px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
}
.usage-capability {
  min-width: 52px;
  padding: 5px 8px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-size: 12px;
}
.usage-capability.active {
  background: rgba(124, 58, 237, 0.85);
  color: #fff;
}
.usage-scope-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
  cursor: pointer;
}
.usage-scope-toggle input {
  accent-color: var(--color-primary);
}
.usage-scope-toggle.is-disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.usage-summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px;
}
.usage-summary-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 82px;
  padding: 10px;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.03);
}
.usage-summary-card strong {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.15;
}
.usage-summary-card small {
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.4;
}
.usage-table-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}
.usage-table-section {
  min-width: 0;
}
.usage-table-section h4 {
  margin: 0 0 6px;
  font-size: 13px;
  opacity: 0.8;
}
.usage-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.usage-table th,
.usage-table td {
  padding: 7px 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  text-align: left;
  vertical-align: top;
}
.usage-table th {
  font-weight: 600;
  opacity: 0.72;
}
.usage-table th.num,
.usage-table td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.usage-events-table {
  min-width: 760px;
}
.usage-table-section:has(.usage-events-table) {
  overflow-x: auto;
}
.usage-feature {
  display: inline-block;
  margin-right: 4px;
  padding: 1px 5px;
  border-radius: 3px;
  background: rgba(124, 58, 237, 0.2);
  font-size: 11px;
}
.usage-request-ids {
  min-width: 8rem;
}
.usage-request-ids code,
.usage-request-ids small {
  display: block;
}
.usage-request-ids small {
  margin-top: 2px;
  color: #64748b;
  font-size: 11px;
}
.usage-badge {
  display: inline-block;
  margin: 0 4px 4px 0;
  padding: 2px 5px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.08);
  font-size: 11px;
  white-space: nowrap;
}
.usage-badge.cached,
.usage-badge.actual {
  color: #4ade80;
  background: rgba(74, 222, 128, 0.12);
}
.usage-badge.live {
  color: #93c5fd;
  background: rgba(147, 197, 253, 0.12);
}
.usage-badge.estimated {
  color: #fbbf24;
  background: rgba(251, 191, 36, 0.12);
}
.turn-link-button {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--color-primary, #a78bfa);
  cursor: pointer;
  font-family: var(--font-mono, monospace);
  font-size: 12px;
}
.turn-link-button:hover {
  text-decoration: underline;
}
.curiosity-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.curiosity-table th,
.curiosity-table td {
  padding: 7px 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  text-align: left;
  vertical-align: top;
}
.curiosity-table th {
  font-weight: 600;
  opacity: 0.7;
}
.curiosity-status {
  display: inline-block;
  min-width: 64px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.08);
  text-align: center;
}
.curiosity-status.status-answered {
  color: #4ade80;
  background: rgba(74, 222, 128, 0.12);
}
.curiosity-status.status-deflected,
.curiosity-status.status-ignored {
  color: #fbbf24;
  background: rgba(251, 191, 36, 0.12);
}
.latency-table th,
.latency-table td {
  padding: 6px 10px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  text-align: left;
}
.latency-table th.num,
.latency-table td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.latency-table th {
  font-weight: 600;
  opacity: 0.7;
}
.settings-row {
  display: flex;
  gap: 12px;
  align-items: flex-end;
  flex-wrap: wrap;
}
.setting-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 130px;
}
.setting-field .field-input {
  width: 100%;
}
.settings-current {
  margin: 0;
  font-size: 13px;
  opacity: 0.85;
}
.settings-note {
  opacity: 0.7;
  margin-left: 6px;
}
.settings-divider {
  margin: 16px 0 12px;
  border: none;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}
.settings-section-title {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
}
.flag-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.flag-row {
  display: grid;
  grid-template-columns: 200px 60px 1fr;
  align-items: center;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.flag-name {
  font-family: var(--font-mono, monospace);
}
.flag-state {
  text-transform: uppercase;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  text-align: center;
}
.flag-state.on {
  color: #4ade80;
  background: rgba(74, 222, 128, 0.12);
}
.flag-state.off {
  color: #f87171;
  background: rgba(248, 113, 113, 0.12);
}
.flag-section {
  opacity: 0.55;
  font-size: 11px;
}
.funnel-summary {
  font-size: 13px;
  opacity: 0.8;
  margin-bottom: 6px;
}
.subsystem-health-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.subsystem-health-card {
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  padding: 12px;
  background: rgba(255, 255, 255, 0.03);
}
.subsystem-health-card h4 {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
}
.subsystem-health-ratio {
  margin: 0 0 6px;
  font-size: 24px;
  font-weight: 600;
}
.subsystem-health-hint {
  margin: 0 0 8px;
  font-size: 12px;
  opacity: 0.7;
  line-height: 1.5;
}
.subsystem-health-by-kind {
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
  opacity: 0.85;
}
.subsystem-health-by-kind li {
  padding: 2px 0;
}
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal-body {
  background: #1a1a1f;
  border-radius: 6px;
  max-width: 900px;
  width: 90vw;
  max-height: 85vh;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  border: 1px solid rgba(255, 255, 255, 0.1);
}
.modal-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.modal-head h3 {
  margin: 0;
  font-size: 14px;
}
.modal-head-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.modal-meta {
  display: flex;
  gap: 12px;
  font-size: 11px;
  opacity: 0.7;
  flex-wrap: wrap;
}
.modal-feedback-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.modal-section h4 {
  margin: 0 0 6px;
  font-size: 12px;
  opacity: 0.8;
}
.code-block {
  background: rgba(0, 0, 0, 0.3);
  border-radius: 3px;
  padding: 8px;
  font-size: 11px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow-y: auto;
  margin: 0;
}
</style>
