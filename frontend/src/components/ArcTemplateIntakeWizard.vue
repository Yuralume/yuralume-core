<script setup lang="ts">
/**
 * 劇情骨架建立 wizard（Phase 2.7 frontend of SCENE_BEAT_PLAN）。
 *
 * 6 步驟 modal，引導操作者把一句話 pitch 收斂成完整 arc template：
 *
 *   1. Pitch — 一句話講想寫什麼
 *   2. Meta — 確認 title / theme / tone / world_frames
 *   3. Premise — 起點＋終點壓 60–120 字
 *   4. Rhythm — 選節奏 pattern + 天數 + beat 數，生空殼 beats
 *   5. Beats — 逐 beat 編輯（候選 chips + 一鍵 summary）
 *   6. Review — 預覽完整 YAML 結構 + 存檔
 *
 * Fast-path：第 1 步可按「全部交給 AI 一次寫完」直接拿完整 draft 跳到第 6 步。
 *
 * 所有 LLM 呼叫 fail-soft：失敗只記 errorMsg，不擋使用者手填。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import {
  blankBeatDraft,
  type BeatContextPayload,
  type BeatDraftPayload,
  type RhythmPattern,
  type ScaffoldsResponse,
  type SuggestBeatOptionsResponse,
  type SuggestMetaResponse,
  type TemplateDraftPayload,
} from '@/types/arcTemplateIntake'
import {
  condensePremise,
  generateBeatSummary,
  generateFullDraft,
  getArcTemplateScaffolds,
  saveArcTemplate,
  suggestBeatOptions,
  suggestMeta,
} from '@/utils/api/arcTemplates'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import {
  clearWizardDraft,
  draftHasWizardContent,
  loadWizardDraft,
  saveWizardDraft,
} from '@/utils/arcWizardDraft'

const emit = defineEmits<{
  /** Wizard 完成存檔；參數是後端回傳的新 template id。 */
  (e: 'saved', templateId: string): void
  (e: 'close'): void
}>()

const props = defineProps<{
  initialDraft?: TemplateDraftPayload | null
  targetCharacterId?: string | null
}>()

const { t, te } = useI18n()
const confirmDiscard = useConfirmDialog()

/**
 * Translate a scaffold `id` via the trilingual bundle (plan #4 / D6).
 * The backend now returns stable ids only; fall back to any legacy
 * `fallback` string (older backend) or the raw id so nothing renders
 * blank.
 */
function scaffoldText(
  group: 'themes' | 'tones' | 'sceneTypes' | 'rhythm',
  id: string,
  suffix: 'label' | 'description' = 'label',
  fallback?: string,
): string {
  const key = `story.arcTemplateIntake.scaffolds.${group}.${id}.${suffix}`
  if (te(key)) return t(key)
  return fallback ?? (suffix === 'label' ? id : '')
}

// ----- 步驟與 draft 狀態 ------------------------------------------------

type Step = 1 | 2 | 3 | 4 | 5 | 6
const step = ref<Step>(1)

const pitch = ref('')
const fastPathBusy = ref(false)

// 完整 draft —— wizard 的 single source of truth
const draft = ref<TemplateDraftPayload>({
  id: '',
  title: '',
  premise: '',
  theme: 'custom',
  tone: 'daily',
  duration_days: 14,
  world_frames: [],
  required_traits: [],
  applicability_scope: 'generic',
  target_character_ids: [],
  beats: [],
})

watch(
  () => props.initialDraft,
  (next) => {
    if (!next) return
    draft.value = cloneDraft(next)
    if (!draft.value.id) {
      draft.value.id = slugifyTitle(draft.value.title)
    }
    pitch.value = draft.value.premise || draft.value.title
    step.value = 6
  },
  { immediate: true },
)

watch(
  () => props.targetCharacterId,
  (characterId) => {
    if (!characterId || props.initialDraft) return
    if (draft.value.target_character_ids.length > 0) return
    draft.value.applicability_scope = 'character_bound'
    draft.value.target_character_ids = [characterId]
  },
  { immediate: true },
)

// ----- Draft autosave / crash recovery ---------------------------------
// `close()` guards a backdrop mis-click, but a reload / tab-close / crash
// would still vaporise an unsaved draft. Autosave the in-progress draft and
// offer to restore it on the next open. Recovery logic lives in the tested
// `@/utils/arcWizardDraft` util (pure functions + injected storage).

const draftStorage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null =
  typeof window !== 'undefined' ? window.localStorage : null

// Read the pre-existing draft BEFORE the autosave watch registers, so the
// restore prompt sees real prior work, not a freshly-autosaved blank.
const persistedWizardState = loadWizardDraft(draftStorage)

watch(
  [draft, pitch, step],
  () => saveWizardDraft(draftStorage, {
    draft: draft.value,
    pitch: pitch.value,
    step: step.value,
  }),
  { deep: true },
)

const errorMsg = ref<string | null>(null)
const busy = ref(false)
const saveBusy = ref(false)
const overwriteAsk = ref(false)

// ----- Scaffolds --------------------------------------------------------

const scaffolds = ref<ScaffoldsResponse | null>(null)
onMounted(async () => {
  try {
    scaffolds.value = await getArcTemplateScaffolds()
  } catch (err) {
    errorMsg.value = t('story.arcTemplateIntake.errors.loadOptionsFailed', { reason: formatErr(err) })
  }
})

onMounted(async () => {
  // Offer to restore a draft left over from an unclean exit (reload / crash
  // / tab-close). Skipped when opened from an `initialDraft` (next-season /
  // fusion adaptation), which already carries its own content.
  if (!persistedWizardState || props.initialDraft) return
  const restore = await confirmDiscard({
    content: t('story.arcTemplateIntake.restoreConfirm'),
    okText: t('story.arcTemplateIntake.restoreOk'),
    cancelText: t('story.arcTemplateIntake.restoreDiscard'),
  })
  if (restore) {
    draft.value = cloneDraft(persistedWizardState.draft)
    pitch.value = persistedWizardState.pitch ?? ''
    const restoredStep = persistedWizardState.step
    step.value = (restoredStep >= 1 && restoredStep <= 6 ? restoredStep : 1) as Step
  } else {
    clearWizardDraft(draftStorage)
  }
})

// ----- Step 1: Pitch ----------------------------------------------------

const metaSuggestions = ref<SuggestMetaResponse | null>(null)

