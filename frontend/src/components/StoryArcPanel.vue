<script setup lang="ts">
/**
 * 角色劇情主軸面板（StoryArc）。
 *
 * 顯示目前 active 的劇情 arc + 它的 beat 時間軸；支援：
 * - 查看 arc 標題／前情提要／每個 beat 的排期與內容
 * - inline 編輯 beat（pending 狀態才可編；realized 的 beat 是歷史，鎖住）
 * - 新增 / 刪除 beat
 * - 重新規劃整條 arc（保留已 realized 的 beat）
 * - 放棄 arc（跳過剩餘 pending beat）
 * - 開新 arc（可選填「希望是什麼方向」的 hint）
 *
 * 跟 StoryPanel 的事件池／種子庫分開：這裡是「劇情骨架」，
 * 種子庫是「日常小事的素材」；active arc 的 beat 到期當天會
 * 取代 gacha，自動變成 story_event。
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type {
  AddStoryArcBeatPayload,
  StoryArc,
  StoryArcBeat,
  StoryArcTension,
  UpdateStoryArcBeatPayload,
} from '@/types/storyArc'
import {
  abandonStoryArc,
  addStoryArcBeat,
  deleteStoryArcBeat,
  getActiveStoryArc,
  listStoryArcs,
  regenerateStoryArc,
  startStoryArc,
  updateStoryArcBeat,
  updateStoryArcMeta,
} from '@/utils/api/storyArc'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { todayISOForTimezone } from '@/i18n/formatters'
import {
  ARC_BEAT_COUNT_MAX,
  ARC_BEAT_COUNT_MIN,
  ARC_DURATION_MAX_DAYS,
  ARC_DURATION_MIN_DAYS,
  validateNewArcDraft,
} from '@/utils/storyArcValidation'
import ArcTemplatePicker from './ArcTemplatePicker.vue'

const props = defineProps<{
  characterId: string | null
  /**
   * The character's currently bound arc-template id (Phase 2). When set,
   * "開始新劇情" goes through the template path on the backend.
   * ``null`` keeps the legacy LLM-planning flow.
   */
  arcTemplateId?: string | null
  /**
   * Character's world_frame — passed to the picker so the operator
   * sees a "世界觀不符" hint on incompatible templates.
   */
  worldFrame?: string | null
}>()

const emit = defineEmits<{
  /**
   * Operator picked a template (or cleared the binding). The parent
   * panel should PATCH ``character.arc_template_id`` and refresh.
   * ``templateId=null`` = clear the binding.
   */
  (e: 'update:arc-template', templateId: string | null): void
  (e: 'active-arc-change', hasActiveArc: boolean): void
}>()

const { t } = useI18n()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()

const pickerOpen = ref(false)

const loading = ref(false)
const activeArc = ref<StoryArc | null>(null)
const pastArcs = ref<StoryArc[]>([])
const errorMsg = ref<string | null>(null)

// 新增 arc 的 modal 狀態
const newArcOpen = ref(false)
const newArcHint = ref('')
const newArcDuration = ref<number>(21)
const newArcBeatCount = ref<number>(5)
const starting = ref(false)

// Arc meta 編輯狀態
const editingMeta = ref(false)
const metaForm = ref({ title: '', premise: '', theme: '' })
const savingMeta = ref(false)

// Beat 編輯狀態
const editingBeatId = ref<string | null>(null)
const beatForm = ref<BeatFormState>(blankBeatForm())

// 新增 beat 狀態
const addBeatOpen = ref(false)
const addBeatForm = ref<BeatFormState>(blankBeatForm())
const savingBeat = ref(false)

// regenerate / abandon busy
const busy = ref(false)

// 過去 arcs 摺疊
const pastArcsOpen = ref(false)

interface BeatFormState {
  scheduled_date: string
  title: string
  summary: string
  tension: StoryArcTension
}

function blankBeatForm(): BeatFormState {
  return {
    scheduled_date: todayISO(),
    title: '',
    summary: '',
    tension: 'rising',
  }
}

