<script setup lang="ts">
/**
 * 新增角色專用 modal。
 *
 * 與「設定」tab 的編輯表單完全獨立：form state 全部是 local ref，
 * 不會被 `selectedCharacter` 的 watch 污染。這樣使用者不會因為
 * 在編輯既有角色時誤按「建立」或反之，造成蓋掉或複製。
 *
 * 進階設定（主動訊息、外界資訊、Story Gacha）留給建立
 * 完成後在編輯模式調整，避免建立表單太長、也讓新手聚焦必填的
 * 人格描述。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type {
  Character,
  CharacterCompanion,
  CharacterPersonalityType,
  CharacterPersonalityTypeCode,
  CharacterPersonalityTypeSource,
  CharacterVisualGenerationStyle,
  CreateCharacterRequest,
  VisualSubjectType,
} from '@/types/character'
import {
  analyzeCharacterCreationIntake,
  createCharacter,
  type CharacterCreationIntakeQuestion,
  type CharacterCreationIntakeWarning,
  type CharacterDraftNameCandidate,
  generateCharacterDraft,
} from '@/utils/api/characters'
import { getCurrentActivity } from '@/utils/api/schedule'
import {
  applyInitialRelationshipSuggestion,
  canonicalInitialRelationshipField,
  nextIntakeRound,
  shouldBlockCreateForIntake,
} from '@/utils/characterCreationIntake'
import { UiButton } from '@/components/ui'
import CharacterIdentityFields from './CharacterIdentityFields.vue'
import {
  buildInitialRelationshipPayload,
  emptyInitialRelationshipForm,
  splitList,
} from '@/composables/useInitialRelationshipForm'

const { t, locale } = useI18n()

const PERSONALITY_TYPE_CODES: CharacterPersonalityTypeCode[] = [
  'INTJ', 'INTP', 'ENTJ', 'ENTP',
  'INFJ', 'INFP', 'ENFJ', 'ENFP',
  'ISTJ', 'ISFJ', 'ESTJ', 'ESFJ',
  'ISTP', 'ISFP', 'ESTP', 'ESFP',
]

const emit = defineEmits<{
  close: []
  created: [char: Character]
}>()

function emptyForm() {
  return {
    name: '',
    summary: '',
    personality: '',
    interests: '',
    speaking_style: '',
    boundaries: '',
    aspirations: '',
    appearance: '',
    gender_identity: '',
    third_person_pronoun: '',
    visual_gender_presentation: '',
    visual_subject_type: 'auto' as VisualSubjectType,
    visual_generation_style: 'anime' as CharacterVisualGenerationStyle,
    date_of_birth: '',
    world_frame: 'modern',
    personality_type_code: '' as CharacterPersonalityTypeCode,
    personality_type_rationale: '',
    personality_type_notes: '',
  }
}

function emptyCompanion(): CharacterCompanion {
  return {
    id: null,
    name: '',
    role: '',
    brief_profile: '',
    personality_sketch: [],
    relationship_snippet: '',
  }
}

const form = ref(emptyForm())
const initialRelationship = ref(emptyInitialRelationshipForm())
const personalityTypeSource = ref<CharacterPersonalityTypeSource>('unset')
const nameCandidates = ref<CharacterDraftNameCandidate[]>([])
const companions = ref<CharacterCompanion[]>([])
const saving = ref(false)
const intakeLoading = ref(false)
const intakeRound = ref(0)
const intakeQuestions = ref<CharacterCreationIntakeQuestion[]>([])
const intakeWarnings = ref<CharacterCreationIntakeWarning[]>([])
const intakeChecked = ref(false)
const intakePassed = ref(false)
const intakeStale = ref(false)
const creationPhase = ref<'idle' | 'creating' | 'preparing'>('idle')
const errorMsg = ref<string | null>(null)

const submitLabel = computed(() => {
  if (creationPhase.value === 'preparing') return t('characterCreate.preparing')
  return saving.value ? t('characterCreate.creating') : t('characterCreate.createAction')
})

const intakeBlocksCreate = computed(() => (
  shouldBlockCreateForIntake(intakeWarnings.value, { stale: intakeStale.value })
))

const intakeActionLabel = computed(() => {
  if (intakeLoading.value) return t('characterCreate.initialRelationship.analyzing')
  return intakeChecked.value
    ? t('characterCreate.initialRelationship.analyzeAgainAction')
    : t('characterCreate.initialRelationship.analyzeAction')
})

const unplacedIntakeQuestions = computed(() => intakeQuestions.value.filter(question => (
  !RELATIONSHIP_INTAKE_FIELDS.has(canonicalInitialRelationshipField(question.field))
)))

// --- AI 草稿 ---
const draftOpen = ref(false)
const draftPrompt = ref('')
const draftImage = ref<File | null>(null)
const draftImagePreview = ref<string | null>(null)
const draftLoading = ref(false)
const draftIntakeLoading = ref(false)
const draftError = ref<string | null>(null)

const STATUS_ROTATE_MS = 5000
const STATUS_KEYS = {
  draft: [
    'readingPrompt',
    'sketchingSilhouette',
    'findingVoice',
    'arrangingTraits',
    'imaginingHome',
    'gatheringCompanions',
    'checkingIdentity',
    'readingReferenceClues',
    'weavingBackstory',
    'balancingTone',
    'choosingDetails',
    'shapingDailyRhythm',
    'checkingWorldFrame',
    'draftingRelationships',
    'polishingDraft',
  ],
  creating: [
    'savingProfile',
    'settingVoice',
    'stitchingCompanions',
    'preparingBoundaries',
    'placingAspirations',
    'groundingIdentity',
    'checkingWorldFrame',
    'preparingPortraitClues',
    'settingStagePresence',
    'linkingLifeHooks',
    'settingProactiveRhythm',
    'closingCreateLoop',
  ],
  preparing: [
    'arrangingSchedule',
    'settingHome',
    'findingLocation',
    'warmingRoutine',
    'checkingEnergy',
    'settingNearbyPeople',
    'preparingFirstBeat',
    'checkingCurrentActivity',
    'settingSceneMood',
    'placingBelongings',
    'syncingCalendar',
    'choosingNextStop',
    'smoothingOpeningLine',
    'lightingStage',
    'preparingConversationContext',
    'openingDay',
  ],
} as const
type StatusGroup = keyof typeof STATUS_KEYS

const RELATIONSHIP_INTAKE_FIELDS = new Set([
  'relationship_label',
  'known_context',
  'living_arrangement',
  'user_address_name',
  'character_address_name',
  'tone_distance',
  'familiarity_boundary',
  'proactive_cadence_hint',
  'user_profile_notes',
  'profile_interests',
  'profile_routine',
  'profile_life_goals',
])

const statusTick = ref(0)
let statusTimer: number | null = null

const activeStatusGroup = computed<StatusGroup | null>(() => {
  if (draftLoading.value) return 'draft'
  if (creationPhase.value === 'creating') return 'creating'
  if (creationPhase.value === 'preparing') return 'preparing'
  return null
})

const rotatingStatusText = computed(() => {
  const group = activeStatusGroup.value
  if (!group) return ''
  const keys = STATUS_KEYS[group]
  const key = keys[statusTick.value % keys.length]
  return t(`characterCreate.status.${group}.${key}`)
})

const draftProgressHint = computed(() => (
  draftIntakeLoading.value
    ? t('characterCreate.initialRelationship.analyzing')
    : draftLoading.value ? rotatingStatusText.value : ''
))

const draftBusy = computed(() => draftLoading.value || draftIntakeLoading.value)

const draftSubmitLabel = computed(() => {
  if (draftIntakeLoading.value) return t('characterCreate.initialRelationship.analyzing')
  if (draftLoading.value) return t('characterCreate.draft.generating')
  return t('characterCreate.draft.generateAction')
})

const progressHint = computed(() => (
  creationPhase.value === 'idle' ? '' : rotatingStatusText.value
))

function stopStatusTimer() {
  if (statusTimer === null) return
  window.clearInterval(statusTimer)
  statusTimer = null
}

watch(activeStatusGroup, (group) => {
  statusTick.value = 0
  if (!group) {
    stopStatusTimer()
    return
  }
  if (statusTimer !== null) return
  statusTimer = window.setInterval(() => {
    statusTick.value += 1
  }, STATUS_ROTATE_MS)
}, { immediate: true })

watch(form, markIntakeStale, { deep: true })
watch(initialRelationship, markIntakeStale, { deep: true })

onBeforeUnmount(stopStatusTimer)

function setNameCandidate(candidate: CharacterDraftNameCandidate) {
  form.value.name = candidate.name
}

function resetIntakeState() {
  intakeRound.value = 0
  intakeQuestions.value = []
  intakeWarnings.value = []
  intakeChecked.value = false
  intakePassed.value = false
  intakeStale.value = false
}

function markIntakeStale() {
  if (!intakeChecked.value) return
  intakeStale.value = true
  intakePassed.value = false
}

function onPersonalityTypeChanged() {
  personalityTypeSource.value = form.value.personality_type_code ? 'user_explicit' : 'unset'
  if (!form.value.personality_type_code) {
    form.value.personality_type_rationale = ''
    form.value.personality_type_notes = ''
  }
}

function buildPersonalityType(): CharacterPersonalityType {
  const code = form.value.personality_type_code
  const source: CharacterPersonalityTypeSource = code ? personalityTypeSource.value : 'unset'
  return {
    system: 'mbti_16' as const,
    code,
    source,
    confidence: code ? 1 : 0,
    rationale: form.value.personality_type_rationale.trim(),
    consistency_notes: splitList(form.value.personality_type_notes),
  }
}

function buildInitialRelationship() {
  return buildInitialRelationshipPayload(initialRelationship.value)
}

function buildIntakeDraft() {
  return {
    name: form.value.name.trim(),
    summary: form.value.summary.trim(),
    personality: splitList(form.value.personality),
    interests: splitList(form.value.interests),
    speaking_style: form.value.speaking_style.trim(),
    boundaries: splitList(form.value.boundaries),
    aspirations: splitList(form.value.aspirations),
    personality_type_code: form.value.personality_type_code,
    personality_type_rationale: form.value.personality_type_rationale.trim(),
  }
}

async function runCreationIntake(): Promise<boolean> {
  intakeLoading.value = true
  errorMsg.value = null
  try {
    const analysis = await analyzeCharacterCreationIntake({
      character_draft: buildIntakeDraft(),
      relationship: buildInitialRelationship(),
      current_locale: String(locale.value),
      round_index: intakeRound.value,
    })
    intakeChecked.value = true
    intakeStale.value = false
    intakeQuestions.value = analysis.questions ?? []
    intakeWarnings.value = analysis.warnings ?? []
    const hasBlockingWarning = intakeWarnings.value.some(warning => warning.blocking)
    intakePassed.value = !hasBlockingWarning && analysis.can_create
    if (intakePassed.value) {
      return true
    }
    intakeRound.value = nextIntakeRound(intakeRound.value)
    errorMsg.value = t('characterCreate.initialRelationship.needsReview')
    return false
  } catch {
    intakeQuestions.value = []
    intakeWarnings.value = []
    intakePassed.value = false
    errorMsg.value = t('characterCreate.initialRelationship.analysisFailed')
    return false
  } finally {
    intakeLoading.value = false
  }
}

function applyIntakeSuggestion(field: string, suggestion: string) {
  const value = suggestion.trim()
  if (!value) return
  if (field === 'personality_type') {
    form.value.personality_type_notes = appendText(form.value.personality_type_notes, value)
    removeAnsweredIntakeQuestion(field)
    markIntakeStale()
    return
  }
  applyInitialRelationshipSuggestion(initialRelationship.value, field, value, t('common.sentenceJoiner'))
  removeAnsweredIntakeQuestion(field)
  markIntakeStale()
  errorMsg.value = null
}

function setLivingArrangement(value: string) {
  initialRelationship.value.living_arrangement = value
  removeAnsweredIntakeQuestion('living_arrangement')
  markIntakeStale()
  errorMsg.value = null
}

function removeAnsweredIntakeQuestion(field: string) {
  const normalized = canonicalInitialRelationshipField(field)
  intakeQuestions.value = intakeQuestions.value.filter(question => (
    canonicalInitialRelationshipField(question.field) !== normalized
  ))
}

function intakeQuestionsFor(field: string): CharacterCreationIntakeQuestion[] {
  return intakeQuestions.value.filter(question => (
    canonicalInitialRelationshipField(question.field) === field
  ))
}

function appendText(current: string, value: string): string {
  const existing = current.trim()
  if (!existing) return value
  return existing.includes(value) ? existing : `${existing}${t('common.sentenceJoiner')}${value}`
}

function openDraft() {
  draftOpen.value = true
  draftError.value = null
}

function closeDraft() {
  draftOpen.value = false
  draftPrompt.value = ''
  clearDraftImage()
  draftError.value = null
}

function onDraftImageChange(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0] ?? null
  clearDraftImage()
  if (file) {
    draftImage.value = file
    draftImagePreview.value = URL.createObjectURL(file)
  }
}

function clearDraftImage() {
  if (draftImagePreview.value) {
    URL.revokeObjectURL(draftImagePreview.value)
  }
  draftImage.value = null
  draftImagePreview.value = null
}

async function submitDraft() {
  const prompt = draftPrompt.value.trim()
  if (!prompt && !draftImage.value) {
    draftError.value = t('characterCreate.errors.draftInputRequired')
    return
  }
  draftLoading.value = true
  draftIntakeLoading.value = false
  draftError.value = null
  try {
    const draft = await generateCharacterDraft({ prompt, image: draftImage.value })
    nameCandidates.value = draft.name_candidates ?? []
    form.value = {
      name: draft.name || form.value.name,
      summary: draft.summary || form.value.summary,
      personality: (draft.personality ?? []).join(', '),
      interests: (draft.interests ?? []).join(', '),
      speaking_style: draft.speaking_style || form.value.speaking_style,
      boundaries: (draft.boundaries ?? []).join(', '),
      aspirations: (draft.aspirations ?? []).join(', '),
      appearance: draft.appearance || form.value.appearance,
      gender_identity: draft.gender_identity || form.value.gender_identity,
      third_person_pronoun: draft.third_person_pronoun || form.value.third_person_pronoun,
      visual_gender_presentation: draft.visual_gender_presentation || form.value.visual_gender_presentation,
      visual_subject_type: draft.visual_subject_type || form.value.visual_subject_type,
      visual_generation_style: form.value.visual_generation_style,
      date_of_birth: draft.date_of_birth || form.value.date_of_birth,
      world_frame: draft.world_frame || form.value.world_frame,
      personality_type_code: draft.personality_type?.code || form.value.personality_type_code,
      personality_type_rationale: draft.personality_type?.rationale || form.value.personality_type_rationale,
      personality_type_notes: (draft.personality_type?.consistency_notes ?? []).join(', ') || form.value.personality_type_notes,
    }
    if (draft.personality_type?.code) {
      personalityTypeSource.value = draft.personality_type.source
    }
    // 草稿帶回來的 NPC 直接覆蓋目前的空白清單；如果使用者已先手動加
    // 過幾位，後面那批就保留下來附加上去 —— AI 建議 + 手動建立可以
    // 共存，避免「點 AI 草稿就把我剛打的兩個同伴弄不見」的小驚嚇。
    if (draft.companions && draft.companions.length) {
      const seen = new Set(companions.value.map(c => c.name).filter(Boolean))
      for (const npc of draft.companions) {
        if (!npc.name || seen.has(npc.name)) continue
        seen.add(npc.name)
        companions.value.push({
          id: null,
          name: npc.name,
          role: npc.role || '',
          brief_profile: npc.brief_profile || '',
          personality_sketch: npc.personality_sketch ?? [],
          relationship_snippet: npc.relationship_snippet || '',
        })
      }
    }
    draftLoading.value = false
    draftIntakeLoading.value = true
    await runCreationIntake()
    closeDraft()
  } catch (err) {
    draftError.value = err instanceof Error ? err.message : t('characterCreate.errors.draftFailed')
  } finally {
    draftLoading.value = false
    draftIntakeLoading.value = false
  }
}

function addCompanion() {
  companions.value.push(emptyCompanion())
}

function removeCompanion(index: number) {
  companions.value.splice(index, 1)
}

function companionSketchText(c: CharacterCompanion): string {
  return (c.personality_sketch ?? []).join(', ')
}

function updateCompanionSketch(index: number, raw: string) {
  const items = raw.split(',').map(v => v.trim()).filter(Boolean)
  companions.value[index].personality_sketch = items
}

async function submit() {
  const name = form.value.name.trim()
  if (!name) {
    errorMsg.value = t('characterCreate.errors.nameRequired')
    return
  }
  saving.value = true
  errorMsg.value = null
  if (intakeBlocksCreate.value) {
    errorMsg.value = t('characterCreate.initialRelationship.needsReview')
    saving.value = false
    return
  }
  creationPhase.value = 'creating'
  try {
    const req: CreateCharacterRequest = {
      name,
      summary: form.value.summary,
      personality: splitList(form.value.personality),
      interests: splitList(form.value.interests),
      speaking_style: form.value.speaking_style || 'natural',
      boundaries: splitList(form.value.boundaries),
      aspirations: splitList(form.value.aspirations),
      appearance: form.value.appearance,
      gender_identity: form.value.gender_identity.trim(),
      third_person_pronoun: form.value.third_person_pronoun.trim(),
      visual_gender_presentation: form.value.visual_gender_presentation.trim(),
      visual_subject_type: form.value.visual_subject_type,
      visual_generation_style: form.value.visual_generation_style,
      date_of_birth: form.value.date_of_birth.trim() || null,
      initial_state: {
        emotion: 'neutral',
        affection: 50,
        fatigue: 0,
        trust: 50,
        energy: 100,
      },
      proactive_enabled: true,
      proactive_daily_limit: 3,
      proactive_cooldown_minutes: 30,
      feed_daily_limit: 3,
      world_frame: form.value.world_frame,
      personality_type: buildPersonalityType(),
      initial_relationship: buildInitialRelationship(),
      companions: companions.value
        .filter(c => c.name.trim())
        .map(c => ({
          id: c.id,
          name: c.name.trim(),
          role: c.role.trim(),
          brief_profile: c.brief_profile.trim(),
          personality_sketch: c.personality_sketch
            .map(p => p.trim())
            .filter(Boolean),
          relationship_snippet: c.relationship_snippet.trim(),
        })),
    }
    const created = await createCharacter(req)
    creationPhase.value = 'preparing'
    try {
      await getCurrentActivity(created.id)
    } catch {
      // The backend also queued the same warmup. Keep the character
      // creation successful even if this foreground probe is interrupted.
    }
    emit('created', created)
    // 重置 form 以便下次開啟時是乾淨的
    form.value = emptyForm()
    initialRelationship.value = emptyInitialRelationshipForm()
    personalityTypeSource.value = 'unset'
    resetIntakeState()
    nameCandidates.value = []
    companions.value = []
    emit('close')
  } catch (err) {
    errorMsg.value = err instanceof Error
      ? t('characterCreate.errors.createFailedWithReason', { reason: err.message })
      : t('characterCreate.errors.createFailed')
  } finally {
    saving.value = false
    creationPhase.value = 'idle'
  }
}

function cancel() {
  if (saving.value) return
  form.value = emptyForm()
  initialRelationship.value = emptyInitialRelationshipForm()
  personalityTypeSource.value = 'unset'
  resetIntakeState()
  nameCandidates.value = []
  companions.value = []
  errorMsg.value = null
  emit('close')
}
</script>

<template>
  <div class="create-modal-backdrop" @click.self="cancel">
    <div class="create-modal" role="dialog" :aria-label="t('characterCreate.title')">
      <div class="modal-header">
        <h3>{{ t('characterCreate.title') }}</h3>
        <button class="modal-close" :aria-label="t('common.actions.close')" @click="cancel">×</button>
      </div>

      <div class="modal-body">
        <button
          type="button"
          class="btn-ai-draft"
          :disabled="saving"
          @click="openDraft"
        >{{ t('characterCreate.aiDraftAction') }}</button>

        <div class="form-section">
          <label class="field-label">{{ t('characterCreate.fields.name.label') }} <span class="required">*</span></label>
          <input
            v-model="form.name"
            class="field-input"
            :placeholder="t('characterCreate.fields.name.placeholder')"
            autofocus
          />
          <div v-if="nameCandidates.length" class="name-candidates">
            <button
              v-for="candidate in nameCandidates"
              :key="candidate.name"
              type="button"
              class="name-candidate"
              @click="setNameCandidate(candidate)"
            >
              <span>{{ candidate.name }}</span>
              <small>{{ candidate.rationale }}</small>
            </button>
          </div>

          <label class="field-label">{{ t('characterCreate.fields.summary.label') }}</label>
          <textarea
            v-model="form.summary"
            class="field-textarea"
            rows="2"
            :placeholder="t('characterCreate.fields.summary.placeholder')"
          />

          <label class="field-label">{{ t('characterCreate.fields.personality.label') }}</label>
          <input v-model="form.personality" class="field-input" :placeholder="t('characterCreate.fields.personality.placeholder')" />

          <label class="field-label">{{ t('characterCreate.fields.interests.label') }}</label>
          <input v-model="form.interests" class="field-input" :placeholder="t('characterCreate.fields.interests.placeholder')" />

          <label class="field-label">{{ t('characterCreate.fields.speakingStyle.label') }}</label>
          <input v-model="form.speaking_style" class="field-input" :placeholder="t('characterCreate.fields.speakingStyle.placeholder')" />

          <label class="field-label">{{ t('characterCreate.personalityType.label') }}</label>
          <select
            v-model="form.personality_type_code"
            class="field-select"
            @change="onPersonalityTypeChanged"
          >
            <option value="">{{ t('characterCreate.personalityType.unset') }}</option>
            <option
              v-for="code in PERSONALITY_TYPE_CODES"
              :key="code"
              :value="code"
            >
              {{ code }}
            </option>
          </select>
          <textarea
            v-if="form.personality_type_code"
            v-model="form.personality_type_rationale"
            class="field-textarea"
            rows="2"
            :placeholder="t('characterCreate.personalityType.rationalePlaceholder')"
          />
          <input
            v-if="form.personality_type_code"
            v-model="form.personality_type_notes"
            class="field-input"
            :placeholder="t('characterCreate.personalityType.notesPlaceholder')"
          />

          <label class="field-label">{{ t('characterCreate.fields.appearance.label') }}</label>
          <textarea
            v-model="form.appearance"
            class="field-textarea"
            rows="3"
            :placeholder="t('characterCreate.fields.appearance.placeholder')"
          />

          <CharacterIdentityFields
            v-model:gender-identity="form.gender_identity"
            v-model:third-person-pronoun="form.third_person_pronoun"
            v-model:visual-gender-presentation="form.visual_gender_presentation"
            v-model:visual-subject-type="form.visual_subject_type"
            v-model:visual-generation-style="form.visual_generation_style"
          />

          <label class="field-label">{{ t('characterCreate.fields.boundaries.label') }}</label>
          <input v-model="form.boundaries" class="field-input" :placeholder="t('characterCreate.fields.boundaries.placeholder')" />

          <label class="field-label">{{ t('characterCreate.fields.aspirations.label') }}</label>
          <input
            v-model="form.aspirations"
            class="field-input"
            :placeholder="t('characterCreate.fields.aspirations.placeholder')"
          />

          <label class="field-label">{{ t('characterCreate.fields.dateOfBirth.label') }}</label>
          <input
            v-model="form.date_of_birth"
            type="date"
            class="field-input"
          />
          <div class="field-hint">
            {{ t('characterCreate.fields.dateOfBirth.hint') }}
          </div>

          <label class="field-label">{{ t('characterCreate.fields.worldFrame.label') }}</label>
          <select v-model="form.world_frame" class="field-select">
            <option value="modern">{{ t('characterCreate.fields.worldFrame.options.modern') }}</option>
            <option value="fantasy">{{ t('characterCreate.fields.worldFrame.options.fantasy') }}</option>
            <option value="school">{{ t('characterCreate.fields.worldFrame.options.school') }}</option>
            <option value="custom">{{ t('characterCreate.fields.worldFrame.options.custom') }}</option>
          </select>
          <div class="field-hint">
            {{ t('characterCreate.fields.worldFrame.hint') }}
          </div>

          <section class="relationship-section">
            <div class="relationship-header">
              <h4>{{ t('characterCreate.initialRelationship.title') }}</h4>
              <UiButton
                size="sm"
                variant="ghost"
                :loading="intakeLoading"
                :disabled="saving"
                @click="runCreationIntake"
              >
                {{ intakeActionLabel }}
              </UiButton>
            </div>
            <div class="field-hint">
              {{ t('characterCreate.initialRelationship.hint') }}
            </div>
            <div
              v-if="intakePassed && !intakeQuestions.length && !intakeWarnings.length"
              class="intake-ready"
              role="status"
            >
              {{ t('characterCreate.initialRelationship.ready') }}
            </div>
            <div
              v-if="unplacedIntakeQuestions.length || intakeWarnings.length"
              class="intake-feedback"
            >
              <div
                v-for="warning in intakeWarnings"
                :key="`${warning.kind}-${warning.message}`"
                class="intake-warning"
              >
                {{ warning.message }}
              </div>
              <div
                v-for="question in unplacedIntakeQuestions"
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
              v-model="initialRelationship.relationship_label"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.relationshipPlaceholder')"
            />
            <div v-if="intakeQuestionsFor('relationship_label').length" class="intake-field-questions">
              <div
                v-for="question in intakeQuestionsFor('relationship_label')"
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
            <div class="relationship-grid">
              <label>
                <span class="field-label">{{ t('characterCreate.initialRelationship.userAddress') }}</span>
                <input
                  v-model="initialRelationship.user_address_name"
                  class="field-input"
                  :placeholder="t('characterCreate.initialRelationship.userAddressPlaceholder')"
                />
                <div v-if="intakeQuestionsFor('user_address_name').length" class="intake-field-questions">
                  <div
                    v-for="question in intakeQuestionsFor('user_address_name')"
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
              </label>
              <label>
                <span class="field-label">{{ t('characterCreate.initialRelationship.characterAddress') }}</span>
                <input
                  v-model="initialRelationship.character_address_name"
                  class="field-input"
                  :placeholder="t('characterCreate.initialRelationship.characterAddressPlaceholder')"
                />
                <div v-if="intakeQuestionsFor('character_address_name').length" class="intake-field-questions">
                  <div
                    v-for="question in intakeQuestionsFor('character_address_name')"
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
              </label>
            </div>
            <label class="field-label">{{ t('characterCreate.initialRelationship.knownContext') }}</label>
            <textarea
              v-model="initialRelationship.known_context"
              class="field-textarea"
              rows="2"
              :placeholder="t('characterCreate.initialRelationship.knownContextPlaceholder')"
            />
            <div v-if="intakeQuestionsFor('known_context').length" class="intake-field-questions">
              <div
                v-for="question in intakeQuestionsFor('known_context')"
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
            <label class="field-label">{{ t('characterCreate.initialRelationship.livingArrangement') }}</label>
            <div class="relationship-chips" role="group">
              <button
                v-for="option in ['together', 'nearby', 'separate', 'unset']"
                :key="option"
                type="button"
                class="relationship-chip"
                :class="{ 'relationship-chip--active': initialRelationship.living_arrangement === t(`characterCreate.initialRelationship.livingOptions.${option}`) || (option === 'unset' && !initialRelationship.living_arrangement) }"
                @click="setLivingArrangement(option === 'unset' ? '' : t(`characterCreate.initialRelationship.livingOptions.${option}`))"
              >
                {{ t(`characterCreate.initialRelationship.livingOptions.${option}`) }}
              </button>
            </div>
            <input
              v-model="initialRelationship.living_arrangement"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.livingPlaceholder')"
            />
            <div v-if="intakeQuestionsFor('living_arrangement').length" class="intake-field-questions">
              <div
                v-for="question in intakeQuestionsFor('living_arrangement')"
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
            <label class="field-label">{{ t('characterCreate.initialRelationship.toneDistance') }}</label>
            <input
              v-model="initialRelationship.tone_distance"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.toneDistancePlaceholder')"
            />
            <div v-if="intakeQuestionsFor('tone_distance').length" class="intake-field-questions">
              <div
                v-for="question in intakeQuestionsFor('tone_distance')"
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
            <label class="field-label">{{ t('characterCreate.initialRelationship.boundary') }}</label>
            <input
              v-model="initialRelationship.familiarity_boundary"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.boundaryPlaceholder')"
            />
            <div v-if="intakeQuestionsFor('familiarity_boundary').length" class="intake-field-questions">
              <div
                v-for="question in intakeQuestionsFor('familiarity_boundary')"
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
            <label class="field-label">{{ t('characterCreate.initialRelationship.scheduleLabel') }}</label>
            <select
              v-model="initialRelationship.schedule_involvement_policy"
              class="field-select"
            >
              <option value="none">{{ t('characterCreate.initialRelationship.scheduleOptions.none') }}</option>
              <option value="mention_only">{{ t('characterCreate.initialRelationship.scheduleOptions.mentionOnly') }}</option>
              <option value="invite_required">{{ t('characterCreate.initialRelationship.scheduleOptions.inviteRequired') }}</option>
              <option value="shared_allowed">{{ t('characterCreate.initialRelationship.scheduleOptions.sharedAllowed') }}</option>
            </select>
            <label class="relationship-checkbox">
              <input v-model="initialRelationship.proactive_permission" type="checkbox" />
              <span class="relationship-checkbox__text">{{ t('characterCreate.initialRelationship.proactivePermission') }}</span>
            </label>
            <input
              v-if="initialRelationship.proactive_permission"
              v-model="initialRelationship.proactive_cadence_hint"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.proactiveCadencePlaceholder')"
            />
            <div v-if="intakeQuestionsFor('proactive_cadence_hint').length" class="intake-field-questions">
              <div
                v-for="question in intakeQuestionsFor('proactive_cadence_hint')"
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
            <div class="relationship-grid">
              <label>
                <span class="field-label">{{ t('characterCreate.initialRelationship.profileInterests') }}</span>
                <input
                  v-model="initialRelationship.profile_interests"
                  class="field-input"
                  :placeholder="t('characterCreate.initialRelationship.profileInterestsPlaceholder')"
                />
                <div v-if="intakeQuestionsFor('profile_interests').length" class="intake-field-questions">
                  <div
                    v-for="question in intakeQuestionsFor('profile_interests')"
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
              </label>
              <label>
                <span class="field-label">{{ t('characterCreate.initialRelationship.profileRoutine') }}</span>
                <input
                  v-model="initialRelationship.profile_routine"
                  class="field-input"
                  :placeholder="t('characterCreate.initialRelationship.profileRoutinePlaceholder')"
                />
                <div v-if="intakeQuestionsFor('profile_routine').length" class="intake-field-questions">
                  <div
                    v-for="question in intakeQuestionsFor('profile_routine')"
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
              </label>
            </div>
            <input
              v-model="initialRelationship.profile_life_goals"
              class="field-input"
              :placeholder="t('characterCreate.initialRelationship.profileGoalsPlaceholder')"
            />
            <div v-if="intakeQuestionsFor('profile_life_goals').length" class="intake-field-questions">
              <div
                v-for="question in intakeQuestionsFor('profile_life_goals')"
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
            <textarea
              v-model="initialRelationship.user_profile_notes"
              class="field-textarea"
              rows="2"
              :placeholder="t('characterCreate.initialRelationship.notesPlaceholder')"
            />
            <div v-if="intakeQuestionsFor('user_profile_notes').length" class="intake-field-questions">
              <div
                v-for="question in intakeQuestionsFor('user_profile_notes')"
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
          </section>

          <div class="companions-section">
            <div class="companions-header">
              <label class="field-label">{{ t('characterCreate.companions.title') }}</label>
              <button
                type="button"
                class="btn-companion-add"
                :disabled="saving"
                @click="addCompanion"
              >{{ t('characterCreate.companions.addAction') }}</button>
            </div>
            <div class="field-hint">
              {{ t('characterCreate.companions.hint') }}
            </div>

            <div
              v-for="(npc, index) in companions"
              :key="index"
              class="companion-card"
            >
              <div class="companion-row companion-row--head">
                <input
                  v-model="npc.name"
                  class="field-input companion-name"
                  :placeholder="t('characterCreate.companions.namePlaceholder')"
                />
                <input
                  v-model="npc.role"
                  class="field-input companion-role"
                  :placeholder="t('characterCreate.companions.rolePlaceholder')"
                />
                <button
                  type="button"
                  class="btn-companion-remove"
                  :disabled="saving"
                  @click="removeCompanion(index)"
                  :aria-label="t('characterCreate.companions.removeLabel')"
                >×</button>
              </div>
              <input
                v-model="npc.brief_profile"
                class="field-input"
                :placeholder="t('characterCreate.companions.profilePlaceholder')"
              />
              <input
                :value="companionSketchText(npc)"
                class="field-input"
                :placeholder="t('characterCreate.companions.sketchPlaceholder')"
                @input="
                  updateCompanionSketch(
                    index,
                    ($event.target as HTMLInputElement).value,
                  )
                "
              />
              <input
                v-model="npc.relationship_snippet"
                class="field-input"
                :placeholder="t('characterCreate.companions.relationshipPlaceholder')"
              />
            </div>

            <div v-if="!companions.length" class="companions-empty">
              {{ t('characterCreate.companions.empty') }}
            </div>
          </div>
        </div>

        <div v-if="errorMsg" class="error-msg">{{ errorMsg }}</div>
      </div>

      <div class="modal-actions">
        <UiButton :disabled="saving" @click="cancel">{{ t('common.actions.cancel') }}</UiButton>
        <UiButton
          variant="primary"
          :loading="saving"
          :disabled="!form.name.trim() || intakeLoading"
          @click="submit"
        >{{ submitLabel }}</UiButton>
      </div>
      <div v-if="progressHint" class="creation-progress status-carousel" role="status" aria-live="polite">
        {{ progressHint }}
      </div>

      <!-- AI 草稿子 modal -->
      <div v-if="draftOpen" class="draft-modal-backdrop" @click.self="closeDraft">
        <div class="draft-modal">
          <div class="modal-header">
            <h3>{{ t('characterCreate.draft.title') }}</h3>
            <button class="modal-close" :aria-label="t('common.actions.close')" @click="closeDraft">×</button>
          </div>

          <div class="modal-body">
            <label class="field-label">{{ t('characterCreate.draft.promptLabel') }}</label>
            <textarea
              v-model="draftPrompt"
              class="field-textarea"
              rows="4"
              :placeholder="t('characterCreate.draft.promptPlaceholder')"
            />

            <label class="field-label">{{ t('characterCreate.draft.imageLabel') }}</label>
            <div class="draft-image-row">
              <input
                type="file"
                accept="image/*"
                class="draft-image-input"
                @change="onDraftImageChange"
              />
              <button
                v-if="draftImage"
                type="button"
                class="draft-image-clear"
                @click="clearDraftImage"
              >{{ t('common.actions.remove') }}</button>
            </div>
            <img
              v-if="draftImagePreview"
              :src="draftImagePreview"
              :alt="t('characterCreate.draft.previewAlt')"
              class="draft-image-preview"
            />
            <div class="field-hint">
              {{ t('characterCreate.draft.imageHint') }}
            </div>

            <div v-if="draftError" class="error-msg">{{ draftError }}</div>
            <div v-if="draftProgressHint" class="draft-progress status-carousel" role="status" aria-live="polite">
              {{ draftProgressHint }}
            </div>
          </div>

          <div class="modal-actions">
            <UiButton :disabled="draftBusy" @click="closeDraft">{{ t('common.actions.cancel') }}</UiButton>
            <UiButton variant="primary" :loading="draftBusy" @click="submitDraft">
              {{ draftSubmitLabel }}
            </UiButton>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.create-modal-backdrop,
.draft-modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.draft-modal-backdrop {
  z-index: 1100;
}

.create-modal,
.draft-modal {
  width: min(560px, 100%);
  max-height: calc(100vh - 40px);
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.45);
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-border);
}

.modal-header h3 {
  margin: 0;
  font-size: 15px;
  color: var(--color-primary-light);
}

.modal-close {
  background: none;
  border: none;
  color: var(--color-text-secondary);
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}

.modal-close:hover {
  color: var(--color-text);
}

.modal-body {
  padding: 16px 18px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.form-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.name-candidates {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 2px 0 8px;
}

.name-candidate {
  max-width: 100%;
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 6px 8px;
  border: 1px solid rgba(183, 93, 63, 0.32);
  border-radius: 6px;
  background: rgba(183, 93, 63, 0.08);
  color: var(--color-text);
  cursor: pointer;
  text-align: left;
}

.name-candidate:hover {
  background: rgba(183, 93, 63, 0.16);
  border-color: rgba(183, 93, 63, 0.48);
}

.name-candidate span {
  font-size: 13px;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.name-candidate small {
  max-width: 220px;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.required {
  color: var(--color-primary-light);
}

.error-msg {
  padding: 8px 10px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.4);
  border-radius: 6px;
  color: #ff8a75;
  font-size: 12px;
  margin-top: 8px;
}

.modal-actions {
  display: flex;
  gap: 8px;
  padding: 12px 18px;
  border-top: 1px solid var(--color-border);
  justify-content: flex-end;
}

.creation-progress {
  padding: 0 18px 12px;
}

.draft-progress {
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid rgba(183, 93, 63, 0.22);
  border-radius: 6px;
  background: rgba(183, 93, 63, 0.08);
}

.status-carousel {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.status-carousel::before {
  content: '';
  width: 6px;
  height: 6px;
  flex: 0 0 auto;
  border-radius: 999px;
  background: var(--color-primary-light);
  box-shadow: 0 0 0 0 rgba(183, 93, 63, 0.38);
  animation: status-pulse 1.6s ease-in-out infinite;
}

@keyframes status-pulse {
  0%,
  100% {
    opacity: 0.45;
    transform: scale(0.85);
    box-shadow: 0 0 0 0 rgba(183, 93, 63, 0.22);
  }

  50% {
    opacity: 1;
    transform: scale(1);
    box-shadow: 0 0 0 6px rgba(183, 93, 63, 0);
  }
}

.btn-ai-draft {
  width: 100%;
  padding: 10px;
  background: rgba(183, 93, 63, 0.12);
  border: 1px dashed var(--color-primary);
  border-radius: 8px;
  color: var(--color-primary-light);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
  margin-bottom: 4px;
}

.btn-ai-draft:hover:not(:disabled) {
  background: rgba(183, 93, 63, 0.2);
}

.btn-ai-draft:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.draft-image-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.draft-image-input {
  flex: 1;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.draft-image-clear {
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  color: var(--color-text-secondary);
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
}

.draft-image-clear:hover {
  background: rgba(255, 255, 255, 0.12);
  color: var(--color-text);
}

.draft-image-preview {
  max-width: 100%;
  max-height: 200px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  object-fit: contain;
  align-self: center;
}

.relationship-section {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--color-border);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.relationship-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.relationship-section h4 {
  margin: 0;
  color: var(--color-primary-light);
  font-size: 13px;
  line-height: 1.35;
}

.intake-ready {
  padding: 8px 10px;
  border: 1px solid rgba(98, 213, 154, 0.26);
  border-radius: 6px;
  background: rgba(98, 213, 154, 0.08);
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.intake-feedback {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 4px 0;
}

.intake-field-questions {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 2px 0 6px;
}

.intake-warning,
.intake-question {
  padding: 8px 10px;
  border: 1px solid rgba(255, 209, 128, 0.26);
  border-radius: 6px;
  background: rgba(255, 209, 128, 0.07);
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.intake-question p {
  margin: 0;
  overflow-wrap: anywhere;
}

.intake-suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.intake-suggestion {
  padding: 4px 8px;
  border: 1px solid rgba(255, 209, 128, 0.3);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--color-text);
  font-size: 11px;
  cursor: pointer;
  overflow-wrap: anywhere;
}

.intake-suggestion:hover {
  background: rgba(255, 209, 128, 0.12);
}

.relationship-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.relationship-grid label {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.relationship-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.relationship-chip {
  min-height: 28px;
  border: 1px solid rgba(255, 209, 128, 0.24);
  border-radius: 6px;
  padding: 4px 8px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 12px;
}

.relationship-chip--active {
  border-color: rgba(255, 209, 128, 0.58);
  background: rgba(255, 209, 128, 0.12);
  color: var(--color-text);
}

.relationship-checkbox {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.45;
}

.relationship-checkbox input[type='checkbox'] {
  flex: 0 0 auto;
  margin: 2px 0 0;
}

.relationship-checkbox__text {
  min-width: 0;
  flex: 1 1 0;
  white-space: normal;
  overflow-wrap: break-word;
  word-break: normal;
}

.companions-section {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--color-border);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.companions-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.btn-companion-add {
  background: rgba(183, 93, 63, 0.12);
  border: 1px solid var(--color-primary);
  color: var(--color-primary-light);
  border-radius: 4px;
  padding: 3px 10px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.btn-companion-add:hover:not(:disabled) {
  background: rgba(183, 93, 63, 0.2);
}

.btn-companion-add:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.companion-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
}

.companion-row {
  display: flex;
  gap: 6px;
}

.companion-row--head {
  align-items: center;
}

.companion-name {
  flex: 1.2;
}

.companion-role {
  flex: 1;
}

.btn-companion-remove {
  background: none;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  color: var(--color-text-secondary);
  width: 26px;
  height: 28px;
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  flex-shrink: 0;
}

.btn-companion-remove:hover:not(:disabled) {
  background: rgba(231, 76, 60, 0.18);
  border-color: rgba(231, 76, 60, 0.5);
  color: #ff8a75;
}

.btn-companion-remove:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.companions-empty {
  font-size: 12px;
  color: var(--color-text-secondary);
  padding: 8px 10px;
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
}

@media (max-width: 520px) {
  .relationship-grid {
    grid-template-columns: 1fr;
  }
}
</style>