async function submitPitch() {
  if (!pitch.value.trim()) {
    errorMsg.value = t('story.arcTemplateIntake.validation.pitchRequired')
    return
  }
  errorMsg.value = null
  busy.value = true
  try {
    metaSuggestions.value = await suggestMeta(pitch.value.trim())
    // 把第一個 suggestion 預填到 draft，操作者可改
    if (metaSuggestions.value.titles[0] && !draft.value.title) {
      draft.value.title = metaSuggestions.value.titles[0]
    }
    if (metaSuggestions.value.themes[0] && draft.value.theme === 'custom') {
      draft.value.theme = metaSuggestions.value.themes[0]
    }
    if (metaSuggestions.value.tones[0] && draft.value.tone === 'daily') {
      draft.value.tone = metaSuggestions.value.tones[0]
    }
    if (metaSuggestions.value.world_frames.length > 0 && draft.value.world_frames.length === 0) {
      draft.value.world_frames = [...metaSuggestions.value.world_frames]
    }
    step.value = 2
  } catch (err) {
    // LLM 失敗也允許進下一步手填
    errorMsg.value = t('story.arcTemplateIntake.errors.suggestMetaFailed', { reason: formatErr(err) })
    step.value = 2
  } finally {
    busy.value = false
  }
}

async function fastPathFullDraft() {
  if (!pitch.value.trim()) {
    errorMsg.value = t('story.arcTemplateIntake.validation.pitchRequired')
    return
  }
  errorMsg.value = null
  fastPathBusy.value = true
  try {
    const result = await generateFullDraft(pitch.value.trim(), '')
    if (result === null) {
      errorMsg.value = t('story.arcTemplateIntake.errors.fullDraftEmpty')
      return
    }
    // 直接套用整份 draft，跳到 review
    draft.value = result
    if (!draft.value.id) {
      draft.value.id = slugifyTitle(draft.value.title)
    }
    step.value = 6
  } catch (err) {
    errorMsg.value = t('story.arcTemplateIntake.errors.fullDraftFailed', { reason: formatErr(err) })
  } finally {
    fastPathBusy.value = false
  }
}

// ----- Step 2: Meta -----------------------------------------------------

function pickTitle(t: string) { draft.value.title = t }
function pickTheme(t: string) { draft.value.theme = t }
function pickTone(t: string) { draft.value.tone = t }
function toggleWorldFrame(f: string) {
  if (draft.value.world_frames.includes(f)) {
    draft.value.world_frames = draft.value.world_frames.filter(x => x !== f)
  } else {
    draft.value.world_frames = [...draft.value.world_frames, f]
  }
}

const idPreview = computed(() => draft.value.id || slugifyTitle(draft.value.title))

function commitMeta() {
  if (!draft.value.title.trim()) {
    errorMsg.value = t('story.arcTemplateIntake.validation.titleRequired')
    return
  }
  if (!draft.value.id.trim()) {
    draft.value.id = slugifyTitle(draft.value.title)
  }
  errorMsg.value = null
  step.value = 3
}

// ----- Step 3: Premise --------------------------------------------------

const startState = ref('')
const endState = ref('')

async function condense() {
  if (!pitch.value.trim()) {
    errorMsg.value = t('story.arcTemplateIntake.validation.pitchNeededForCondense')
    return
  }
  errorMsg.value = null
  busy.value = true
  try {
    const premise = await condensePremise({
      logline: pitch.value.trim(),
      start_state: startState.value.trim(),
      end_state: endState.value.trim(),
      tone: draft.value.tone,
    })
    draft.value.premise = premise
  } catch (err) {
    errorMsg.value = t('story.arcTemplateIntake.errors.condenseFailed', { reason: formatErr(err) })
  } finally {
    busy.value = false
  }
}

function commitPremise() {
  if (!draft.value.premise.trim()) {
    errorMsg.value = t('story.arcTemplateIntake.validation.premiseRequired')
    return
  }
  errorMsg.value = null
  step.value = 4
}

// ----- Step 4: Rhythm ---------------------------------------------------

const selectedRhythm = ref<RhythmPattern | null>(null)

function pickRhythm(r: RhythmPattern) {
  selectedRhythm.value = r
  // 推薦 duration / beat count（取下緣讓使用者再放大）
  const [, durMax] = r.recommended_duration
  const [, beatMax] = r.recommended_beat_count
  draft.value.duration_days = Math.min(draft.value.duration_days, durMax) || durMax
  // 如果還沒手動改過 beat count，套 pattern 的 distribution 數
  if (draft.value.beats.length === 0) {
    draft.value.beats = scaleDistribution(r, draft.value.duration_days, beatMax)
  }
}

function regenerateBeats() {
  if (!selectedRhythm.value) {
    // 沒選 pattern 時用最簡單的均分
    const count = Math.max(3, Math.min(10, draft.value.beats.length || 5))
    draft.value.beats = uniformBeats(count, draft.value.duration_days)
    return
  }
  draft.value.beats = scaleDistribution(
    selectedRhythm.value,
    draft.value.duration_days,
    draft.value.beats.length || 5,
  )
}

function commitRhythm() {
  if (draft.value.beats.length === 0) {
    errorMsg.value = t('story.arcTemplateIntake.validation.beatsRequired')
    return
  }
  errorMsg.value = null
  step.value = 5
}

// ----- Step 5: Beats ----------------------------------------------------

const editingBeatIdx = ref<number | null>(null)
const beatOptions = ref<SuggestBeatOptionsResponse | null>(null)
const beatBusy = ref(false)

function buildBeatContext(idx: number): BeatContextPayload {
  const beat = draft.value.beats[idx]
  return {
    template_title: draft.value.title,
    premise: draft.value.premise,
    theme: draft.value.theme,
    tone: draft.value.tone,
    duration_days: draft.value.duration_days,
    world_frames: draft.value.world_frames,
    beat_position: idx,
    total_beats: draft.value.beats.length,
    day_offset: beat.day_offset,
    tension: beat.tension,
    prior_titles: draft.value.beats.slice(0, idx).map(b => b.title).filter(Boolean),
  }
}

async function suggestForBeat(idx: number) {
  errorMsg.value = null
  beatBusy.value = true
  beatOptions.value = null
  try {
    beatOptions.value = await suggestBeatOptions(buildBeatContext(idx))
  } catch (err) {
    errorMsg.value = t('story.arcTemplateIntake.errors.suggestBeatFailed', { reason: formatErr(err) })
  } finally {
    beatBusy.value = false
  }
}

