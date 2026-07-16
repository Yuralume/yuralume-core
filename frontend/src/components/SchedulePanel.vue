<script setup lang="ts">
/**
 * 角色行程面板。
 *
 * 日期切換 + 每日活動列表 + inline 新增 / 編輯 / 刪除。
 * 已 memorialized 的活動（過去完成並轉成 episodic 記憶）會鎖起來
 * 不允許編輯或刪除 — 跟後端 `ScheduleService.apply_adjustments` 的
 * 保護規則對齊，避免改到已經進入長期記憶的歷史。
 *
 * 跟 ChatPanel header 的「當前活動徽章」的差別：
 * - header 只看「此刻 + 接下來」；這個面板看「整天 + 任一天」
 * - header 不能編輯；這個面板可以完整 CRUD
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { DailySchedule, ScheduleActivity } from '@/types/schedule'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import {
  addCivilDays,
  formatTime,
  timeInputValueForTimezone,
  todayISOForTimezone,
} from '@/i18n/formatters'
import {
  addScheduleActivity,
  deleteScheduleActivity,
  getSchedule,
  regenerateSchedule,
  updateScheduleActivity,
  type AddScheduleActivityPayload,
  type UpdateScheduleActivityPayload,
} from '@/utils/api/schedule'

const props = defineProps<{
  characterId: string | null
}>()

const { t, locale } = useI18n()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()

function todayISO(): string {
  return todayISOForTimezone(timeZone.value)
}

function shiftDate(iso: string, deltaDays: number): string {
  return addCivilDays(iso, deltaDays)
}

const selectedDate = ref<string>(todayISO())
const schedule = ref<DailySchedule | null>(null)
const loading = ref(false)
const busyId = ref<string | null>(null)
const errorMsg = ref<string | null>(null)
const regenerating = ref(false)

// 編輯中的 activity id（inline 展開成編輯表單用）
const editingId = ref<string | null>(null)
const editForm = ref<EditForm>(blankEditForm())

// 新增表單顯示狀態
const addFormOpen = ref(false)
const addForm = ref<AddForm>(blankAddForm())
const adding = ref(false)

interface AddForm {
  start: string
  end: string
  description: string
  category: string
  location: string
  busy_score: number
}

interface EditForm extends AddForm {}

function blankAddForm(): AddForm {
  return {
    start: '09:00',
    end: '10:00',
    description: '',
    category: '',
    location: '',
    busy_score: 0.5,
  }
}

function blankEditForm(): EditForm {
  return blankAddForm()
}

const sortedActivities = computed<ScheduleActivity[]>(() => {
  if (!schedule.value) return []
  return [...schedule.value.activities].sort(
    (a, b) => new Date(a.start_at).getTime() - new Date(b.start_at).getTime(),
  )
})

async function reload() {
  if (!props.characterId) {
    schedule.value = null
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    schedule.value = await getSchedule(props.characterId, selectedDate.value)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('schedulePanel.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

watch(() => props.characterId, () => {
  selectedDate.value = todayISO()
  schedule.value = null
  editingId.value = null
  addFormOpen.value = false
  reload()
}, { immediate: true })

watch(selectedDate, () => {
  editingId.value = null
  addFormOpen.value = false
  reload()
})

watch(timeZone, () => {
  const next = todayISO()
  if (selectedDate.value === next) {
    reload()
    return
  }
  selectedDate.value = next
})

async function handleRegenerate() {
  if (!props.characterId) return
  if (!await confirmDialog({
    content: t('schedulePanel.confirm.regenerate'),
  })) return
  regenerating.value = true
  errorMsg.value = null
  try {
    schedule.value = await regenerateSchedule(props.characterId, selectedDate.value)
    editingId.value = null
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('schedulePanel.errors.regenerateFailed')
  } finally {
    regenerating.value = false
  }
}

function openAddForm() {
  addForm.value = blankAddForm()
  addFormOpen.value = true
}

function closeAddForm() {
  addFormOpen.value = false
}

async function submitAdd() {
  if (!props.characterId) return
  const payload: AddScheduleActivityPayload = {
    start: addForm.value.start,
    end: addForm.value.end,
    description: addForm.value.description.trim(),
    category: addForm.value.category.trim(),
    location: addForm.value.location.trim() || null,
    busy_score: addForm.value.busy_score,
  }
  if (!payload.description) {
    errorMsg.value = t('schedulePanel.validation.descriptionRequired')
    return
  }
  if (!payload.category) {
    errorMsg.value = t('schedulePanel.validation.categoryRequired')
    return
  }
  adding.value = true
  errorMsg.value = null
  try {
    schedule.value = await addScheduleActivity(
      props.characterId, selectedDate.value, payload,
    )
    addFormOpen.value = false
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('schedulePanel.errors.addFailed')
  } finally {
    adding.value = false
  }
}

function formatClock(iso: string): string {
  return formatTime(iso, locale.value, timeZone.value)
}

function formatTimeInput(iso: string): string {
  return timeInputValueForTimezone(iso, timeZone.value)
}

function beginEdit(activity: ScheduleActivity) {
  if (activity.memorialized) return
  editingId.value = activity.id
  editForm.value = {
    start: formatTimeInput(activity.start_at),
    end: formatTimeInput(activity.end_at),
    description: activity.description,
    category: activity.category,
    location: activity.location ?? '',
    busy_score: activity.busy_score,
  }
}

function cancelEdit() {
  editingId.value = null
}

async function submitEdit(activity: ScheduleActivity) {
  if (!props.characterId) return
  const payload: UpdateScheduleActivityPayload = {
    start: editForm.value.start,
    end: editForm.value.end,
    description: editForm.value.description.trim() || undefined,
    category: editForm.value.category.trim() || undefined,
    location: editForm.value.location.trim() || null,
    busy_score: editForm.value.busy_score,
  }
  busyId.value = activity.id
  errorMsg.value = null
  try {
    schedule.value = await updateScheduleActivity(
      props.characterId, selectedDate.value, activity.id, payload,
    )
    editingId.value = null
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('schedulePanel.errors.updateFailed')
  } finally {
    busyId.value = null
  }
}

async function handleDelete(activity: ScheduleActivity) {
  if (!props.characterId) return
  if (activity.memorialized) return
  if (!await confirmDialog({
    content: t('schedulePanel.confirm.delete', { description: activity.description }),
    okText: t('common.actions.delete'),
    danger: true,
  })) return
  busyId.value = activity.id
  errorMsg.value = null
  try {
    schedule.value = await deleteScheduleActivity(
      props.characterId, selectedDate.value, activity.id,
    )
    if (editingId.value === activity.id) editingId.value = null
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('schedulePanel.errors.deleteFailed')
  } finally {
    busyId.value = null
  }
}

function extractError(err: unknown): string | null {
  if (err instanceof Error) return err.message
  return null
}

function prevDay() { selectedDate.value = shiftDate(selectedDate.value, -1) }
function nextDay() { selectedDate.value = shiftDate(selectedDate.value, 1) }
function jumpToday() { selectedDate.value = todayISO() }

function busyLabel(score: number): string {
  if (score >= 0.8) return t('schedulePanel.busy.high')
  if (score >= 0.5) return t('schedulePanel.busy.medium')
  if (score >= 0.2) return t('schedulePanel.busy.light')
  return t('schedulePanel.busy.free')
}

function characterParticipants(activity: ScheduleActivity): string[] {
  return (activity.participant_refs ?? [])
    .filter((ref) => ref.actor_kind === 'character')
    .map((ref) => ref.display_name)
    .filter(Boolean)
}
</script>

<template>
  <div class="schedule-panel">
    <div v-if="!characterId" class="empty-hint">{{ t('schedulePanel.empty.selectCharacter') }}</div>

    <template v-else>
      <div class="date-bar">
        <button class="icon-btn" :title="t('schedulePanel.actions.previousDay')" @click="prevDay">◀</button>
        <input
          v-model="selectedDate"
          type="date"
          class="field-input date-input"
        />
        <button class="icon-btn" :title="t('schedulePanel.actions.nextDay')" @click="nextDay">▶</button>
        <button class="chip-btn" :disabled="selectedDate === todayISO()" @click="jumpToday">{{ t('schedulePanel.actions.today') }}</button>
        <button
          class="chip-btn regen-btn"
          :disabled="regenerating || loading"
          @click="handleRegenerate"
        >{{ regenerating ? t('schedulePanel.actions.regenerating') : t('schedulePanel.actions.regenerate') }}</button>
      </div>

      <div v-if="loading" class="empty-hint">{{ t('common.state.loading') }}</div>

      <template v-else>
        <div v-if="sortedActivities.length === 0 && !addFormOpen" class="empty-hint">
          {{ t('schedulePanel.empty.noActivities') }}
        </div>

        <ul v-else class="activity-list">
          <li
            v-for="activity in sortedActivities"
            :key="activity.id"
            :class="['activity-item', { memorialized: activity.memorialized, editing: editingId === activity.id }]"
          >
            <div class="activity-time">
              <span class="time-range">
                {{ formatClock(activity.start_at) }} – {{ formatClock(activity.end_at) }}
              </span>
              <span v-if="activity.memorialized" class="memorialized-badge">{{ t('schedulePanel.status.memorialized') }}</span>
            </div>

            <!-- 讀取模式 -->
            <div v-if="editingId !== activity.id" class="activity-body">
              <div class="activity-head">
                <span class="activity-category">{{ activity.category }}</span>
                <span class="busy-pill" :title="t('schedulePanel.busy.title', { score: activity.busy_score.toFixed(2) })">
                  {{ busyLabel(activity.busy_score) }}
                </span>
              </div>
              <div class="activity-desc">{{ activity.description }}</div>
              <div v-if="activity.location" class="activity-location">📍 {{ activity.location }}</div>
              <div v-if="characterParticipants(activity).length" class="activity-participants">
                {{ t('schedulePanel.participants.characters', { names: characterParticipants(activity).join(t('common.listSeparator')) }) }}
              </div>
              <div v-else-if="activity.companion_names?.length" class="activity-participants npc">
                {{ t('schedulePanel.participants.companions', { names: activity.companion_names.join(t('common.listSeparator')) }) }}
              </div>

              <div class="activity-actions">
                <button
                  class="chip-btn"
                  :disabled="activity.memorialized || busyId === activity.id"
                  @click="beginEdit(activity)"
                >{{ activity.memorialized ? t('schedulePanel.actions.locked') : t('common.actions.edit') }}</button>
                <button
                  class="chip-btn danger"
                  :disabled="activity.memorialized || busyId === activity.id"
                  @click="handleDelete(activity)"
                >{{ t('common.actions.delete') }}</button>
              </div>
            </div>

            <!-- 編輯模式 -->
            <div v-else class="edit-form">
              <div class="form-row">
                <label class="field-small">
                  <span>{{ t('schedulePanel.fields.start') }}</span>
                  <input type="time" v-model="editForm.start" class="field-input" />
                </label>
                <label class="field-small">
                  <span>{{ t('schedulePanel.fields.end') }}</span>
                  <input type="time" v-model="editForm.end" class="field-input" />
                </label>
              </div>
              <label class="field-small">
                <span>{{ t('schedulePanel.fields.category') }}</span>
                <input
                  type="text"
                  v-model="editForm.category"
                  class="field-input"
                  :placeholder="t('schedulePanel.placeholders.category')"
                />
              </label>
              <label class="field-small">
                <span>{{ t('schedulePanel.fields.description') }}</span>
                <textarea
                  v-model="editForm.description"
                  class="field-textarea compact-textarea"
                  rows="2"
                />
              </label>
              <label class="field-small">
                <span>{{ t('schedulePanel.fields.location') }}</span>
                <input
                  type="text"
                  v-model="editForm.location"
                  class="field-input"
                />
              </label>
              <label class="field-small">
                <span>{{ t('schedulePanel.fields.busyScore', { score: editForm.busy_score.toFixed(2) }) }}</span>
                <input
                  type="range"
                  v-model.number="editForm.busy_score"
                  min="0" max="1" step="0.05"
                  class="field-range"
                />
              </label>
              <div class="form-actions">
                <button class="chip-btn" @click="cancelEdit">{{ t('common.actions.cancel') }}</button>
                <button
                  class="chip-btn primary"
                  :disabled="busyId === activity.id"
                  @click="submitEdit(activity)"
                >{{ busyId === activity.id ? t('common.state.saving') : t('common.actions.save') }}</button>
              </div>
            </div>
          </li>
        </ul>

        <!-- 新增表單 -->
        <div v-if="addFormOpen" class="add-form">
          <div class="form-title">{{ t('schedulePanel.add.title') }}</div>
          <div class="form-row">
            <label class="field-small">
              <span>{{ t('schedulePanel.fields.start') }}</span>
              <input type="time" v-model="addForm.start" class="field-input" />
            </label>
            <label class="field-small">
              <span>{{ t('schedulePanel.fields.end') }}</span>
              <input type="time" v-model="addForm.end" class="field-input" />
            </label>
          </div>
          <label class="field-small">
            <span>{{ t('schedulePanel.fields.category') }}</span>
            <input
              type="text"
              v-model="addForm.category"
              class="field-input"
              :placeholder="t('schedulePanel.placeholders.category')"
            />
          </label>
          <label class="field-small">
            <span>{{ t('schedulePanel.fields.description') }}</span>
            <textarea
              v-model="addForm.description"
              class="field-textarea compact-textarea"
              rows="2"
              :placeholder="t('schedulePanel.placeholders.description')"
            />
          </label>
          <label class="field-small">
            <span>{{ t('schedulePanel.fields.location') }}</span>
            <input type="text" v-model="addForm.location" class="field-input" />
          </label>
          <label class="field-small">
            <span>{{ t('schedulePanel.fields.busyScore', { score: addForm.busy_score.toFixed(2) }) }}</span>
            <input
              type="range"
              v-model.number="addForm.busy_score"
              min="0" max="1" step="0.05"
              class="field-range"
            />
          </label>
          <div class="form-actions">
            <button class="chip-btn" @click="closeAddForm">{{ t('common.actions.cancel') }}</button>
            <button
              class="chip-btn primary"
              :disabled="adding"
              @click="submitAdd"
            >{{ adding ? t('schedulePanel.add.adding') : t('schedulePanel.add.submit') }}</button>
          </div>
        </div>
        <button
          v-else
          class="add-btn"
          @click="openAddForm"
        >{{ t('schedulePanel.add.open') }}</button>
      </template>

      <div v-if="errorMsg" class="error-msg">{{ errorMsg }}</div>
    </template>
  </div>
</template>

<style scoped>
.schedule-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px;
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
}

.date-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.date-input {
  flex: 1 1 120px;
  min-width: 0;
}

.icon-btn {
  width: 28px;
  height: 28px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  font-size: 11px;
  cursor: pointer;
}

.icon-btn:hover { background: rgba(255, 255, 255, 0.1); }

.chip-btn {
  padding: 4px 10px;
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
  color: #ff8a75;
}

.regen-btn {
  margin-left: auto;
}

.activity-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.activity-item {
  min-width: 0;
  padding: 10px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.activity-item.memorialized {
  background: rgba(255, 255, 255, 0.015);
  opacity: 0.72;
}

.activity-item.editing {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 1px rgba(183, 93, 63, 0.3);
}

.activity-time {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.time-range {
  flex: 0 1 auto;
  max-width: 100%;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-primary-light);
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.memorialized-badge {
  max-width: 100%;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.1);
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}

.activity-head {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
}

.activity-category {
  max-width: 100%;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: rgba(183, 93, 63, 0.18);
  color: var(--color-primary-light);
  font-weight: 600;
  overflow-wrap: anywhere;
}

.activity-body {
  min-width: 0;
}

.busy-pill {
  max-width: 100%;
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}

.activity-desc {
  font-size: 13px;
  color: var(--color-text);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.activity-location {
  font-size: 11px;
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}

.activity-participants {
  font-size: 11px;
  color: var(--color-primary-light);
  overflow-wrap: anywhere;
}

.activity-participants.npc {
  color: var(--color-text-secondary);
}

.activity-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 4px;
}

.add-btn {
  padding: 10px;
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: rgba(255, 255, 255, 0.03);
  transition: background 0.2s;
}

.add-btn:hover {
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text);
}

.add-form,
.edit-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: rgba(183, 93, 63, 0.06);
  border: 1px solid rgba(183, 93, 63, 0.25);
  border-radius: 6px;
}

.form-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-primary-light);
  letter-spacing: 0.5px;
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

.compact-textarea {
  min-height: 56px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 2px;
}

.error-msg {
  padding: 6px 10px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.4);
  border-radius: 6px;
  color: #ff8a75;
  font-size: 12px;
}
</style>
