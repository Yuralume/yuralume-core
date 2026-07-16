<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  getOperatorPersona,
  rejectPersonaCandidate,
  transitionPersonaFieldState,
  triggerPersonaDreamTick,
  type PersonaCandidate,
  type PersonaField,
  type PersonaSnapshot,
} from '@/utils/api/operatorPersona'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { formatDateTime, formatRelativeTime } from '@/i18n/formatters'
import { UiButton } from '@/components/ui'

const props = defineProps<{
  // Required — persona is per-character. Each character builds its
  // own picture of the operator; sibling characters can't see each
  // other's observations. The panel needs to know which character's
  // view it's showing.
  characterId: string | null
}>()

const { t } = useI18n()
const { locale } = useLocale()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()
const snapshot = ref<PersonaSnapshot | null>(null)
const loading = ref(false)
const errorMsg = ref<string | null>(null)
const actionBusy = ref(false)
const actionMsg = ref<string | null>(null)

const FAMILIARITY_LABEL_KEY: Record<string, string> = {
  stranger: 'operatorPersona.familiarity.stranger',
  acquaintance: 'operatorPersona.familiarity.acquaintance',
  familiar: 'operatorPersona.familiarity.familiar',
  close: 'operatorPersona.familiarity.close',
}

const LAYER_TITLE_KEY: Record<number, string> = {
  1: 'operatorPersona.layers.identity',
  2: 'operatorPersona.layers.life',
  3: 'operatorPersona.layers.emotional',
  5: 'operatorPersona.layers.trust',
}

const FIELD_LABEL_KEY: Record<string, string> = {
  // Layer 1
  name: 'operatorPersona.fields.name',
  nickname: 'operatorPersona.fields.nickname',
  age: 'operatorPersona.fields.age',
  occupation: 'operatorPersona.fields.occupation',
  company_or_school: 'operatorPersona.fields.companyOrSchool',
  residence: 'operatorPersona.fields.residence',
  family: 'operatorPersona.fields.family',
  // Layer 2
  interests: 'operatorPersona.fields.interests',
  diet: 'operatorPersona.fields.diet',
  routine: 'operatorPersona.fields.routine',
  consumption_style: 'operatorPersona.fields.consumptionStyle',
  income_band: 'operatorPersona.fields.incomeBand',
  relationship_status: 'operatorPersona.fields.relationshipStatus',
  life_goals: 'operatorPersona.fields.lifeGoals',
  // Layer 3
  anxieties: 'operatorPersona.fields.anxieties',
  traumas: 'operatorPersona.fields.traumas',
  secrets: 'operatorPersona.fields.secrets',
  vulnerabilities: 'operatorPersona.fields.vulnerabilities',
  values: 'operatorPersona.fields.values',
  openness_level: 'operatorPersona.fields.opennessLevel',
  // Layer 5
  money_borrowed: 'operatorPersona.fields.moneyBorrowed',
  help_asked: 'operatorPersona.fields.helpAsked',
  vulnerability_shown: 'operatorPersona.fields.vulnerabilityShown',
  family_introduced: 'operatorPersona.fields.familyIntroduced',
  resource_shared: 'operatorPersona.fields.resourceShared',
  secret_kept: 'operatorPersona.fields.secretKept',
}

const layers = computed(() => {
  if (!snapshot.value) return []
  return [
    { layer: 1, fields: snapshot.value.layer1_identity },
    { layer: 2, fields: snapshot.value.layer2_life },
    { layer: 3, fields: snapshot.value.layer3_emotional },
    { layer: 5, fields: snapshot.value.layer5_trust },
  ]
})

const strengthLabel = computed(() => {
  const s = snapshot.value?.interaction_strength
  if (!s) return ''
  const key = FAMILIARITY_LABEL_KEY[s.familiarity_band]
  return key ? t(key) : s.familiarity_band
})

const initialRelationshipLabel = computed(() => {
  const label = snapshot.value?.initial_relationship?.relationship_label?.trim()
  return label || ''
})