function applyBeatOption(field: keyof BeatDraftPayload, value: string) {
  if (editingBeatIdx.value === null) return
  const beat = draft.value.beats[editingBeatIdx.value]
  if (field === 'scene_characters') {
    if (!beat.scene_characters.includes(value)) {
      beat.scene_characters = [...beat.scene_characters, value]
    }
  } else if (field === 'location' || field === 'dramatic_question') {
    beat[field] = value
  } else if (field === 'title' || field === 'scene_type') {
    beat[field] = value
  }
}

async function regenSummary(idx: number) {
  errorMsg.value = null
  beatBusy.value = true
  try {
    const summary = await generateBeatSummary(
      draft.value.beats[idx],
      buildBeatContext(idx),
    )
    draft.value.beats[idx].summary = summary
  } catch (err) {
    errorMsg.value = t('story.arcTemplateIntake.errors.summaryFailed', { reason: formatErr(err) })
  } finally {
    beatBusy.value = false
  }
}

function addBeat() {
  const last = draft.value.beats[draft.value.beats.length - 1]
  const seq = draft.value.beats.length
  const day = Math.min(
    draft.value.duration_days,
    last ? Math.min(draft.value.duration_days, last.day_offset + 2) : 0,
  )
  draft.value.beats.push(blankBeatDraft(seq, day))
}

function removeBeat(idx: number) {
  draft.value.beats.splice(idx, 1)
  // 重編 sequence
  draft.value.beats.forEach((b, i) => { b.sequence = i })
  if (editingBeatIdx.value === idx) {
    editingBeatIdx.value = null
    beatOptions.value = null
  }
}

function removeSceneCharacter(idx: number, name: string) {
  draft.value.beats[idx].scene_characters = draft.value.beats[idx].scene_characters
    .filter(n => n !== name)
}

const newCharacterInput = ref('')
function addSceneCharacter(idx: number) {
  const name = newCharacterInput.value.trim()
  if (!name) return
  if (!draft.value.beats[idx].scene_characters.includes(name)) {
    draft.value.beats[idx].scene_characters = [
      ...draft.value.beats[idx].scene_characters,
      name,
    ]
  }
  newCharacterInput.value = ''
}

watch(editingBeatIdx, () => {
  // 切 beat 時清掉 chip 提案，避免錯把上一個的 chip 應用到新的
  beatOptions.value = null
  newCharacterInput.value = ''
})

function commitBeats() {
  // 驗證每個 beat 至少有 title
  const missing = draft.value.beats.findIndex(b => !b.title.trim())
  if (missing >= 0) {
    errorMsg.value = t('story.arcTemplateIntake.validation.beatTitleMissing', { index: missing + 1 })
    editingBeatIdx.value = missing
    return
  }
  errorMsg.value = null
  step.value = 6
}

// ----- Step 6: Review + Save --------------------------------------------

async function save(overwrite = false) {
  if (!draft.value.id.trim()) {
    draft.value.id = slugifyTitle(draft.value.title)
  }
  if (draft.value.beats.length === 0) {
    errorMsg.value = t('story.arcTemplateIntake.validation.atLeastOneBeat')
    return
  }
  if (props.targetCharacterId && draft.value.applicability_scope === 'character_bound') {
    draft.value.target_character_ids = [props.targetCharacterId]
  }
  errorMsg.value = null
  saveBusy.value = true
  overwriteAsk.value = false
  try {
    const result = await saveArcTemplate(draft.value, overwrite)
    clearWizardDraft(draftStorage)
    emit('saved', result.template_id)
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 409) {
      // id 已存在，問操作者要不要覆寫
      overwriteAsk.value = true
      errorMsg.value = t('story.arcTemplateIntake.errors.idExists', { id: draft.value.id })
    } else {
      errorMsg.value = t('story.arcTemplateIntake.errors.saveFailed', { reason: formatErr(err) })
    }
  } finally {
    saveBusy.value = false
  }
}

// ----- Helpers ---------------------------------------------------------

/**
 * Whether the wizard holds unsaved work worth guarding. Includes the
 * one-line pitch, the meta fields, and any authored beats — and also
 * covers an `initialDraft` (next-season / fusion adaptation), which is
 * itself unsaved LLM output.
 */
const hasDraftContent = computed(() =>
  draftHasWizardContent(draft.value, pitch.value),
)

/**
 * Close the wizard, but guard against a mis-click (backdrop / ×) throwing
 * away a whole draft. A stray click used to vaporise minutes of authoring +
 * several LLM calls with no undo; now it asks first, and the autosaved copy
 * is only cleared once the operator confirms the discard.
 */
async function close() {
  if (hasDraftContent.value) {
    const confirmed = await confirmDiscard({
      content: t('story.arcTemplateIntake.discardConfirm'),
      okText: t('story.arcTemplateIntake.discardOk'),
      danger: true,
    })
    if (!confirmed) return
  }
  clearWizardDraft(draftStorage)
  emit('close')
}

function setCharacterBoundScope() {
  if (!props.targetCharacterId) return
  draft.value.applicability_scope = 'character_bound'
  draft.value.target_character_ids = [props.targetCharacterId]
}

function setGenericScope() {
  draft.value.applicability_scope = 'generic'
  draft.value.target_character_ids = []
}

function back() {
  if (step.value > 1) step.value = (step.value - 1) as Step
}

function formatErr(err: unknown): string {
  if (err instanceof Error) return err.message
  return String(err)
}

function slugifyTitle(title: string): string {
  // 中文 title 直接走拼音不切實際；保留中文＋去空白＋小寫，後端 YAML 檔名容錯
  return title
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '_')
    .replace(/[^\w\u4e00-\u9fa5_-]/g, '')
    .slice(0, 64) || `arc_${Date.now().toString(36)}`
}

function cloneDraft(source: TemplateDraftPayload): TemplateDraftPayload {
  return {
    ...source,
    applicability_scope: source.applicability_scope ?? 'generic',
    world_frames: [...source.world_frames],
    required_traits: [...source.required_traits],
    target_character_ids: [...(source.target_character_ids ?? [])],
    beats: source.beats.map(beat => ({
      ...beat,
      scene_characters: [...beat.scene_characters],
    })),
  }
}

function uniformBeats(count: number, days: number): BeatDraftPayload[] {
  const tensions = ['setup', 'rising', 'rising', 'climax', 'falling', 'resolution']
  return Array.from({ length: count }, (_, i) => {
    const beat = blankBeatDraft(i, Math.round((days * i) / Math.max(1, count - 1)))
    beat.tension = tensions[Math.min(i, tensions.length - 1)]
    return beat
  })
}