function todayISO(): string {
  return todayISOForTimezone(timeZone.value)
}

const sortedBeats = computed<StoryArcBeat[]>(() => {
  if (!activeArc.value) return []
  return [...activeArc.value.beats].sort((a, b) => {
    if (a.scheduled_date !== b.scheduled_date) {
      return a.scheduled_date.localeCompare(b.scheduled_date)
    }
    return a.sequence - b.sequence
  })
})

async function reload() {
  if (!props.characterId) {
    activeArc.value = null
    pastArcs.value = []
    emit('active-arc-change', false)
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    const [active, all] = await Promise.all([
      getActiveStoryArc(props.characterId),
      listStoryArcs(props.characterId),
    ])
    activeArc.value = active
    emit('active-arc-change', Boolean(active))
    pastArcs.value = all.filter(a => a.status !== 'active')
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('story.arcPanel.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

watch(
  () => props.characterId,
  () => {
    editingBeatId.value = null
    editingMeta.value = false
    addBeatOpen.value = false
    newArcOpen.value = false
    reload()
  },
  { immediate: true },
)

// ---- Arc lifecycle ---------------------------------------------------

function openNewArc() {
  newArcHint.value = ''
  newArcDuration.value = 21
  newArcBeatCount.value = 5
  newArcOpen.value = true
}

async function submitNewArc() {
  if (!props.characterId) return
  // Template-bound path ignores hint/duration/beat_count server-side
  // (see templateHint above), so client-side bounds don't apply there.
  if (!props.arcTemplateId) {
    const invalidReason = validateNewArcDraft({
      duration_days: newArcDuration.value,
      beat_count: newArcBeatCount.value,
    })
    if (invalidReason) {
      errorMsg.value = t(`story.arcPanel.newArc.validation.${invalidReason}`)
      return
    }
  }
  starting.value = true
  errorMsg.value = null
  try {
    activeArc.value = await startStoryArc(props.characterId, {
      hint: newArcHint.value.trim() || undefined,
      duration_days: newArcDuration.value,
      beat_count: newArcBeatCount.value,
    })
    newArcOpen.value = false
    await reload()
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('story.arcPanel.errors.startFailed')
  } finally {
    starting.value = false
  }
}

async function handleRegenerate() {
  if (!activeArc.value) return
  if (!await confirmDialog({
    content: t('story.arcPanel.confirm.regenerate'),
  })) return
  busy.value = true
  errorMsg.value = null
  try {
    activeArc.value = await regenerateStoryArc(activeArc.value.id)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('story.arcPanel.errors.regenerateFailed')
  } finally {
    busy.value = false
  }
}

async function handleAbandon() {
  if (!activeArc.value) return
  if (!await confirmDialog({
    content: t('story.arcPanel.confirm.abandon'),
    danger: true,
  })) return
  busy.value = true
  errorMsg.value = null
  try {
    await abandonStoryArc(activeArc.value.id)
    await reload()
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('story.arcPanel.errors.abandonFailed')
  } finally {
    busy.value = false
  }
}

// ---- Meta edit -------------------------------------------------------

function beginEditMeta() {
  if (!activeArc.value) return
  metaForm.value = {
    title: activeArc.value.title,
    premise: activeArc.value.premise,
    theme: activeArc.value.theme,
  }
  editingMeta.value = true
}

function cancelEditMeta() {
  editingMeta.value = false
}

async function saveMeta() {
  if (!activeArc.value) return
  savingMeta.value = true
  errorMsg.value = null
  try {
    activeArc.value = await updateStoryArcMeta(activeArc.value.id, {
      title: metaForm.value.title.trim(),
      premise: metaForm.value.premise.trim(),
      theme: metaForm.value.theme.trim(),
    })
    editingMeta.value = false
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('story.arcPanel.errors.updateMetaFailed')
  } finally {
    savingMeta.value = false
  }
}

// ---- Beat CRUD --------------------------------------------------------

function beginEditBeat(beat: StoryArcBeat) {
  if (beat.status === 'realized') return
  editingBeatId.value = beat.id
  beatForm.value = {
    scheduled_date: beat.scheduled_date,
    title: beat.title,
    summary: beat.summary,
    tension: beat.tension,
  }
}

function cancelEditBeat() {
  editingBeatId.value = null
}

async function saveBeat(beat: StoryArcBeat) {
  savingBeat.value = true
  errorMsg.value = null
  try {
    const payload: UpdateStoryArcBeatPayload = {
      scheduled_date: beatForm.value.scheduled_date,
      title: beatForm.value.title.trim() || undefined,
      summary: beatForm.value.summary.trim() || undefined,
      tension: beatForm.value.tension,
    }
    activeArc.value = await updateStoryArcBeat(beat.id, payload)
    editingBeatId.value = null
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('story.arcPanel.errors.updateBeatFailed')
  } finally {
    savingBeat.value = false
  }
}

async function handleDeleteBeat(beat: StoryArcBeat) {
  if (beat.status === 'realized') return
  if (!await confirmDialog({
    content: t('story.arcPanel.confirm.deleteBeat', { title: beat.title }),
    okText: t('common.actions.delete'),
    danger: true,
  })) return
  savingBeat.value = true
  errorMsg.value = null
  try {
    activeArc.value = await deleteStoryArcBeat(beat.id)
    if (editingBeatId.value === beat.id) editingBeatId.value = null
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('story.arcPanel.errors.deleteBeatFailed')
  } finally {
    savingBeat.value = false
  }
}

function openAddBeat() {
  addBeatForm.value = blankBeatForm()
  addBeatOpen.value = true
}

function closeAddBeat() {
  addBeatOpen.value = false
}

async function submitAddBeat() {
  if (!activeArc.value) return
  if (!addBeatForm.value.title.trim() || !addBeatForm.value.summary.trim()) {
    errorMsg.value = t('story.arcPanel.validation.titleAndSummaryRequired')
    return
  }
  savingBeat.value = true
  errorMsg.value = null
  try {
    const payload: AddStoryArcBeatPayload = {
      scheduled_date: addBeatForm.value.scheduled_date,
      title: addBeatForm.value.title.trim(),
      summary: addBeatForm.value.summary.trim(),
      tension: addBeatForm.value.tension,
    }
    activeArc.value = await addStoryArcBeat(activeArc.value.id, payload)
    addBeatOpen.value = false
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('story.arcPanel.errors.addBeatFailed')
  } finally {
    savingBeat.value = false
  }
}

// ---- helpers ----------------------------------------------------------

function extractError(err: unknown): string | null {
  if (err instanceof Error) return err.message
  return null
}

function tensionLabel(tension: StoryArcTension): string {
  return t(`story.arcPanel.tension.${tension}`)
}

function statusLabel(s: string): string {
  const key = {
    pending: 'story.arcPanel.status.pending',
    active: 'story.arcPanel.status.active',
    realized: 'story.arcPanel.status.realized',
    skipped: 'story.arcPanel.status.skipped',
    'active-arc': 'story.arcPanel.status.active',
    completed: 'story.arcPanel.status.completed',
    abandoned: 'story.arcPanel.status.abandoned',
  }[s]
  return key ? t(key) : s
}

function sceneTypeLabel(s: string): string {
  const key = {
    encounter: 'story.arcPanel.sceneType.encounter',
    revelation: 'story.arcPanel.sceneType.revelation',
    conflict: 'story.arcPanel.sceneType.conflict',
    resolution: 'story.arcPanel.sceneType.resolution',
    interlude: 'story.arcPanel.sceneType.interlude',
  }[s]
  return key ? t(key) : s
}

function beatHasSceneStructure(beat: StoryArcBeat): boolean {
  return Boolean(
    beat.location
    || (beat.scene_characters && beat.scene_characters.length > 0)
    || beat.dramatic_question,
  )
}

// ---- Template picker -------------------------------------------------

function openTemplatePicker() {
  pickerOpen.value = true
}

function onPickTemplate(templateId: string) {
  emit('update:arc-template', templateId)
  pickerOpen.value = false
}

function onClearTemplate() {
  emit('update:arc-template', null)
  pickerOpen.value = false
}

function closePicker() {
  pickerOpen.value = false
}

defineExpose({
  openNewArc,
  openTemplatePicker,
  reload,
})
</script>

<template>
  <div class="arc-panel">
    <div class="arc-header">
      <h3 class="section-title">{{ t('story.arcPanel.title') }}</h3>
      <p class="arc-hint">
        {{ t('story.arcPanel.hint') }}
      </p>
      <!-- 範本綁定狀態列 — 跟「開新劇情」時走的路徑相連 -->
      <div v-if="characterId" class="template-bar">
        <span class="template-label">{{ t('story.arcPanel.template.label') }}</span>
        <span v-if="arcTemplateId" class="template-pill bound">
          {{ t('story.arcPanel.template.bound', { id: arcTemplateId }) }}
        </span>
        <span v-else class="template-pill llm">{{ t('story.arcPanel.template.unbound') }}</span>
        <button class="chip-btn small" @click="openTemplatePicker">
          {{ arcTemplateId ? t('story.arcPanel.template.changeOrClear') : t('story.arcPanel.template.choose') }}
        </button>
      </div>
    </div>

    <div v-if="!characterId" class="empty-hint">{{ t('story.arcPanel.noCharacter') }}</div>
    <div v-else-if="loading" class="empty-hint">{{ t('common.state.loading') }}</div>

    <template v-else>
      <!-- Active arc -->
      <div v-if="!activeArc" class="empty-hint">
        {{ t('story.arcPanel.emptyActive') }}
        <button class="chip-btn primary" @click="openNewArc">{{ t('story.arcPanel.startNew') }}</button>
      </div>

      <div v-else class="active-arc">
        <!-- Arc meta -->
        <div v-if="!editingMeta" class="arc-meta">
          <div class="arc-title-row">
            <div class="arc-title">{{ activeArc.title }}</div>
            <span class="theme-pill">{{ activeArc.theme }}</span>
          </div>
          <div class="arc-premise">{{ activeArc.premise }}</div>
          <div class="arc-date-range">
            {{ activeArc.start_date }}–{{ activeArc.end_date }}
          </div>
          <div class="arc-actions">
            <button class="chip-btn" @click="beginEditMeta">{{ t('story.arcPanel.actions.editMeta') }}</button>
            <button
              class="chip-btn"
              :disabled="busy"
              @click="handleRegenerate"
            >{{ t('story.arcPanel.actions.regenerate') }}</button>
            <button
              class="chip-btn danger"
              :disabled="busy"
              @click="handleAbandon"
            >{{ t('story.arcPanel.actions.abandon') }}</button>
          </div>
        </div>

        <div v-else class="meta-form">
          <label class="field-small">
            <span class="field-label">{{ t('story.arcPanel.fields.title') }}</span>
            <input v-model="metaForm.title" class="field-input" />
          </label>
          <label class="field-small">
            <span class="field-label">{{ t('story.arcPanel.fields.theme') }}</span>
            <input v-model="metaForm.theme" class="field-input" />
          </label>
          <label class="field-small">
            <span class="field-label">{{ t('story.arcPanel.fields.premise') }}</span>
            <textarea v-model="metaForm.premise" class="field-textarea" rows="3" />
          </label>
          <div class="form-actions">
            <button class="chip-btn" @click="cancelEditMeta">{{ t('common.actions.cancel') }}</button>
            <button
              class="chip-btn primary"
              :disabled="savingMeta"
              @click="saveMeta"
            >{{ savingMeta ? t('common.state.saving') : t('common.actions.save') }}</button>
          </div>
        </div>

        <!-- Beat timeline -->
        <div class="beats-section">
          <div class="beats-title">{{ t('story.arcPanel.beats.title') }}</div>
          <div v-if="sortedBeats.length === 0" class="empty-hint small">
            {{ t('story.arcPanel.beats.empty') }}
          </div>
          <ul v-else class="beat-list">
            <li
              v-for="beat in sortedBeats"
              :key="beat.id"
              :class="[
                'beat-item',
                `status-${beat.status}`,
                `tension-${beat.tension}`,
                { editing: editingBeatId === beat.id },
              ]"
            >
              <!-- 讀取模式 -->
              <div v-if="editingBeatId !== beat.id" class="beat-body">
                <div class="beat-head">
                  <span class="beat-date">{{ beat.scheduled_date }}</span>
                  <span class="tension-pill">{{ tensionLabel(beat.tension) }}</span>
                  <span class="status-pill">{{ statusLabel(beat.status) }}</span>
                  <span v-if="beat.scene_type && beat.scene_type !== 'encounter'" class="scene-pill">
                    {{ sceneTypeLabel(beat.scene_type) }}
                  </span>
                  <span v-if="!beat.required" class="optional-pill">{{ t('story.arcPanel.optional') }}</span>
                </div>
                <div class="beat-title">{{ beat.title }}</div>
                <div class="beat-summary">{{ beat.summary }}</div>
                <div v-if="beatHasSceneStructure(beat)" class="beat-scene-block">
                  <div v-if="beat.location" class="scene-line">
                    <span class="scene-tag">{{ t('story.arcPanel.sceneLabels.location') }}</span>
                    <span>{{ beat.location }}</span>
                  </div>
                  <div v-if="beat.scene_characters && beat.scene_characters.length > 0" class="scene-line">
                    <span class="scene-tag">{{ t('story.arcPanel.sceneLabels.characters') }}</span>
                    <span>{{ beat.scene_characters.join(t('common.listSeparator')) }}</span>
                  </div>
                  <div v-if="beat.dramatic_question" class="scene-line">
                    <span class="scene-tag">{{ t('story.arcPanel.sceneLabels.question') }}</span>
                    <span>{{ beat.dramatic_question }}</span>
                  </div>
                </div>
                <div class="beat-actions">
                  <button
                    class="chip-btn"
                    :disabled="beat.status === 'realized' || savingBeat"
                    @click="beginEditBeat(beat)"
                  >{{ beat.status === 'realized' ? t('story.arcPanel.status.lockedRealized') : t('common.actions.edit') }}</button>
                  <button
                    class="chip-btn danger"
                    :disabled="beat.status === 'realized' || savingBeat"
                    @click="handleDeleteBeat(beat)"
                  >{{ t('common.actions.delete') }}</button>
                </div>
              </div>

              <!-- 編輯模式 -->
              <div v-else class="beat-edit-form">
                <div class="form-row">
                  <label class="field-small">
                    <span class="field-label">{{ t('story.arcPanel.fields.scheduledDate') }}</span>
                    <input
                      type="date"
                      v-model="beatForm.scheduled_date"
                      class="field-input"
                    />
                  </label>
                  <label class="field-small">
                    <span class="field-label">{{ t('story.arcPanel.fields.tension') }}</span>
                    <select v-model="beatForm.tension" class="field-select">
                      <option value="setup">{{ tensionLabel('setup') }} · setup</option>
                      <option value="rising">{{ tensionLabel('rising') }} · rising</option>
                      <option value="climax">{{ tensionLabel('climax') }} · climax</option>
                      <option value="falling">{{ tensionLabel('falling') }} · falling</option>
                      <option value="resolution">{{ tensionLabel('resolution') }} · resolution</option>
                    </select>
                  </label>
                </div>
                <label class="field-small">
                  <span class="field-label">{{ t('story.arcPanel.fields.title') }}</span>
                  <input v-model="beatForm.title" class="field-input" />
                </label>
                <label class="field-small">
                  <span class="field-label">{{ t('story.arcPanel.fields.summary') }}</span>
                  <textarea
                    v-model="beatForm.summary"
                    class="field-textarea"
                    rows="3"
                  />
                </label>
                <div class="form-actions">
                  <button class="chip-btn" @click="cancelEditBeat">{{ t('common.actions.cancel') }}</button>
                  <button
                    class="chip-btn primary"
                    :disabled="savingBeat"
                    @click="saveBeat(beat)"
                  >{{ savingBeat ? t('common.state.saving') : t('common.actions.save') }}</button>
                </div>
              </div>
            </li>
          </ul>

          <!-- Add beat form -->
          <div v-if="addBeatOpen" class="add-beat-form">
            <div class="form-row">
              <label class="field-small">
                <span class="field-label">{{ t('story.arcPanel.fields.scheduledDate') }}</span>
                <input
                  type="date"
                  v-model="addBeatForm.scheduled_date"
                  class="field-input"
                />
              </label>
              <label class="field-small">
                <span class="field-label">{{ t('story.arcPanel.fields.tension') }}</span>
                <select v-model="addBeatForm.tension" class="field-select">
                  <option value="setup">{{ tensionLabel('setup') }} · setup</option>
                  <option value="rising">{{ tensionLabel('rising') }} · rising</option>
                  <option value="climax">{{ tensionLabel('climax') }} · climax</option>
                  <option value="falling">{{ tensionLabel('falling') }} · falling</option>
                  <option value="resolution">{{ tensionLabel('resolution') }} · resolution</option>
                </select>
              </label>
            </div>
            <label class="field-small">
              <span class="field-label">{{ t('story.arcPanel.fields.title') }}</span>
              <input v-model="addBeatForm.title" class="field-input" />
            </label>
            <label class="field-small">
              <span class="field-label">{{ t('story.arcPanel.fields.summaryShort') }}</span>
              <textarea
                v-model="addBeatForm.summary"
                class="field-textarea"
                rows="3"
              />
            </label>
            <div class="form-actions">
              <button class="chip-btn" @click="closeAddBeat">{{ t('common.actions.cancel') }}</button>
              <button
                class="chip-btn primary"
                :disabled="savingBeat"
                @click="submitAddBeat"
              >{{ savingBeat ? t('story.arcPanel.actions.adding') : t('story.arcPanel.actions.add') }}</button>
            </div>
          </div>
          <button
            v-else
            class="add-btn"
            @click="openAddBeat"
          >{{ t('story.arcPanel.actions.addBeat') }}</button>
        </div>
      </div>

      <!-- Past arcs -->
      <div v-if="pastArcs.length > 0" class="past-section">
        <button class="past-toggle" @click="pastArcsOpen = !pastArcsOpen">
          {{ pastArcsOpen ? '▼' : '▶' }} {{ t('story.arcPanel.past.title', { count: pastArcs.length }) }}
        </button>
        <ul v-if="pastArcsOpen" class="past-list">
          <li v-for="arc in pastArcs" :key="arc.id" class="past-item">
            <div class="past-title">
              {{ arc.title }}
              <span class="status-pill">{{ statusLabel(arc.status) }}</span>
            </div>
            <div class="past-premise">{{ arc.premise }}</div>
            <div class="past-range">{{ arc.start_date }}–{{ arc.end_date }}</div>
          </li>
        </ul>
      </div>

      <div v-if="errorMsg" class="error-msg">{{ errorMsg }}</div>

      <!-- New arc modal -->
      <Teleport to="body">
        <div v-if="newArcOpen" class="modal-backdrop">
          <div class="modal" role="dialog" :aria-label="t('story.arcPanel.newArc.title')">
            <div class="modal-header">
              <div class="modal-title">{{ t('story.arcPanel.newArc.title') }}</div>
              <div class="modal-hint">
                <template v-if="arcTemplateId">
                  {{ t('story.arcPanel.newArc.templateHint', { id: arcTemplateId }) }}
                </template>
                <template v-else>
                  {{ t('story.arcPanel.newArc.llmHint') }}
                </template>
              </div>
            </div>
            <div class="modal-body">
              <label class="field-small">
                <span class="field-label">{{ t('story.arcPanel.newArc.hintLabel') }}</span>
                <textarea
                  v-model="newArcHint"
                  class="field-textarea"
                  rows="3"
                  :placeholder="t('story.arcPanel.newArc.hintPlaceholder')"
                  :disabled="!!arcTemplateId"
                />
              </label>
              <div class="form-row">
                <label class="field-small">
                  <span class="field-label">{{ t('story.arcPanel.newArc.durationLabel') }}</span>
                  <input
                    type="number"
                    v-model.number="newArcDuration"
                    class="field-input"
                    :min="ARC_DURATION_MIN_DAYS"
                    :max="ARC_DURATION_MAX_DAYS"
                    :disabled="!!arcTemplateId"
                  />
                </label>
                <label class="field-small">
                  <span class="field-label">{{ t('story.arcPanel.newArc.beatCountLabel') }}</span>
                  <input
                    type="number"
                    v-model.number="newArcBeatCount"
                    class="field-input"
                    :min="ARC_BEAT_COUNT_MIN"
                    :max="ARC_BEAT_COUNT_MAX"
                    :disabled="!!arcTemplateId"
                  />
                </label>
              </div>
              <div v-if="!arcTemplateId" class="field-hint">
                {{ t('story.arcPanel.newArc.durationRangeHint', {
                  min: ARC_DURATION_MIN_DAYS,
                  max: ARC_DURATION_MAX_DAYS,
                }) }}
              </div>
            </div>
            <div class="modal-actions">
              <button class="chip-btn" @click="newArcOpen = false">{{ t('common.actions.cancel') }}</button>
              <button
                class="chip-btn primary"
                :disabled="starting"
                @click="submitNewArc"
              >{{ starting ? (arcTemplateId ? t('story.arcPanel.newArc.applyingTemplate') : t('story.arcPanel.newArc.planning')) : t('story.arcPanel.newArc.start') }}</button>
            </div>
          </div>
        </div>
      </Teleport>

      <!-- Arc template picker — opens on demand from the header bar -->
      <ArcTemplatePicker
        v-if="pickerOpen"
        :current-template-id="arcTemplateId ?? null"
        :character-id="characterId ?? null"
        :world-frame="worldFrame ?? null"
        @select="onPickTemplate"
        @clear="onClearTemplate"
        @close="closePicker"
      />
    </template>
  </div>
</template>

<style scoped>
.arc-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 0;
}

.arc-hint,
.arc-header {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.arc-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.empty-hint {
  padding: 14px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.02);
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  line-height: 1.6;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
}

.empty-hint.small {
  padding: 8px;
  font-size: 11px;
}

.active-arc {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  background: rgba(183, 93, 63, 0.06);
  border: 1px solid rgba(183, 93, 63, 0.25);
  border-radius: 8px;
}

.arc-meta {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.arc-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.arc-title {
  min-width: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--color-primary-light);
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.theme-pill {
  max-width: 100%;
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 10px;
  background: rgba(183, 93, 63, 0.24);
  color: var(--color-primary-light);
  font-weight: 600;
  overflow-wrap: anywhere;
}

.arc-premise {
  font-size: 12px;
  color: var(--color-text);
  line-height: 1.6;
  white-space: pre-wrap;
}

.arc-date-range {
  font-size: 11px;
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.arc-actions,
.form-actions,
.beat-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.form-actions {
  justify-content: flex-end;
  margin-top: 2px;
}

.chip-btn {
  padding: 5px 10px;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--color-border);
  border-radius: 999px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
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
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.chip-btn.primary:hover:not(:disabled) {
  background: var(--color-primary-dark);
}

.chip-btn.danger {
  color: #ff8a75;
}

.chip-btn.danger:hover:not(:disabled) {
  background: rgba(231, 76, 60, 0.15);
}

.meta-form,
.add-beat-form,
.beat-edit-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: rgba(0, 0, 0, 0.15);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}

.field-small {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.beats-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.beats-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-primary-light);
  letter-spacing: 0.5px;
}

.beat-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.beat-item {
  min-width: 0;
  padding: 10px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  border-left-width: 3px;
}

.beat-item.tension-setup { border-left-color: #6f9fbe; }
.beat-item.tension-rising { border-left-color: #d4a15a; }
.beat-item.tension-climax { border-left-color: #d06b6b; }
.beat-item.tension-falling { border-left-color: #8a7cb5; }
.beat-item.tension-resolution { border-left-color: #7ab28a; }

.beat-item.status-realized { opacity: 0.7; }
.beat-item.status-skipped { opacity: 0.5; text-decoration: line-through; }

.beat-item.editing {
  box-shadow: 0 0 0 1px rgba(183, 93, 63, 0.3);
}

.beat-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.beat-head {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
}

.beat-date {
  flex: 0 1 auto;
  max-width: 100%;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-primary-light);
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.tension-pill,
.status-pill {
  max-width: 100%;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}

.beat-title {
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  overflow-wrap: anywhere;
}

.beat-summary {
  min-width: 0;
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.beat-actions {
  margin-top: 4px;
}

.add-btn {
  padding: 8px;
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  text-align: center;
  font-size: 11px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: rgba(255, 255, 255, 0.03);
  transition: background 0.2s;
}

.add-btn:hover {
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text);
}

.past-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
}

.past-toggle {
  background: none;
  border: none;
  padding: 4px 0;
  font-size: 11px;
  color: var(--color-text-secondary);
  cursor: pointer;
  text-align: left;
}

.past-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.past-item {
  padding: 8px;
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}

.past-title {
  font-size: 12px;
  font-weight: 600;
  display: flex;
  gap: 6px;
  align-items: center;
}

.past-premise {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin-top: 3px;
}

.past-range {
  font-size: 10px;
  color: var(--color-text-secondary);
  margin-top: 3px;
}

.error-msg {
  padding: 6px 10px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.4);
  border-radius: 6px;
  color: #ff8a75;
  font-size: 12px;
}

/* Template-binding bar */
.template-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 6px 8px;
  margin-top: 6px;
  background: rgba(0, 0, 0, 0.18);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}

.template-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-primary-light);
}

.template-pill {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  font-weight: 600;
}

.template-pill.bound {
  background: rgba(183, 93, 63, 0.22);
  color: var(--color-primary-light);
}

.template-pill.llm {
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-secondary);
}