const hasAnyConfirmed = computed(() => {
  if (!snapshot.value) return false
  return (
    snapshot.value.layer1_identity.length +
    snapshot.value.layer2_life.length +
    snapshot.value.layer3_emotional.length +
    snapshot.value.layer5_trust.length
  ) > 0
})

async function reload() {
  if (!props.characterId) {
    snapshot.value = null
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    snapshot.value = await getOperatorPersona(props.characterId)
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t('operatorPersona.errors.loadFailed')
    snapshot.value = null
  } finally {
    loading.value = false
  }
}

async function handleDreamTick() {
  if (actionBusy.value || !props.characterId) return
  actionBusy.value = true
  actionMsg.value = null
  try {
    const result = await triggerPersonaDreamTick(props.characterId)
    if (!result.applied) {
      actionMsg.value = t('operatorPersona.dreamResult.noop')
    } else {
      const bits: string[] = []
      if (result.promotions) bits.push(t('operatorPersona.dreamResult.promotions', { count: result.promotions }))
      if (result.merges) bits.push(t('operatorPersona.dreamResult.merges', { count: result.merges }))
      if (result.supersedes) bits.push(t('operatorPersona.dreamResult.supersedes', { count: result.supersedes }))
      if (result.rejections) bits.push(t('operatorPersona.dreamResult.rejections', { count: result.rejections }))
      if (result.decays) bits.push(t('operatorPersona.dreamResult.decays', { count: result.decays }))
      if (result.inferences) bits.push(t('operatorPersona.dreamResult.inferences', { count: result.inferences }))
      actionMsg.value = t('operatorPersona.dreamResult.applied', { details: bits.join(t('common.listSeparator')) })
    }
    await reload()
  } catch (err) {
    actionMsg.value = err instanceof Error
      ? t('operatorPersona.errors.triggerFailedWithReason', { reason: err.message })
      : t('operatorPersona.errors.triggerFailed')
  } finally {
    actionBusy.value = false
    setTimeout(() => { actionMsg.value = null }, 6000)
  }
}

async function handleRejectCandidate(cand: PersonaCandidate) {
  if (actionBusy.value || !cand.candidate_id) return
  if (!await confirmDialog({
    content: t('operatorPersona.confirmRejectCandidate', {
      field: cand.field_key,
      value: cand.proposed_value,
      quote: cand.evidence.quote,
    }),
    okText: t('common.actions.remove'),
    danger: true,
  })) {
    return
  }
  actionBusy.value = true
  try {
    await rejectPersonaCandidate(cand.candidate_id)
    await reload()
  } catch (err) {
    actionMsg.value = err instanceof Error
      ? t('operatorPersona.errors.rejectFailedWithReason', { reason: err.message })
      : t('operatorPersona.errors.rejectFailed')
  } finally {
    actionBusy.value = false
  }
}

async function handleMarkStale(field: PersonaField) {
  if (actionBusy.value || !field.field_id) return
  if (!await confirmDialog({
    content: t('operatorPersona.confirmMarkStale', {
      field: field.field_key,
      value: field.value,
    }),
  })) {
    return
  }
  actionBusy.value = true
  try {
    await transitionPersonaFieldState(field.field_id, 'stale')
    await reload()
  } catch (err) {
    actionMsg.value = err instanceof Error
      ? t('operatorPersona.errors.actionFailedWithReason', { reason: err.message })
      : t('operatorPersona.errors.actionFailed')
  } finally {
    actionBusy.value = false
  }
}

function fieldLabel(key: string): string {
  const labelKey = FIELD_LABEL_KEY[key]
  return labelKey ? t(labelKey) : key
}

function confidenceTier(conf: number): 'high' | 'mid' | 'low' {
  if (conf >= 0.9) return 'high'
  if (conf >= 0.75) return 'mid'
  return 'low'
}

function formatConfidence(conf: number): string {
  return `${Math.round(conf * 100)}%`
}

function formatRelative(iso: string): string {
  return formatRelativeTime(iso, locale.value)
}

function formatAbsolute(iso: string): string {
  return formatDateTime(iso, locale.value, timeZone.value)
}

watch(() => props.characterId, () => { void reload() }, { immediate: true })