function scaleDistribution(
  pattern: RhythmPattern,
  days: number,
  targetCount: number,
): BeatDraftPayload[] {
  const ref = pattern.default_distribution_14d
  // 把 14 天的 day_offset 線性縮放到目標 days；如果 targetCount 跟 ref 數一樣就直接 1:1
  if (targetCount === ref.length) {
    return ref.map((b, i) => ({
      ...blankBeatDraft(i, Math.round((b.day_offset * days) / 14)),
      tension: b.tension,
      scene_type: b.scene_type,
    }))
  }
  // targetCount 不同：抽樣 ref 的索引
  return Array.from({ length: targetCount }, (_, i) => {
    const refIdx = Math.min(
      ref.length - 1,
      Math.round((i * (ref.length - 1)) / Math.max(1, targetCount - 1)),
    )
    const r = ref[refIdx]
    return {
      ...blankBeatDraft(i, Math.round((r.day_offset * days) / 14)),
      tension: r.tension,
      scene_type: r.scene_type,
    }
  })
}

function tensionLabel(tension: string): string {
  const key = {
    setup: 'story.arcTemplateIntake.tension.setup',
    rising: 'story.arcTemplateIntake.tension.rising',
    climax: 'story.arcTemplateIntake.tension.climax',
    falling: 'story.arcTemplateIntake.tension.falling',
    resolution: 'story.arcTemplateIntake.tension.resolution',
  }[tension]
  return key ? t(key) : tension
}

function sceneTypeLabel(s: string): string {
  const key = {
    encounter: 'story.arcTemplateIntake.sceneType.encounter',
    revelation: 'story.arcTemplateIntake.sceneType.revelation',
    conflict: 'story.arcTemplateIntake.sceneType.conflict',
    resolution: 'story.arcTemplateIntake.sceneType.resolution',
    interlude: 'story.arcTemplateIntake.sceneType.interlude',
  }[s]
  return key ? t(key) : s
}
</script>

