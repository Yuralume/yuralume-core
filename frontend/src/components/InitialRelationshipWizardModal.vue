<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CloseOutlined } from '@ant-design/icons-vue'
import { UiButton } from '@/components/ui'
import type { InitialRelationshipPayload } from '@/types/character'
import {
  analyzeCharacterCreationIntake,
  type CharacterCardPreview,
  type CharacterCreationIntakeQuestion,
  type CharacterCreationIntakeWarning,
} from '@/utils/api/characters'
import {
  applyInitialRelationshipSuggestion,
  canonicalInitialRelationshipField,
  nextIntakeRound,
} from '@/utils/characterCreationIntake'
import { buildCharacterCardIntakeDraft } from '@/utils/characterCardInitialRelationship'
import {
  buildInitialRelationshipPayload,
  emptyInitialRelationshipForm,
} from '@/composables/useInitialRelationshipForm'

const props = defineProps<{
  visible: boolean
  cardName: string
  card: CharacterCardPreview | null
  loading?: boolean
  /** Neutral scenario rewrite from a converted SillyTavern card. Pre-fills
   *  known_context but stays fully editable — the importer confirms it
   *  (never auto-applied to a relationship). */
  suggestedKnownContext?: string
}>()

const emit = defineEmits<{
  close: []
  confirm: [payload: InitialRelationshipPayload | null]
}>()

const { t, locale } = useI18n()
const form = ref(emptyInitialRelationshipForm())
const intakeLoading = ref(false)
const intakeRound = ref(0)
const intakeQuestions = ref<CharacterCreationIntakeQuestion[]>([])
const intakeWarnings = ref<CharacterCreationIntakeWarning[]>([])
const intakeChecked = ref(false)
const intakePassed = ref(false)
const intakeStale = ref(false)
const intakeError = ref<string | null>(null)

const payload = computed(() => buildInitialRelationshipPayload(form.value))
const canSubmitSeed = computed(() => payload.value !== null)
const intakeActionLabel = computed(() => {
  if (intakeLoading.value) return t('playerSidebar.characterCards.relationship.analyzing')
  return intakeChecked.value
    ? t('playerSidebar.characterCards.relationship.analyzeAgainAction')
    : t('playerSidebar.characterCards.relationship.analyzeAction')
})
const showIntakeFeedback = computed(() => Boolean(
  intakeError.value
  || intakeWarnings.value.length
  || intakeQuestions.value.length
  || (intakePassed.value && !intakeStale.value),
))

watch(() => props.visible, (visible) => {
  if (visible) {
    const fresh = emptyInitialRelationshipForm()
    // Pre-fill known_context from a converted SillyTavern scenario (D5).
    // The importer still edits/confirms it before it seeds anything.
    if (props.suggestedKnownContext) {
      fresh.known_context = props.suggestedKnownContext
    }
    form.value = fresh
    resetIntakeState()
  }
})

watch(form, markIntakeStale, { deep: true })

function close() {
  if (props.loading || intakeLoading.value) return
  emit('close')
}

function skip() {
  if (props.loading || intakeLoading.value) return
  emit('confirm', null)
}

function confirm() {
  if (props.loading || intakeLoading.value) return
  emit('confirm', payload.value)
}

async function runRelationshipIntake() {
  if (!props.card || props.loading || intakeLoading.value) return
  intakeLoading.value = true
  intakeError.value = null
  try {
    const analysis = await analyzeCharacterCreationIntake({
      character_draft: buildCharacterCardIntakeDraft(props.card),
      relationship: payload.value,
      current_locale: String(locale.value),
      round_index: intakeRound.value,
    })
    intakeChecked.value = true
    intakeStale.value = false
    intakeQuestions.value = analysis.questions ?? []
    intakeWarnings.value = analysis.warnings ?? []
    const hasBlockingWarning = intakeWarnings.value.some(warning => warning.blocking)
    intakePassed.value = !hasBlockingWarning && analysis.can_create
    if (!intakePassed.value) {
      intakeRound.value = nextIntakeRound(intakeRound.value)
    }
  } catch {
    intakeQuestions.value = []
    intakeWarnings.value = []
    intakePassed.value = false
    intakeError.value = t('playerSidebar.characterCards.relationship.analysisFailed')
  } finally {
    intakeLoading.value = false
  }
}