// 30s polling so promote / supersede show up after a dream tick
// without manual refresh. Persona changes slowly enough that 30s is
// generous; the panel is mounted only when the operator opens this
// section so we're not burning network when idle.
let pollTimer: ReturnType<typeof setInterval> | null = null
pollTimer = setInterval(() => { void reload() }, 30000)
onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})
</script>

<template>
  <section class="persona-panel">
    <header class="panel-header">
      <div>
        <h3 class="section-title">{{ t('operatorPersona.title') }}</h3>
        <p class="panel-hint">
          {{ t('operatorPersona.hint') }}
        </p>
      </div>
      <div class="panel-actions">
        <UiButton size="sm" :loading="loading" @click="reload">{{ t('common.actions.refresh') }}</UiButton>
        <UiButton
          size="sm"
          :loading="actionBusy"
          :title="t('operatorPersona.dreamNowTitle')"
          @click="handleDreamTick"
        >{{ t('operatorPersona.dreamNowAction') }}</UiButton>
      </div>
    </header>

    <div v-if="actionMsg" class="panel-toast">{{ actionMsg }}</div>
    <div v-if="errorMsg" class="panel-error">{{ errorMsg }}</div>

    <div v-if="!characterId" class="panel-empty">{{ t('operatorPersona.noCharacter') }}</div>
    <div v-else-if="loading && !snapshot" class="panel-empty">{{ t('common.state.loading') }}</div>

    <template v-else-if="snapshot">
      <div v-if="initialRelationshipLabel" class="relationship-card">
        <span class="relationship-kicker">{{ t('operatorPersona.initialRelationship.title') }}</span>
        <span class="relationship-label">{{ initialRelationshipLabel }}</span>
      </div>

      <!-- Layer 4: interaction strength -->
      <div v-if="snapshot.interaction_strength" class="strength-card">
        <div class="strength-head">
          <span class="strength-title">{{ t('operatorPersona.strength.title') }}</span>
          <span class="strength-band">{{ strengthLabel }}</span>
          <span class="strength-days">
            {{ t('operatorPersona.strength.daysKnown', { count: snapshot.interaction_strength.days_since_first_contact }) }}
          </span>
        </div>
        <div class="strength-meta">
          <span>{{ t('operatorPersona.strength.totalMessages', { count: snapshot.interaction_strength.total_user_messages }) }}</span>
          <span>{{ t('operatorPersona.strength.messages7d', { count: snapshot.interaction_strength.messages_last_7_days }) }}</span>
          <span>{{ t('operatorPersona.strength.messages30d', { count: snapshot.interaction_strength.messages_last_30_days }) }}</span>
          <span>{{ t('operatorPersona.strength.longestSession', { count: snapshot.interaction_strength.longest_session_minutes }) }}</span>
          <span>{{ t('operatorPersona.strength.sharedArcs', { count: snapshot.interaction_strength.shared_arc_realized_count }) }}</span>
          <span>{{ t('operatorPersona.strength.sharedDramas', { count: snapshot.interaction_strength.shared_drama_count }) }}</span>
        </div>
        <p class="strength-note">
          {{ t('operatorPersona.strength.note', { band: strengthLabel }) }}
        </p>
      </div>

      <!-- Confirmed fields per layer -->
      <div v-if="hasAnyConfirmed" class="layers-block">
        <div
          v-for="entry in layers"
          v-show="entry.fields.length > 0"
          :key="entry.layer"
          class="layer-card"
        >
          <h4 class="layer-title">
            <span class="layer-badge">L{{ entry.layer }}</span>
            {{ t(LAYER_TITLE_KEY[entry.layer]) }}
            <span class="layer-count">{{ entry.fields.length }}</span>
          </h4>
          <ul class="field-list">
            <li
              v-for="field in entry.fields"
              :key="field.field_id ?? field.field_key"
              :class="['field-row', `conf-${confidenceTier(field.confidence)}`]"
            >
              <div class="field-head">
                <span class="field-key">{{ fieldLabel(field.field_key) }}</span>
                <span class="field-conf">{{ formatConfidence(field.confidence) }}</span>
                <span
                  v-if="field.source === 'dream_inference'"
                  class="field-source-pill"
                  :title="t('operatorPersona.source.inferenceTitle')"
                >{{ t('operatorPersona.source.inference') }}</span>
                <span
                  v-else-if="field.source === 'user_explicit'"
                  class="field-source-pill explicit"
                  :title="t('operatorPersona.source.explicitTitle')"
                >{{ t('operatorPersona.source.explicit') }}</span>
                <button
                  class="row-icon-btn"
                  :disabled="actionBusy || !field.field_id"
                  :title="t('operatorPersona.hideTitle')"
                  @click="handleMarkStale(field)"
                >{{ t('operatorPersona.hideAction') }}</button>
              </div>
              <div class="field-value">{{ field.value }}</div>
              <details v-if="field.evidence.length > 0" class="evidence-block">
                <summary>{{ t('operatorPersona.evidenceSummary', { count: field.evidence.length, updates: field.update_count }) }}</summary>
                <ul class="evidence-list">
                  <li v-for="(ev, idx) in field.evidence" :key="idx">
                    <span class="evidence-quote">{{ t('operatorPersona.quote', { quote: ev.quote }) }}</span>
                    <span class="evidence-time" :title="formatAbsolute(ev.extracted_at)">
                      · {{ formatRelative(ev.extracted_at) }}
                    </span>
                  </li>
                </ul>
              </details>
            </li>
          </ul>
        </div>
      </div>
      <div v-else class="panel-empty">
        {{ t('operatorPersona.emptyConfirmed') }}
      </div>

      <!-- Pending candidates -->
      <div v-if="snapshot.pending_candidates.length > 0" class="pending-card">
        <h4 class="pending-title">
          {{ t('operatorPersona.pendingTitle', { count: snapshot.pending_candidates.length }) }}
          <span class="pending-hint">{{ t('operatorPersona.pendingHint') }}</span>
        </h4>
        <ul class="pending-list">
          <li
            v-for="cand in snapshot.pending_candidates"
            :key="cand.candidate_id ?? cand.evidence.quote"
            class="pending-row"
          >
            <div class="pending-head">
              <span class="layer-badge">L{{ cand.layer }}</span>
              <span class="field-key">{{ fieldLabel(cand.field_key) }}</span>
              <span class="field-conf">{{ formatConfidence(cand.raw_extractor_confidence) }}</span>
              <span v-if="cand.explicit" class="field-source-pill explicit">{{ t('operatorPersona.source.explicit') }}</span>
              <button
                class="row-icon-btn warn"
                :disabled="actionBusy || !cand.candidate_id"
                :title="t('operatorPersona.rejectTitle')"
                @click="handleRejectCandidate(cand)"
              >{{ t('operatorPersona.rejectAction') }}</button>
            </div>
            <div class="field-value">{{ cand.proposed_value }}</div>
            <div class="evidence-inline">
              {{ t('operatorPersona.quote', { quote: cand.evidence.quote }) }}
              <span class="evidence-time" :title="formatAbsolute(cand.extracted_at)">
                · {{ formatRelative(cand.extracted_at) }}
              </span>
            </div>
          </li>
        </ul>
      </div>

      <!-- Prompt preview -->
      <details v-if="snapshot.prompt_preview_lines.length > 0" class="prompt-preview">
        <summary>{{ t('operatorPersona.promptPreviewTitle') }}</summary>
        <pre class="preview-text">{{ snapshot.prompt_preview_lines.join('\n') }}</pre>
      </details>
    </template>
  </section>