<template>
  <Teleport to="body">
    <div class="modal-backdrop" @click.self="close">
      <div class="wizard" role="dialog" :aria-label="t('story.arcTemplateIntake.ariaLabel')">
        <div class="wiz-header">
          <div>
            <div class="wiz-title display-title">{{ t('story.arcTemplateIntake.title') }}</div>
            <div class="wiz-steps">
              <span :class="{ active: step === 1, done: step > 1 }">{{ t('story.arcTemplateIntake.steps.pitch') }}</span>
              <span :class="{ active: step === 2, done: step > 2 }">{{ t('story.arcTemplateIntake.steps.meta') }}</span>
              <span :class="{ active: step === 3, done: step > 3 }">{{ t('story.arcTemplateIntake.steps.premise') }}</span>
              <span :class="{ active: step === 4, done: step > 4 }">{{ t('story.arcTemplateIntake.steps.rhythm') }}</span>
              <span :class="{ active: step === 5, done: step > 5 }">5 beats</span>
              <span :class="{ active: step === 6 }">{{ t('story.arcTemplateIntake.steps.review') }}</span>
            </div>
          </div>
          <button class="close-btn" @click="close" :aria-label="t('common.actions.close')">×</button>
        </div>

        <div class="wiz-body">
          <!-- ===== Step 1: Pitch ===== -->
          <section v-if="step === 1" class="step">
            <p class="hint">
              {{ t('story.arcTemplateIntake.pitch.hint') }}
            </p>
            <textarea
              v-model="pitch"
              class="field-textarea big-input"
              rows="3"
              :placeholder="t('story.arcTemplateIntake.pitch.placeholder')"
            />
            <div class="step-actions">
              <button
                class="chip-btn alt"
                :disabled="!pitch.trim() || fastPathBusy || busy"
                @click="fastPathFullDraft"
              >{{ fastPathBusy ? t('story.arcTemplateIntake.pitch.fullDraftBusy') : t('story.arcTemplateIntake.pitch.fullDraft') }}</button>
              <button
                class="chip-btn primary"
                :disabled="!pitch.trim() || busy || fastPathBusy"
                @click="submitPitch"
              >{{ busy ? t('story.arcTemplateIntake.pitch.suggesting') : t('story.arcTemplateIntake.pitch.next') }}</button>
            </div>
          </section>

          <!-- ===== Step 2: Meta ===== -->
          <section v-else-if="step === 2" class="step">
            <p class="hint">{{ t('story.arcTemplateIntake.meta.hint') }}</p>

            <label class="field">
              <span class="field-label">{{ t('story.arcTemplateIntake.fields.title') }}</span>
              <input v-model="draft.title" class="field-input" :placeholder="t('story.arcTemplateIntake.meta.titlePlaceholder')" />
              <div v-if="metaSuggestions?.titles.length" class="chip-row">
                <button
                  v-for="title in metaSuggestions.titles"
                  :key="title"
                  type="button"
                  class="chip"
                  :class="{ active: draft.title === title }"
                  @click="pickTitle(title)"
                >{{ title }}</button>
              </div>
            </label>

            <label class="field">
              <span class="field-label">{{ t('story.arcTemplateIntake.fields.theme') }}</span>
              <div class="chip-row">
                <button
                  v-for="theme in scaffolds?.themes ?? []"
                  :key="theme.id"
                  type="button"
                  class="chip"
                  :class="{ active: draft.theme === theme.id }"
                  @click="pickTheme(theme.id)"
                >{{ scaffoldText('themes', theme.id, 'label', theme.label) }}</button>
                <button
                  v-for="theme in metaSuggestions?.themes.filter(x => !(scaffolds?.themes ?? []).some(s => s.id === x)) ?? []"
                  :key="`ai-${theme}`"
                  type="button"
                  class="chip ai"
                  :class="{ active: draft.theme === theme }"
                  @click="pickTheme(theme)"
                >AI: {{ theme }}</button>
              </div>
            </label>

            <label class="field">
              <span class="field-label">{{ t('story.arcTemplateIntake.fields.tone') }}</span>
              <div class="chip-row">
                <button
                  v-for="tone in scaffolds?.tones ?? []"
                  :key="tone.id"
                  type="button"
                  class="chip"
                  :class="{ active: draft.tone === tone.id }"
                  :title="scaffoldText('tones', tone.id, 'description', tone.description)"
                  @click="pickTone(tone.id)"
                >{{ scaffoldText('tones', tone.id, 'label', tone.label) }}</button>
              </div>
              <div class="hint-small">
                {{ t('story.arcTemplateIntake.meta.toneHint') }}
              </div>
            </label>

            <label class="field">
              <span class="field-label">{{ t('story.arcTemplateIntake.fields.worldFrames') }}</span>
              <div class="chip-row">
                <button
                  v-for="f in scaffolds?.world_frames ?? []"
                  :key="f"
                  type="button"
                  class="chip"
                  :class="{ active: draft.world_frames.includes(f) }"
                  @click="toggleWorldFrame(f)"
                >{{ f }}</button>
              </div>
            </label>

            <label class="field">
              <span class="field-label">{{ t('story.arcTemplateIntake.fields.id') }}</span>
              <input
                v-model="draft.id"
                class="field-input mono"
                :placeholder="idPreview"
              />
            </label>

            <div class="step-actions">
              <button class="chip-btn" @click="back">{{ t('story.arcTemplateIntake.actions.back') }}</button>
              <button class="chip-btn primary" @click="commitMeta">{{ t('story.arcTemplateIntake.actions.next') }}</button>
            </div>
          </section>

          <!-- ===== Step 3: Premise ===== -->
          <section v-else-if="step === 3" class="step">
            <p class="hint">
              {{ t('story.arcTemplateIntake.premise.hint') }}
            </p>
            <label class="field">
              <span class="field-label">{{ t('story.arcTemplateIntake.fields.startState') }}</span>
              <textarea
                v-model="startState"
                class="field-textarea"
                rows="2"
                :placeholder="t('story.arcTemplateIntake.premise.startPlaceholder')"
              />
            </label>
            <label class="field">
              <span class="field-label">{{ t('story.arcTemplateIntake.fields.endState') }}</span>
              <textarea
                v-model="endState"
                class="field-textarea"
                rows="2"
                :placeholder="t('story.arcTemplateIntake.premise.endPlaceholder')"
              />
            </label>
            <div class="step-actions">
              <button
                class="chip-btn alt"
                :disabled="busy"
                @click="condense"
              >{{ busy ? t('story.arcTemplateIntake.premise.condensing') : t('story.arcTemplateIntake.premise.condense') }}</button>
            </div>
            <label class="field">
              <span class="field-label">{{ t('story.arcTemplateIntake.fields.premise') }}</span>
              <textarea
                v-model="draft.premise"
                class="field-textarea"
                rows="5"
                :placeholder="t('story.arcTemplateIntake.premise.placeholder')"
              />
              <div class="hint-small">{{ t('story.arcTemplateIntake.premise.charCount', { count: draft.premise.length }) }}</div>
            </label>
            <div class="step-actions">
              <button class="chip-btn" @click="back">{{ t('story.arcTemplateIntake.actions.back') }}</button>
              <button class="chip-btn primary" @click="commitPremise">{{ t('story.arcTemplateIntake.actions.next') }}</button>
            </div>
          </section>

          <!-- ===== Step 4: Rhythm ===== -->
          <section v-else-if="step === 4" class="step">
            <p class="hint">
              {{ t('story.arcTemplateIntake.rhythm.hint') }}
            </p>
            <ul class="rhythm-list">
              <li
                v-for="r in scaffolds?.rhythm_patterns ?? []"
                :key="r.id"
                :class="['rhythm-card', { active: selectedRhythm?.id === r.id }]"
                @click="pickRhythm(r)"
              >
                <div class="rhythm-head">
                  <span class="rhythm-label">{{ scaffoldText('rhythm', r.id, 'label', r.label) }}</span>
                  <span class="rhythm-meta">
                    {{ t('story.arcTemplateIntake.rhythm.rangeMeta', {
                      minDays: r.recommended_duration[0],
                      maxDays: r.recommended_duration[1],
                      minBeats: r.recommended_beat_count[0],
                      maxBeats: r.recommended_beat_count[1],
                    }) }}
                  </span>
                </div>
                <div class="rhythm-desc">{{ scaffoldText('rhythm', r.id, 'description', r.description) }}</div>
              </li>
            </ul>

            <div class="field-row">
              <label class="field-small">
                <span class="field-label">{{ t('story.arcTemplateIntake.fields.durationDays') }}</span>
                <input
                  v-model.number="draft.duration_days"
                  type="number"
                  min="3" max="90"
                  class="field-input"
                />
              </label>
              <button class="chip-btn alt" @click="regenerateBeats">
                {{ t('story.arcTemplateIntake.rhythm.regenerate') }}
              </button>
            </div>

            <div v-if="draft.beats.length > 0" class="beat-overview">
              <div class="hint-small">{{ t('story.arcTemplateIntake.rhythm.generatedShells', { count: draft.beats.length }) }}</div>
              <div class="beat-pills">
                <span
                  v-for="b in draft.beats"
                  :key="b.sequence"
                  class="beat-pill"
                  :title="t('story.arcTemplateIntake.rhythm.beatPillTitle', { day: b.day_offset, tension: b.tension })"
                >
                  D{{ b.day_offset }}・{{ tensionLabel(b.tension) }}
                </span>
              </div>
            </div>

            <div class="step-actions">
              <button class="chip-btn" @click="back">{{ t('story.arcTemplateIntake.actions.back') }}</button>
              <button class="chip-btn primary" @click="commitRhythm">{{ t('story.arcTemplateIntake.rhythm.next') }}</button>
            </div>
          </section>

          <!-- ===== Step 5: Beats ===== -->
          <section v-else-if="step === 5" class="step beats-step">
            <p class="hint">
              {{ t('story.arcTemplateIntake.beats.hint') }}
            </p>

            <ul class="beats-list">
              <li
                v-for="(beat, idx) in draft.beats"
                :key="idx"
                :class="['beat-edit', { open: editingBeatIdx === idx }]"
              >
                <div
                  class="beat-summary-row"
                  @click="editingBeatIdx = editingBeatIdx === idx ? null : idx"
                >
                  <span class="beat-day">{{ t('story.arcTemplateIntake.beats.day', { day: beat.day_offset }) }}</span>
                  <span class="beat-tension">{{ tensionLabel(beat.tension) }}</span>
                  <span class="beat-title-text">{{ beat.title || t('story.arcTemplateIntake.beats.untitled') }}</span>
                  <span v-if="!beat.required" class="optional-pill">{{ t('story.arcTemplateIntake.optional') }}</span>
                  <button
                    class="mini-btn danger"
                    type="button"
                    :title="t('story.arcTemplateIntake.beats.deleteTitle')"
                    @click.stop="removeBeat(idx)"
                  >×</button>
                </div>

                <div v-if="editingBeatIdx === idx" class="beat-form">
                  <div class="field-row">
                    <label class="field-small">
                      <span class="field-label">{{ t('story.arcTemplateIntake.fields.dayOffset') }}</span>
                      <input
                        v-model.number="beat.day_offset"
                        type="number"
                        min="0" :max="draft.duration_days"
                        class="field-input"
                      />
                    </label>
                    <label class="field-small">
                      <span class="field-label">{{ t('story.arcTemplateIntake.fields.tension') }}</span>
                      <select v-model="beat.tension" class="field-select">
                        <option value="setup">{{ tensionLabel('setup') }} setup</option>
                        <option value="rising">{{ tensionLabel('rising') }} rising</option>
                        <option value="climax">{{ tensionLabel('climax') }} climax</option>
                        <option value="falling">{{ tensionLabel('falling') }} falling</option>
                        <option value="resolution">{{ tensionLabel('resolution') }} resolution</option>
                      </select>
                    </label>
                    <label class="field-small">
                      <span class="field-label">{{ t('story.arcTemplateIntake.fields.required') }}</span>
                      <select v-model="beat.required" class="field-select">
                        <option :value="true">{{ t('story.arcTemplateIntake.beats.requiredOption') }}</option>
                        <option :value="false">{{ t('story.arcTemplateIntake.beats.optionalOption') }}</option>
                      </select>
                    </label>
                  </div>

                  <div class="field-row">
                    <button
                      class="chip-btn alt"
                      :disabled="beatBusy"
                      @click="suggestForBeat(idx)"
                    >{{ beatBusy ? t('story.arcTemplateIntake.beats.suggesting') : t('story.arcTemplateIntake.beats.suggest') }}</button>
                  </div>

                  <label class="field">
                    <span class="field-label">{{ t('story.arcTemplateIntake.fields.beatTitle') }}</span>
                    <input v-model="beat.title" class="field-input" />
                    <div v-if="beatOptions?.titles.length" class="chip-row">
                      <button
                        v-for="t in beatOptions.titles"
                        :key="t"
                        type="button"
                        class="chip ai"
                        @click="applyBeatOption('title', t)"
                      >{{ t }}</button>
                    </div>
                  </label>

                  <label class="field">
                    <span class="field-label">{{ t('story.arcTemplateIntake.fields.sceneType') }}</span>
                    <div class="chip-row">
                      <button
                        v-for="s in scaffolds?.scene_types ?? []"
                        :key="s.id"
                        type="button"
                        class="chip"
                        :class="{ active: beat.scene_type === s.id }"
                        @click="beat.scene_type = s.id"
                      >{{ scaffoldText('sceneTypes', s.id, 'label', s.label) }}</button>
                    </div>
                  </label>

                  <label class="field">
                    <span class="field-label">{{ t('story.arcTemplateIntake.fields.location') }}</span>
                    <input
                      v-model="beat.location"
                      class="field-input"
                      :placeholder="t('story.arcTemplateIntake.beats.locationPlaceholder')"
                    />
                    <div v-if="beatOptions?.locations.length" class="chip-row">
                      <button
                        v-for="l in beatOptions.locations"
                        :key="l"
                        type="button"
                        class="chip ai"
                        @click="applyBeatOption('location', l)"
                      >{{ l }}</button>
                    </div>
                  </label>

                  <label class="field">
                    <span class="field-label">{{ t('story.arcTemplateIntake.fields.sceneCharacters') }}</span>
                    <div class="char-row">
                      <span
                        v-for="c in beat.scene_characters"
                        :key="c"
                        class="char-pill"
                      >{{ c }}
                        <button class="char-x" @click="removeSceneCharacter(idx, c)">×</button>
                      </span>
                      <input
                        v-model="newCharacterInput"
                        class="field-input char-input"
                        :placeholder="t('story.arcTemplateIntake.beats.characterPlaceholder')"
                        @keydown.enter.prevent="addSceneCharacter(idx)"
                      />
                    </div>
                    <div v-if="beatOptions?.scene_characters.length" class="chip-row">
                      <button
                        v-for="c in beatOptions.scene_characters"
                        :key="c"
                        type="button"
                        class="chip ai"
                        @click="applyBeatOption('scene_characters', c)"
                      >+ {{ c }}</button>
                    </div>
                  </label>

                  <label class="field">
                    <span class="field-label">{{ t('story.arcTemplateIntake.fields.dramaticQuestion') }}</span>
                    <input
                      v-model="beat.dramatic_question"
                      class="field-input"
                      :placeholder="t('story.arcTemplateIntake.beats.questionPlaceholder')"
                    />
                    <div v-if="beatOptions?.dramatic_questions.length" class="chip-row">
                      <button
                        v-for="q in beatOptions.dramatic_questions"
                        :key="q"
                        type="button"
                        class="chip ai"
                        @click="applyBeatOption('dramatic_question', q)"
                      >{{ q }}</button>
                    </div>
                  </label>

                  <label class="field">
                    <span class="field-label">{{ t('story.arcTemplateIntake.fields.summary') }}</span>
                    <textarea
                      v-model="beat.summary"
                      class="field-textarea"
                      rows="4"
                      :placeholder="t('story.arcTemplateIntake.beats.summaryPlaceholder')"
                    />
                    <div class="field-actions">
                      <button
                        class="chip-btn alt"
                        :disabled="beatBusy"
                        @click="regenSummary(idx)"
                      >{{ beatBusy ? t('story.arcTemplateIntake.beats.summaryBusy') : t('story.arcTemplateIntake.beats.summaryAction') }}</button>
                    </div>
                  </label>
                </div>
              </li>
            </ul>

            <div class="step-actions">
              <button class="chip-btn" @click="back">{{ t('story.arcTemplateIntake.actions.back') }}</button>
              <button class="chip-btn alt" @click="addBeat">{{ t('story.arcTemplateIntake.beats.addBeat') }}</button>
              <button class="chip-btn primary" @click="commitBeats">{{ t('story.arcTemplateIntake.beats.next') }}</button>
            </div>
          </section>

          <!-- ===== Step 6: Review ===== -->
          <section v-else-if="step === 6" class="step">
            <p class="hint">
              {{ t('story.arcTemplateIntake.review.hintPrefix') }}
              <code>data/arc_templates/{{ idPreview }}.yaml</code>
              {{ t('story.arcTemplateIntake.review.hintSuffix') }}
            </p>

            <div class="review-card">
              <div v-if="props.targetCharacterId" class="scope-control">
                <button
                  type="button"
                  :class="[
                    'scope-option',
                    { active: draft.applicability_scope === 'character_bound' },
                  ]"
                  @click="setCharacterBoundScope"
                >{{ t('story.arcTemplateIntake.review.scopeCharacter') }}</button>
                <button
                  type="button"
                  :class="[
                    'scope-option',
                    { active: draft.applicability_scope === 'generic' },
                  ]"
                  @click="setGenericScope"
                >{{ t('story.arcTemplateIntake.review.scopeGeneric') }}</button>
              </div>
              <div class="review-row">
                <span class="review-label">{{ t('story.arcTemplateIntake.review.idLabel') }}</span>
                <span class="review-value mono">{{ idPreview }}</span>
              </div>
              <div class="review-row">
                <span class="review-label">{{ t('story.arcTemplateIntake.review.titleLabel') }}</span>
                <span class="review-value">{{ draft.title }}</span>
              </div>
              <div class="review-row">
                <span class="review-label">{{ t('story.arcTemplateIntake.review.themeToneLabel') }}</span>
                <span class="review-value">{{ draft.theme }} / {{ draft.tone }}</span>
              </div>
              <div class="review-row">
                <span class="review-label">{{ t('story.arcTemplateIntake.review.duration') }}</span>
                <span class="review-value">{{ t('story.arcTemplateIntake.review.durationValue', { days: draft.duration_days, beats: draft.beats.length }) }}</span>
              </div>
              <div class="review-row">
                <span class="review-label">{{ t('story.arcTemplateIntake.review.worldFramesLabel') }}</span>
                <span class="review-value">
                  {{ draft.world_frames.length === 0 ? t('story.arcTemplateIntake.review.unlimited') : draft.world_frames.join(t('common.listSeparator')) }}
                </span>
              </div>
              <div class="review-row block">
                <span class="review-label">{{ t('story.arcTemplateIntake.review.premiseLabel') }}</span>
                <span class="review-value">{{ draft.premise }}</span>
              </div>
            </div>

            <div class="review-beats">
              <div class="review-beats-title">{{ t('story.arcTemplateIntake.review.beatsLabel') }}</div>
              <ol class="review-beat-list">
                <li
                  v-for="b in draft.beats"
                  :key="b.sequence"
                  :class="['review-beat', `tension-${b.tension}`, { optional: !b.required }]"
                >
                  <div class="review-beat-head">
                    <span class="day">D{{ b.day_offset }}</span>
                    <span class="tension">{{ tensionLabel(b.tension) }}</span>
                    <span class="scene">{{ sceneTypeLabel(b.scene_type) }}</span>
                    <span v-if="!b.required" class="optional-pill">{{ t('story.arcTemplateIntake.optional') }}</span>
                    <span class="title">{{ b.title }}</span>
                  </div>
                  <div v-if="b.summary" class="review-beat-summary">{{ b.summary }}</div>
                  <div v-if="b.location || b.scene_characters.length || b.dramatic_question" class="review-beat-meta">
                    <span v-if="b.location"><b>{{ t('story.arcTemplateIntake.sceneLabels.location') }}</b> {{ b.location }}</span>
                    <span v-if="b.scene_characters.length"><b>{{ t('story.arcTemplateIntake.sceneLabels.characters') }}</b> {{ b.scene_characters.join(t('common.listSeparator')) }}</span>
                    <span v-if="b.dramatic_question"><b>{{ t('story.arcTemplateIntake.sceneLabels.question') }}</b> {{ b.dramatic_question }}</span>
                  </div>
                </li>
              </ol>
            </div>

            <div class="step-actions">
              <button class="chip-btn" @click="back">{{ t('story.arcTemplateIntake.review.backToBeats') }}</button>
              <button
                class="chip-btn primary"
                :disabled="saveBusy"
                @click="save(false)"
              >{{ saveBusy ? t('story.arcTemplateIntake.review.saving') : t('story.arcTemplateIntake.review.save') }}</button>
              <button
                v-if="overwriteAsk"
                class="chip-btn danger"
                :disabled="saveBusy"
                @click="save(true)"
              >{{ t('story.arcTemplateIntake.review.overwrite') }}</button>
            </div>
          </section>
        </div>

        <div v-if="errorMsg" class="wiz-error">{{ errorMsg }}</div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1400;
  background: rgba(0, 0, 0, 0.78);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.wizard {
  width: min(820px, 100%);
  max-height: 92vh;
  display: flex;
  flex-direction: column;
  background:
    linear-gradient(145deg, rgba(var(--color-primary-rgb), 0.08), rgba(255, 255, 255, 0.025)),
    var(--color-surface);
  border: 1px solid rgba(var(--color-primary-rgb), 0.24);
  border-radius: 10px;
  overflow: hidden;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.46);
}