.chip-btn.small {
  padding: 3px 8px;
  font-size: 10px;
}

/* Scene-structure pills + block in beat rows */
.scene-pill,
.optional-pill {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(120, 180, 220, 0.15);
  color: #8ac8e8;
  font-weight: 600;
}

.optional-pill {
  background: rgba(180, 180, 180, 0.15);
  color: #b0b0b0;
}

.beat-scene-block {
  margin-top: 4px;
  padding: 6px 8px;
  background: rgba(0, 0, 0, 0.16);
  border-left: 2px solid rgba(120, 180, 220, 0.4);
  border-radius: 3px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.scene-line {
  font-size: 11px;
  color: var(--color-text-secondary);
  display: flex;
  gap: 6px;
  line-height: 1.4;
}

.scene-tag {
  flex-shrink: 0;
  width: 30px;
  color: var(--color-primary-light);
  font-weight: 600;
}

/* Modal */
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1200;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.modal {
  width: min(520px, 100%);
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  overflow: hidden;
}

.modal-header {
  padding: 14px 18px 10px;
  border-bottom: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.modal-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--color-primary-light);
}

.modal-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.modal-body {
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.modal-actions {
  padding: 10px 14px;
  border-top: 1px solid var(--color-border);
  background: rgba(0, 0, 0, 0.2);
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
</style>