</template>

<style scoped>
.persona-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
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
  color: var(--color-text-secondary);
  line-height: 1.5;
  max-width: 380px;
}

.panel-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.panel-toast {
  padding: 8px 10px;
  background: rgba(96, 165, 250, 0.12);
  border: 1px solid rgba(96, 165, 250, 0.45);
  border-radius: 6px;
  font-size: 12px;
}

.panel-error {
  padding: 8px 10px;
  background: rgba(239, 68, 68, 0.12);
  border: 1px solid rgba(239, 68, 68, 0.45);
  border-radius: 6px;
  font-size: 12px;
  color: #ff8a75;
}

.panel-empty {
  padding: 12px;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.6;
  background: rgba(255, 255, 255, 0.03);
  border: 1px dashed var(--color-border);
  border-radius: 6px;
}

.relationship-card {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border: 1px solid var(--color-border, #ddd);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.04);
}

.relationship-kicker {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.relationship-label {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-primary);
}

/* Layer 4 strength card */
.strength-card {
  padding: 10px 12px;
  border: 1px solid var(--color-border, #ddd);
  border-radius: 8px;
  background: linear-gradient(135deg, rgba(168, 85, 247, 0.06), rgba(96, 165, 250, 0.06));
}

.strength-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.strength-title {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.strength-band {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-primary-light);
}

.strength-days {
  font-size: 13px;
  color: var(--color-text-secondary);
}

.strength-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.strength-note {
  margin: 6px 0 0 0;
  font-size: 11px;
  color: var(--color-text-secondary);
  font-style: italic;
}

/* Layer cards */
.layers-block {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.layer-card {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 8px 10px;
  background: rgba(255, 255, 255, 0.025);
}

.layer-title {
  margin: 0 0 6px 0;
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}

.layer-badge {
  font-size: 10px;
  padding: 2px 5px;
  border-radius: 4px;
  background: rgba(96, 165, 250, 0.15);
  color: #1e40af;
  font-weight: 600;
}

.layer-count {
  margin-left: auto;
  font-size: 11px;
  color: var(--color-text-secondary);
  font-weight: normal;
}

.field-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-row {
  padding: 6px 8px;
  border-left: 3px solid var(--color-border);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.035);
}
.field-row.conf-high { border-left-color: #16a34a; }
.field-row.conf-mid  { border-left-color: #eab308; }
.field-row.conf-low  { border-left-color: #9ca3af; }

.field-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  flex-wrap: wrap;
}

.field-key {
  font-weight: 600;
}

.field-conf {
  font-variant-numeric: tabular-nums;
  color: var(--color-text-secondary);
}

.field-source-pill {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 3px;
  background: rgba(250, 204, 21, 0.18);
  color: #92400e;
}
.field-source-pill.explicit {
  background: rgba(34, 197, 94, 0.18);
  color: #166534;
}

.row-icon-btn {
  margin-left: auto;
  padding: 2px 6px;
  font-size: 11px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text);
  border-radius: 4px;
  cursor: pointer;
}
.row-icon-btn.warn { color: #ff8a75; border-color: rgba(239, 68, 68, 0.4); }
.row-icon-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.field-value {
  font-size: 13px;
  margin: 2px 0 4px 0;
  line-height: 1.4;
}

.evidence-block summary {
  cursor: pointer;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.evidence-list {
  list-style: none;
  margin: 4px 0 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.evidence-list li {
  font-size: 11px;
  line-height: 1.5;
}

.evidence-quote {
  color: var(--color-text);
}

.evidence-time {
  color: var(--color-text-secondary);
  margin-left: 4px;
}

.evidence-inline {
  font-size: 11px;
  color: var(--color-text-secondary);
  margin-top: 4px;
  font-style: italic;
}

/* Pending card */
.pending-card {
  border: 1px dashed rgba(250, 204, 21, 0.5);
  border-radius: 8px;
  padding: 8px 10px;
  background: rgba(250, 204, 21, 0.05);
}

.pending-title {
  margin: 0 0 6px 0;
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}

.pending-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  font-weight: normal;
}

.pending-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pending-row {
  padding: 6px 8px;
  background: rgba(255, 255, 255, 0.035);
  border-radius: 4px;
}

.pending-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  flex-wrap: wrap;
}

/* Prompt preview */
.prompt-preview {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  padding: 8px 10px;
  background: rgba(255, 255, 255, 0.025);
}

.prompt-preview summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.preview-text {
  margin: 8px 0 0 0;
  padding: 8px;
  font-size: 11px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  background: rgba(0, 0, 0, 0.22);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  color: var(--color-text);
  white-space: pre-wrap;
  line-height: 1.5;
}
</style>