.wiz-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-border);
  gap: 12px;
}

.wiz-title {
  font-size: 26px;
  color: var(--color-text);
  margin-bottom: 6px;
}

.wiz-steps {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 10px;
  color: var(--color-text-secondary);
}

.wiz-steps span {
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.055);
  border: 1px solid transparent;
}

.wiz-steps span.active {
  background: rgba(var(--color-spark-rgb), 0.15);
  border-color: rgba(var(--color-spark-rgb), 0.38);
  color: var(--color-spark);
  box-shadow: 0 0 16px rgba(var(--color-spark-rgb), 0.12);
  font-weight: 700;
}

.wiz-steps span.done {
  background: rgba(var(--color-primary-rgb), 0.18);
  border-color: rgba(var(--color-primary-rgb), 0.28);
  color: var(--color-primary-light);
}

.wiz-steps span.done {
  color: white;
}

.close-btn {
  background: none;
  border: none;
  color: var(--color-text-secondary);
  font-size: 20px;
  cursor: pointer;
  line-height: 1;
  padding: 0 4px;
}

.close-btn:hover { color: var(--color-text); }

.wiz-body {
  flex: 1;
  overflow-y: auto;
  padding: 14px 18px;
}

.step {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.6;
  margin: 0;
}

.hint-small {
  font-size: 10px;
  color: var(--color-text-secondary);
  margin-top: 4px;
}

