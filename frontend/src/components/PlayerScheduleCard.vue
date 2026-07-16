<script setup lang="ts">
/**
 * Player-facing character-day editor.
 *
 * This is intentionally lighter than SchedulePanel: players can shape
 * time, activity text, category, and location, while regenerate and
 * busy_score stay in the Admin surface.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
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
  getCurrentActivity,
  getSchedule,
  updateScheduleActivity,
  type AddScheduleActivityPayload,
  type UpdateScheduleActivityPayload,
} from '@/utils/api/schedule'

const { t, locale } = useI18n()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()

const props = defineProps<{
  characterId: string | null
  showAdminLink?: boolean
}>()

interface ActivityForm {
  start: string
  end: string
  description: string
  category: string
  location: string
}

function blankActivityForm(): ActivityForm {
  return {
    start: '09:00',
    end: '10:00',
    description: '',
    category: '',
    location: '',
  }
}

function todayISO(): string {
  return todayISOForTimezone(timeZone.value)
}

function shiftDate(iso: string, deltaDays: number): string {
  return addCivilDays(iso, deltaDays)
}

const selectedDate = ref(todayISO())
const schedule = ref<DailySchedule | null>(null)
const currentActivityId = ref<string | null>(null)
const loading = ref(false)
const errorMsg = ref<string | null>(null)
const busyId = ref<string | null>(null)
const editingId = ref<string | null>(null)
const editForm = ref<ActivityForm>(blankActivityForm())
const addFormOpen = ref(false)
const addForm = ref<ActivityForm>(blankActivityForm())
const adding = ref(false)

const today = computed(() => todayISO())
const isViewingToday = computed(() => selectedDate.value === today.value)

const sortedActivities = computed<ScheduleActivity[]>(() => {
  if (!schedule.value) return []
  return [...schedule.value.activities].sort(
    (a, b) => new Date(a.start_at).getTime() - new Date(b.start_at).getTime(),
  )
})

function resetForms() {
  editingId.value = null
  addFormOpen.value = false
  addForm.value = blankActivityForm()
  editForm.value = blankActivityForm()
}

async function reload() {
  if (!props.characterId) {
    schedule.value = null
    currentActivityId.value = null
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    const [day, currentSnap] = await Promise.all([
      getSchedule(props.characterId, selectedDate.value),
      isViewingToday.value
        ? getCurrentActivity(props.characterId).catch(() => null)
        : Promise.resolve(null),
    ])
    schedule.value = day
    currentActivityId.value = currentSnap?.current?.id ?? null
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('playerSchedule.errors.loadFailed')
    schedule.value = null
  } finally {
    loading.value = false
  }
}

watch(() => props.characterId, () => {
  selectedDate.value = todayISO()
  schedule.value = null
  currentActivityId.value = null
  resetForms()
  void reload()
}, { immediate: true })

watch(selectedDate, () => {
  resetForms()
  void reload()
})

watch(timeZone, () => {
  selectedDate.value = todayISO()
  void reload()
})

let pollTimer: ReturnType<typeof setInterval> | null = null
watch(() => props.characterId, (id) => {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  if (id) {
    pollTimer = setInterval(() => {
      if (!editingId.value && !addFormOpen.value) void reload()
    }, 60_000)
  }
}, { immediate: true })

onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})

function formatClock(iso: string): string {
  return formatTime(iso, locale.value, timeZone.value)
}

function formatTimeInput(iso: string): string {
  return timeInputValueForTimezone(iso, timeZone.value)
}

function isCurrent(activity: ScheduleActivity): boolean {
  return isViewingToday.value && activity.id === currentActivityId.value
}

function openAddForm() {
  editingId.value = null
  addForm.value = blankActivityForm()
  addFormOpen.value = true
}

function closeAddForm() {
  addFormOpen.value = false
}

function beginEdit(activity: ScheduleActivity) {
  if (activity.memorialized) return
  addFormOpen.value = false
  editingId.value = activity.id
  editForm.value = {
    start: formatTimeInput(activity.start_at),
    end: formatTimeInput(activity.end_at),
    description: activity.description,
    category: activity.category,
    location: activity.location ?? '',
  }
}

function cancelEdit() {
  editingId.value = null
}

function validateForm(form: ActivityForm): boolean {
  if (!form.description.trim()) {
    errorMsg.value = t('playerSchedule.validation.descriptionRequired')
    return false
  }
  if (!form.category.trim()) {
    errorMsg.value = t('playerSchedule.validation.categoryRequired')
    return false
  }
  return true
}

async function submitAdd() {
  if (!props.characterId || !validateForm(addForm.value)) return
  const payload: AddScheduleActivityPayload = {
    start: addForm.value.start,
    end: addForm.value.end,
    description: addForm.value.description.trim(),
    category: addForm.value.category.trim(),
    location: addForm.value.location.trim() || null,
  }
  adding.value = true
  errorMsg.value = null
  try {
    schedule.value = await addScheduleActivity(
      props.characterId,
      selectedDate.value,
      payload,
    )
    addFormOpen.value = false
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('playerSchedule.errors.addFailed')
  } finally {
    adding.value = false
  }
}

async function submitEdit(activity: ScheduleActivity) {
  if (!props.characterId || !validateForm(editForm.value)) return
  const payload: UpdateScheduleActivityPayload = {
    start: editForm.value.start,
    end: editForm.value.end,
    description: editForm.value.description.trim(),
    category: editForm.value.category.trim(),
    location: editForm.value.location.trim() || null,
  }
  busyId.value = activity.id
  errorMsg.value = null
  try {
    schedule.value = await updateScheduleActivity(
      props.characterId,
      selectedDate.value,
      activity.id,
      payload,
    )
    editingId.value = null
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('playerSchedule.errors.updateFailed')
  } finally {
    busyId.value = null
  }
}

async function handleDelete(activity: ScheduleActivity) {
  if (!props.characterId || activity.memorialized) return
  if (!await confirmDialog({
    content: t('playerSchedule.confirmDelete', { description: activity.description }),
    okText: t('common.actions.delete'),
    danger: true,
  })) return
  busyId.value = activity.id
  errorMsg.value = null
  try {
    schedule.value = await deleteScheduleActivity(
      props.characterId,
      selectedDate.value,
      activity.id,
    )
    if (editingId.value === activity.id) editingId.value = null
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('playerSchedule.errors.deleteFailed')
  } finally {
    busyId.value = null
  }
}

function prevDay() { selectedDate.value = shiftDate(selectedDate.value, -1) }
function nextDay() { selectedDate.value = shiftDate(selectedDate.value, 1) }
function jumpToday() { selectedDate.value = todayISO() }

function characterParticipants(activity: ScheduleActivity): string[] {
  return (activity.participant_refs ?? [])
    .filter((ref) => ref.actor_kind === 'character')
    .map((ref) => ref.display_name)
    .filter(Boolean)
}

function extractError(err: unknown): string | null {
  return err instanceof Error ? err.message : null
}
</script>

<template>
  <div class="player-schedule-card">
    <header class="card-head">
      <div class="head-title">
        <span class="title-text">{{ t('playerSchedule.title') }}</span>
        <span class="date-chip">{{ selectedDate }}</span>
      </div>
      <RouterLink
        v-if="characterId && showAdminLink"
        :to="{ name: 'admin-schedule', query: { character: characterId } }"
        class="admin-link"
        :title="t('playerSchedule.advancedTitle')"
      >{{ t('playerSchedule.advancedLink') }}</RouterLink>
    </header>

    <div v-if="characterId" class="date-bar">
      <button
        type="button"
        class="icon-btn"
        :title="t('playerSchedule.previousDay')"
        @click="prevDay"
      >◀</button>
      <input
        v-model="selectedDate"
        type="date"
        class="field-input date-input"
        :aria-label="t('playerSchedule.dateLabel')"
      />
      <button
        type="button"
        class="icon-btn"
        :title="t('playerSchedule.nextDay')"
        @click="nextDay"
      >▶</button>
      <button
        type="button"
        class="chip-btn"
        :disabled="selectedDate === today"
        @click="jumpToday"
      >{{ t('playerSchedule.today') }}</button>
    </div>

    <div v-if="loading" class="state-row">{{ t('common.state.loading') }}</div>
    <div v-else-if="errorMsg" class="state-row state-row--err">{{ errorMsg }}</div>
    <div v-else-if="!characterId" class="state-row">{{ t('playerSchedule.selectCharacter') }}</div>

    <template v-else>
      <div v-if="sortedActivities.length === 0 && !addFormOpen" class="state-row">
        {{ t('playerSchedule.emptyDay') }}
      </div>

      <ul v-else class="activity-list">
        <li
          v-for="activity in sortedActivities"
          :key="activity.id"
          :class="['activity-item', { 'is-current': isCurrent(activity), memorialized: activity.memorialized, editing: editingId === activity.id }]"
        >
          <div class="activity-time">
            <span class="time-range">
              {{ formatClock(activity.start_at) }} – {{ formatClock(activity.end_at) }}
            </span>
            <span v-if="activity.has_memory" class="memorialized-badge">
              {{ t('playerSchedule.rememberedBadge') }}
            </span>
          </div>

          <div v-if="editingId !== activity.id" class="activity-body">
            <div class="activity-desc">
              <span v-if="isCurrent(activity)" class="badge-now">{{ t('playerSchedule.nowBadge') }}</span>
              {{ activity.description }}
            </div>
            <div v-if="activity.location || activity.category" class="activity-meta">
              <span v-if="activity.category" class="meta-chip">{{ activity.category }}</span>
              <span v-if="activity.location" class="meta-chip meta-chip--loc">{{ activity.location }}</span>
            </div>
            <div v-if="characterParticipants(activity).length" class="activity-participants">
              {{ t('schedulePanel.participants.characters', { names: characterParticipants(activity).join(t('common.listSeparator')) }) }}
            </div>
            <div v-else-if="activity.companion_names?.length" class="activity-participants">
              {{ t('schedulePanel.participants.companions', { names: activity.companion_names.join(t('common.listSeparator')) }) }}
            </div>

            <div class="activity-actions">
              <button
                type="button"
                class="chip-btn"
                :disabled="activity.memorialized || busyId === activity.id"
                @click="beginEdit(activity)"
              >{{ activity.memorialized ? t('playerSchedule.lockedAction') : t('common.actions.edit') }}</button>
              <button
                v-if="!activity.memorialized"
                type="button"
                class="chip-btn danger"
                :disabled="busyId === activity.id"
                @click="handleDelete(activity)"
              >{{ t('common.actions.delete') }}</button>
            </div>
          </div>

          <div v-else class="schedule-form">
            <div class="form-title">{{ t('playerSchedule.editTitle') }}</div>
            <div class="form-row">
              <label class="field-small">
                <span>{{ t('playerSchedule.fields.start') }}</span>
                <input v-model="editForm.start" type="time" class="field-input" />
              </label>
              <label class="field-small">
                <span>{{ t('playerSchedule.fields.end') }}</span>
                <input v-model="editForm.end" type="time" class="field-input" />
              </label>
            </div>
            <label class="field-small">
              <span>{{ t('playerSchedule.fields.category') }}</span>
              <input
                v-model="editForm.category"
                type="text"
                class="field-input"
                :placeholder="t('playerSchedule.placeholders.category')"
              />
            </label>
            <label class="field-small">
              <span>{{ t('playerSchedule.fields.description') }}</span>
              <textarea
                v-model="editForm.description"
                class="field-textarea compact-textarea"
                rows="2"
                :placeholder="t('playerSchedule.placeholders.description')"
              />
            </label>
            <label class="field-small">
              <span>{{ t('playerSchedule.fields.location') }}</span>
              <input
                v-model="editForm.location"
                type="text"
                class="field-input"
                :placeholder="t('playerSchedule.placeholders.location')"
              />
            </label>
            <div class="form-actions">
              <button type="button" class="chip-btn" @click="cancelEdit">
                {{ t('common.actions.cancel') }}
              </button>
              <button
                type="button"
                class="chip-btn primary"
                :disabled="busyId === activity.id"
                @click="submitEdit(activity)"
              >{{ busyId === activity.id ? t('common.state.saving') : t('common.actions.save') }}</button>
            </div>
          </div>
        </li>
      </ul>

      <div v-if="addFormOpen" class="schedule-form">
        <div class="form-title">{{ t('playerSchedule.addTitle') }}</div>
        <div class="form-row">
          <label class="field-small">
            <span>{{ t('playerSchedule.fields.start') }}</span>
            <input v-model="addForm.start" type="time" class="field-input" />
          </label>
          <label class="field-small">
            <span>{{ t('playerSchedule.fields.end') }}</span>
            <input v-model="addForm.end" type="time" class="field-input" />
          </label>
        </div>
        <label class="field-small">
          <span>{{ t('playerSchedule.fields.category') }}</span>
          <input
            v-model="addForm.category"
            type="text"
            class="field-input"
            :placeholder="t('playerSchedule.placeholders.category')"
          />
        </label>
        <label class="field-small">
          <span>{{ t('playerSchedule.fields.description') }}</span>
          <textarea
            v-model="addForm.description"
            class="field-textarea compact-textarea"
            rows="2"
            :placeholder="t('playerSchedule.placeholders.description')"
          />
        </label>
        <label class="field-small">
          <span>{{ t('playerSchedule.fields.location') }}</span>
          <input
            v-model="addForm.location"
            type="text"
            class="field-input"
            :placeholder="t('playerSchedule.placeholders.location')"
          />
        </label>
        <div class="form-actions">
          <button type="button" class="chip-btn" @click="closeAddForm">
            {{ t('common.actions.cancel') }}
          </button>
          <button
            type="button"
            class="chip-btn primary"
            :disabled="adding"
            @click="submitAdd"
          >{{ adding ? t('playerSchedule.adding') : t('playerSchedule.addSubmit') }}</button>
        </div>
      </div>

      <button
        v-else
        type="button"
        class="add-btn"
        @click="openAddForm"
      >{{ t('playerSchedule.addOpen') }}</button>
    </template>
  </div>
</template>

<style scoped>
.player-schedule-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--card-padding);
  background: var(--card-bg);
  border: var(--card-border);
  border-radius: var(--card-radius);
}
.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-2);
}
.head-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.title-text {
  font-size: var(--font-md);
  font-weight: 600;
  color: var(--color-primary-light);
}
.date-chip {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.05);
  padding: 1px 6px;
  border-radius: 4px;
}
.admin-link {
  font-size: var(--font-xs);
  color: var(--color-primary);
  text-decoration: none;
}
.admin-link:hover {
  text-decoration: underline;
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
.icon-btn:hover {
  background: rgba(255, 255, 255, 0.1);
}
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
  border-color: var(--color-primary);
  color: white;
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
.state-row {
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  padding: var(--space-3) 0;
  text-align: center;
  line-height: 1.5;
}
.state-row--err {
  color: #f4a3a3;
}
.activity-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.activity-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  padding: var(--space-2);
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid transparent;
}
.activity-item.is-current {
  background: rgba(183, 93, 63, 0.12);
  border-color: rgba(183, 93, 63, 0.4);
}
.activity-item.memorialized {
  background: rgba(255, 255, 255, 0.015);
  opacity: 0.76;
}
.activity-item.editing {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 1px rgba(183, 93, 63, 0.25);
}
.activity-time {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}
.time-range {
  font-size: var(--font-xs);
  color: var(--color-primary-light);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.memorialized-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.09);
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}
.activity-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.activity-desc {
  font-size: var(--font-sm);
  color: var(--color-text);
  line-height: 1.45;
  word-break: break-word;
  white-space: pre-wrap;
}
.badge-now {
  display: inline-block;
  font-size: 10px;
  padding: 1px 5px;
  margin-right: 6px;
  border-radius: 4px;
  background: rgba(183, 93, 63, 0.32);
  color: #ffb892;
  font-weight: 600;
  vertical-align: middle;
}
.activity-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.meta-chip {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--color-text-secondary);
}
.meta-chip--loc {
  background: rgba(107, 153, 178, 0.15);
  color: #8cb4cc;
}
.activity-participants {
  font-size: 11px;
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}
.activity-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 2px;
}
.schedule-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: 1px solid rgba(183, 93, 63, 0.25);
  border-radius: 7px;
  background: rgba(183, 93, 63, 0.06);
}
.form-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-primary-light);
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
  color: var(--color-text-secondary);
  font-size: 11px;
}
.compact-textarea {
  min-height: 56px;
}
.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  flex-wrap: wrap;
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
  transition: background 0.2s, color 0.2s;
}
.add-btn:hover {
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text);
}
</style>