function applyIntakeSuggestion(field: string, suggestion: string) {
  const value = suggestion.trim()
  if (!value) return
  applyInitialRelationshipSuggestion(form.value, field, value, t('common.sentenceJoiner'))
  removeAnsweredIntakeQuestion(field)
  markIntakeStale()
  intakeError.value = null
}

function setLivingArrangement(value: string) {
  form.value.living_arrangement = value
  removeAnsweredIntakeQuestion('living_arrangement')
  markIntakeStale()
  intakeError.value = null
}

function removeAnsweredIntakeQuestion(field: string) {
  const normalized = canonicalInitialRelationshipField(field)
  intakeQuestions.value = intakeQuestions.value.filter(question => (
    canonicalInitialRelationshipField(question.field) !== normalized
  ))
}

function markIntakeStale() {
  if (!intakeChecked.value) return
  intakeStale.value = true
  intakePassed.value = false
}

function resetIntakeState() {
  intakeLoading.value = false
  intakeRound.value = 0
  intakeQuestions.value = []
  intakeWarnings.value = []
  intakeChecked.value = false
  intakePassed.value = false
  intakeStale.value = false
  intakeError.value = null
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="relationship-wizard"
      role="dialog"
      aria-modal="true"
      @click.self="close"
    >
      <section class="relationship-wizard__panel">
        <header class="relationship-wizard__header">
          <div>
            <p class="relationship-wizard__eyebrow">
              {{ t('playerSidebar.characterCards.relationship.eyebrow') }}
            </p>
            <h3>{{ t('playerSidebar.characterCards.relationship.title', { name: cardName }) }}</h3>
          </div>
          <div class="relationship-wizard__header-actions">
            <UiButton
              size="sm"
              variant="ghost"
              :loading="intakeLoading"
              :disabled="loading || !card"
              @click="runRelationshipIntake"
            >
              {{ intakeActionLabel }}
            </UiButton>
            <button
              type="button"
              class="relationship-wizard__close"
              :disabled="loading || intakeLoading"
              :aria-label="t('common.actions.close')"
              @click="close"
            >
              <CloseOutlined />
            </button>
          </div>
        </header>

        <p class="field-hint">
          {{ t('playerSidebar.characterCards.relationship.hint') }}
        </p>

        <div v-if="showIntakeFeedback" class="relationship-wizard__intake">
          <div v-if="intakeError" class="intake-warning">
            {{ intakeError }}
          </div>
          <div
            v-if="intakePassed && !intakeStale && !intakeQuestions.length && !intakeWarnings.length"
            class="intake-ready"
            role="status"
          >
            {{ t('playerSidebar.characterCards.relationship.ready') }}
          </div>
          <div
            v-for="warning in intakeWarnings"
            :key="`${warning.kind}-${warning.message}`"
            class="intake-warning"
          >
            {{ warning.message }}
          </div>
          <div
            v-for="question in intakeQuestions"
            :key="`${question.field}-${question.question}`"
            class="intake-question"
          >
            <p>{{ question.question }}</p>
            <div v-if="question.suggestions.length" class="intake-suggestions">
              <button
                v-for="suggestion in question.suggestions"
                :key="`${question.field}-${suggestion}`"
                type="button"
                class="intake-suggestion"
                @click="applyIntakeSuggestion(question.field, suggestion)"
              >
                {{ suggestion }}
              </button>
            </div>
          </div>
        </div>

        <label class="field-label">{{ t('characterCreate.initialRelationship.relationshipLabel') }}</label>
        <input
          v-model="form.relationship_label"
          class="field-input"
          :placeholder="t('characterCreate.initialRelationship.relationshipPlaceholder')"
        />

        <div class="relationship-wizard__grid">
          <label>
            <span class="field-label">{{ t('characterCreate.initialRelationship.userAddress') }}</span>
            <input
              v-model="form.user_address_name"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.userAddressPlaceholder')"
            />
          </label>
          <label>
            <span class="field-label">{{ t('characterCreate.initialRelationship.characterAddress') }}</span>
            <input
              v-model="form.character_address_name"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.characterAddressPlaceholder')"
            />
          </label>
        </div>

        <label class="field-label">{{ t('characterCreate.initialRelationship.knownContext') }}</label>
        <textarea
          v-model="form.known_context"
          class="field-textarea"
          rows="2"
          :placeholder="t('playerSidebar.characterCards.relationship.knownContextPlaceholder')"
        />

        <label class="field-label">{{ t('characterCreate.initialRelationship.livingArrangement') }}</label>
        <div class="relationship-wizard__chips" role="group">
          <button
            v-for="option in ['together', 'nearby', 'separate', 'unset']"
            :key="option"
            type="button"
            class="relationship-chip"
            :class="{ 'relationship-chip--active': form.living_arrangement === t(`characterCreate.initialRelationship.livingOptions.${option}`) || (option === 'unset' && !form.living_arrangement) }"
            @click="setLivingArrangement(option === 'unset' ? '' : t(`characterCreate.initialRelationship.livingOptions.${option}`))"
          >
            {{ t(`characterCreate.initialRelationship.livingOptions.${option}`) }}
          </button>
        </div>
        <input
          v-model="form.living_arrangement"
          class="field-input"
          :placeholder="t('characterCreate.initialRelationship.livingPlaceholder')"
        />

        <label class="field-label">{{ t('characterCreate.initialRelationship.toneDistance') }}</label>
        <input
          v-model="form.tone_distance"
          class="field-input"
          :placeholder="t('characterCreate.initialRelationship.toneDistancePlaceholder')"
        />

        <label class="field-label">{{ t('characterCreate.initialRelationship.boundary') }}</label>
        <input
          v-model="form.familiarity_boundary"
          class="field-input"
          :placeholder="t('playerSidebar.characterCards.relationship.boundaryPlaceholder')"
        />

        <div class="relationship-wizard__grid">
          <label>
            <span class="field-label">{{ t('characterCreate.initialRelationship.scheduleLabel') }}</span>
            <select
              v-model="form.schedule_involvement_policy"
              class="field-select"
            >
              <option value="none">{{ t('characterCreate.initialRelationship.scheduleOptions.none') }}</option>
              <option value="mention_only">{{ t('characterCreate.initialRelationship.scheduleOptions.mentionOnly') }}</option>
              <option value="invite_required">{{ t('characterCreate.initialRelationship.scheduleOptions.inviteRequired') }}</option>
              <option value="shared_allowed">{{ t('characterCreate.initialRelationship.scheduleOptions.sharedAllowed') }}</option>
            </select>
          </label>
        </div>

        <label class="relationship-wizard__checkbox">
          <input v-model="form.proactive_permission" type="checkbox" />
          <span>{{ t('characterCreate.initialRelationship.proactivePermission') }}</span>
        </label>
        <input
          v-if="form.proactive_permission"
          v-model="form.proactive_cadence_hint"
          class="field-input"
          :placeholder="t('characterCreate.initialRelationship.proactiveCadencePlaceholder')"
        />

        <div class="relationship-wizard__grid">
          <label>
            <span class="field-label">{{ t('characterCreate.initialRelationship.profileInterests') }}</span>
            <input
              v-model="form.profile_interests"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.profileInterestsPlaceholder')"
            />
          </label>
          <label>
            <span class="field-label">{{ t('characterCreate.initialRelationship.profileRoutine') }}</span>
            <input
              v-model="form.profile_routine"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.profileRoutinePlaceholder')"
            />
          </label>
        </div>
        <input
          v-model="form.profile_life_goals"
          class="field-input"
          :placeholder="t('characterCreate.initialRelationship.profileGoalsPlaceholder')"
        />
        <textarea
          v-model="form.user_profile_notes"
          class="field-textarea"
          rows="2"
          :placeholder="t('characterCreate.initialRelationship.notesPlaceholder')"
        />

        <footer class="relationship-wizard__actions">
          <UiButton variant="ghost" :disabled="loading || intakeLoading" @click="skip">
            {{ t('playerSidebar.characterCards.relationship.skipAction') }}
          </UiButton>
          <UiButton
            variant="primary"
            :loading="loading"
            :disabled="intakeLoading || !canSubmitSeed"
            @click="confirm"
          >
            {{ t('playerSidebar.characterCards.relationship.confirmAction') }}
          </UiButton>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.relationship-wizard {
  position: fixed;
  inset: 0;
  z-index: 1200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(5, 8, 14, 0.72);
}

.relationship-wizard__panel {
  width: min(720px, 100%);
  max-height: min(760px, calc(100vh - 48px));
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px;
  border: 1px solid rgba(148, 163, 184, 0.22);
  border-radius: 8px;
  background: #111827;
  color: #e5e7eb;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.38);
}

.relationship-wizard__header,
.relationship-wizard__actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.relationship-wizard__header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.relationship-wizard__header h3 {
  margin: 2px 0 0;
  font-size: 1.05rem;
}

.relationship-wizard__eyebrow {
  margin: 0;
  color: #93c5fd;
  font-size: 0.78rem;
}

.relationship-wizard__close {
  width: 32px;
  height: 32px;
  border: 1px solid rgba(148, 163, 184, 0.28);
  border-radius: 6px;
  background: rgba(15, 23, 42, 0.86);
  color: #e5e7eb;
  cursor: pointer;
}

.relationship-wizard__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.relationship-wizard__intake {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 1px solid rgba(148, 163, 184, 0.2);
  border-radius: 8px;
  background: rgba(15, 23, 42, 0.72);
}

.intake-ready {
  color: #86efac;
  font-size: 0.86rem;
}

.intake-warning,
.intake-question {
  padding: 8px;
  border-radius: 6px;
  background: rgba(30, 41, 59, 0.72);
  color: #dbeafe;
  font-size: 0.86rem;
  line-height: 1.5;
}

.intake-warning {
  color: #fde68a;
}

.intake-question p {
  margin: 0 0 8px;
}

.intake-suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.intake-suggestion {
  border: 1px solid rgba(147, 197, 253, 0.36);
  border-radius: 6px;
  padding: 5px 8px;
  background: rgba(37, 99, 235, 0.14);
  color: #bfdbfe;
  cursor: pointer;
  font-size: 0.8rem;
}

.intake-suggestion:hover {
  background: rgba(37, 99, 235, 0.24);
}

.relationship-wizard__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.relationship-chip {
  min-height: 30px;
  border: 1px solid rgba(147, 197, 253, 0.28);
  border-radius: 6px;
  padding: 5px 8px;
  background: rgba(15, 23, 42, 0.86);
  color: #dbeafe;
  cursor: pointer;
  font-size: 0.82rem;
}

.relationship-chip--active {
  border-color: rgba(147, 197, 253, 0.72);
  background: rgba(37, 99, 235, 0.22);
  color: #eff6ff;
}

.relationship-wizard__checkbox {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  color: #d1d5db;
  font-size: 0.9rem;
}

.relationship-wizard__actions {
  justify-content: flex-end;
  padding-top: 4px;
}

@media (max-width: 640px) {
  .relationship-wizard {
    padding: 12px;
    align-items: stretch;
  }

  .relationship-wizard__panel {
    max-height: calc(100vh - 24px);
  }

  .relationship-wizard__header {
    align-items: flex-start;
  }

  .relationship-wizard__header-actions {
    flex-direction: column-reverse;
    align-items: flex-end;
  }

  .relationship-wizard__grid {
    grid-template-columns: 1fr;
  }
}
</style>