.big-input {
  font-size: 14px;
  min-height: 84px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.field-label {
  font-size: 11px;
  color: var(--color-text-secondary);
  font-weight: 600;
}

.field-input.mono {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12px;
}

.field-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: flex-end;
}

.field-small {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.field-small > span {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.field-actions {
  display: flex;
  gap: 6px;
  margin-top: 4px;
}

.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}

.chip {
  padding: 3px 9px;
  font-size: 11px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  cursor: pointer;
}

.chip:hover {
  background: rgba(255, 255, 255, 0.12);
  color: var(--color-text);
}

.chip.active {
  background: rgba(var(--color-spark-rgb), 0.16);
  color: var(--color-spark);
  border-color: rgba(var(--color-spark-rgb), 0.42);
}

.chip.ai {
  border-style: dashed;
  border-color: rgba(var(--color-secondary-rgb), 0.4);
  color: #8ac8e8;
}

.step-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
}

.chip-btn {
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--color-border);
  border-radius: 999px;
  cursor: pointer;
  white-space: nowrap;
}

.chip-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.1);
  color: var(--color-text);
}

.chip-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.chip-btn.primary {
  background: var(--grad-flame);
  color: white;
  border-color: rgba(var(--color-primary-rgb), 0.58);
  box-shadow: 0 8px 24px rgba(var(--color-primary-rgb), 0.22);
}

.chip-btn.primary:hover:not(:disabled) {
  filter: brightness(1.08);
}

.chip-btn.alt {
  background: rgba(var(--color-secondary-rgb), 0.15);
  color: #8ac8e8;
  border-color: rgba(var(--color-secondary-rgb), 0.4);
}

.chip-btn.alt:hover:not(:disabled) {
  background: rgba(var(--color-secondary-rgb), 0.25);
}

.chip-btn.danger {
  background: rgba(208, 107, 107, 0.18);
  color: #f0a4a4;
  border-color: rgba(208, 107, 107, 0.4);
}

/* ----- Rhythm ----- */
.rhythm-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.rhythm-card {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  padding: 8px 10px;
  background: rgba(255, 255, 255, 0.03);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.rhythm-card:hover { background: rgba(255, 255, 255, 0.06); }

.rhythm-card.active {
  border-color: var(--color-primary);
  background: rgba(var(--color-primary-rgb), 0.12);
}

.rhythm-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 6px;
}

.rhythm-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
}

.rhythm-meta {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.rhythm-desc {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.beat-overview {
  margin-top: 4px;
}

.beat-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}

.beat-pill {
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.07);
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}

/* ----- Beats step ----- */
.beats-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.beat-edit {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
}

.beat-edit.open {
  background: rgba(255, 255, 255, 0.05);
}

.beat-summary-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  cursor: pointer;
}

.beat-day {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-primary-light);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
  min-width: 50px;
}

.beat-tension {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.beat-title-text {
  flex: 1;
  font-size: 12px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.optional-pill {
  font-size: 9px;
  padding: 1px 5px;
  border-radius: 999px;
  background: rgba(180, 180, 180, 0.15);
  color: #b0b0b0;
}

.mini-btn {
  background: none;
  border: none;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 14px;
  padding: 2px 6px;
  border-radius: 4px;
}

.mini-btn:hover { color: var(--color-text); background: rgba(255, 255, 255, 0.08); }

.mini-btn.danger:hover { color: #f0a4a4; background: rgba(208, 107, 107, 0.18); }

.beat-form {
  padding: 10px 12px 12px;
  border-top: 1px dashed var(--color-border);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* ----- scene_characters input ----- */
.char-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
  padding: 4px 6px;
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid var(--color-border);
  border-radius: 4px;
}

.char-pill {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 11px;
  padding: 2px 4px 2px 8px;
  border-radius: 999px;
  background: rgba(var(--color-secondary-rgb), 0.18);
  color: #8ac8e8;
}

.char-x {
  background: none;
  border: none;
  color: inherit;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
}

.char-input {
  flex: 1;
  min-width: 100px;
  background: transparent;
  border: none;
  color: var(--color-text);
  font-size: 12px;
  outline: none;
  padding: 2px 0;
}

/* ----- Review ----- */
.review-card {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.scope-control {
  display: inline-grid;
  grid-template-columns: repeat(2, minmax(90px, 1fr));
  gap: 4px;
  width: min(240px, 100%);
  padding: 2px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.04);
}

.scope-option {
  min-height: 30px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}

.scope-option.active {
  background: var(--color-primary);
  color: var(--color-text-on-primary);
}

.review-row {
  display: flex;
  gap: 10px;
  font-size: 12px;
}

.review-row.block {
  flex-direction: column;
  gap: 3px;
}

.review-label {
  flex-shrink: 0;
  width: 90px;
  color: var(--color-text-secondary);
  font-weight: 600;
}

.review-value {
  color: var(--color-text);
  line-height: 1.5;
  white-space: pre-wrap;
}

.review-value.mono {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11px;
}

.review-beats {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.review-beats-title {
  font-size: 11px;
  font-weight: 700;
  color: var(--color-primary-light);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.review-beat-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.review-beat {
  border: 1px solid var(--color-border);
  border-left-width: 3px;
  border-radius: 4px;
  padding: 7px 10px;
  background: rgba(0, 0, 0, 0.18);
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.review-beat.tension-setup { border-left-color: #6f9fbe; }
.review-beat.tension-rising { border-left-color: #d4a15a; }
.review-beat.tension-climax { border-left-color: #d06b6b; }
.review-beat.tension-falling { border-left-color: #8a7cb5; }
.review-beat.tension-resolution { border-left-color: #7ab28a; }
.review-beat.optional { opacity: 0.78; }

.review-beat-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  font-size: 11px;
}

.review-beat-head .day {
  font-weight: 600;
  color: var(--color-primary-light);
  font-variant-numeric: tabular-nums;
}

.review-beat-head .tension,
.review-beat-head .scene {
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-secondary);
  font-size: 9px;
}

.review-beat-head .title {
  font-weight: 600;
  color: var(--color-text);
}

.review-beat-summary {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  white-space: pre-wrap;
}

.review-beat-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 10px;
  color: var(--color-text-secondary);
}

.review-beat-meta b {
  color: var(--color-primary-light);
  font-weight: 600;
  margin-right: 3px;
}

.wiz-error {
  padding: 8px 18px;
  border-top: 1px solid var(--color-border);
  background: rgba(208, 107, 107, 0.12);
  color: #f0a4a4;
  font-size: 11px;
  line-height: 1.6;
}

@media (max-width: 720px) {
  .rhythm-list { grid-template-columns: 1fr; }
  .modal-backdrop { padding: 8px; }
  .wizard { max-height: 96vh; }
}
</style>
