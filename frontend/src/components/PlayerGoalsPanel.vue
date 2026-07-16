<script setup lang="ts">
/**
 * 玩家側的中期目標面板。
 *
 * 三件事：
 *   1. 顯示當下意圖（read-only，AI 每輪更新）
 *   2. 列出 active / paused / done / abandoned 目標 + 狀態切換
 *   3. 新增目標
 *
 * 從 PlayerSidebar 抽出來；同檔內維護 goals 載入、新增、狀態切換、刪除。
 * 跟 follow-ups 卡片分開放在 sidebar 的 goals tab 內。
 */
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import type { Goal, GoalStatus } from '@/types/goal'
import { createGoal, deleteGoal, listGoals, updateGoal } from '@/utils/api/goals'
import { UiButton } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const props = defineProps<{
  character: Character
}>()

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

const goals = ref<Goal[]>([])
const goalsLoading = ref(false)
const newGoalContent = ref('')
const newGoalPriority = ref(3)
const goalActionBusy = ref<string | null>(null)

const STATUS_LABEL_KEY: Record<GoalStatus, string> = {
  active: 'playerGoals.status.active',
  paused: 'playerGoals.status.paused',
  done: 'playerGoals.status.done',
  abandoned: 'playerGoals.status.abandoned',
}

async function reloadGoals() {
  goalsLoading.value = true
  try {
    goals.value = await listGoals(props.character.id)
  } catch { /* swallow */ }
  finally { goalsLoading.value = false }
}

async function handleCreateGoal() {
  const content = newGoalContent.value.trim()
  if (!content) return
  goalActionBusy.value = 'create'
  try {
    const created = await createGoal(props.character.id, {
      content,
      priority: newGoalPriority.value,
    })
    goals.value = [created, ...goals.value]
    newGoalContent.value = ''
    newGoalPriority.value = 3
  } finally { goalActionBusy.value = null }
}

async function handleUpdateGoalStatus(goal: Goal, status: GoalStatus) {
  goalActionBusy.value = goal.id
  try {
    const updated = await updateGoal(goal.id, { status })
    goals.value = goals.value.map(g => g.id === updated.id ? updated : g)
  } finally { goalActionBusy.value = null }
}

async function handleDeleteGoal(goal: Goal) {
  if (!await confirmDialog({
    content: t('playerGoals.confirmDelete', { content: goal.content }),
    okText: t('common.actions.delete'),
    danger: true,
  })) return
  goalActionBusy.value = goal.id
  try {
    await deleteGoal(goal.id)
    goals.value = goals.value.filter(g => g.id !== goal.id)
  } finally { goalActionBusy.value = null }
}

watch(() => props.character.id, () => { void reloadGoals() }, { immediate: true })
</script>

<template>
  <div class="goals-panel">
    <header class="goals-header">
      <h3 class="section-title">{{ t('playerGoals.title') }}</h3>
      <p class="goals-hint">
        {{ t('playerGoals.hint') }}
      </p>
    </header>

    <div v-if="character.state.current_intent" class="intent-card">
      <div class="intent-label">{{ t('playerGoals.intent.label') }}</div>
      <div class="intent-text">{{ character.state.current_intent }}</div>
      <div class="intent-foot">{{ t('playerGoals.intent.autoUpdated') }}</div>
    </div>

    <div class="goal-create">
      <textarea
        v-model="newGoalContent"
        class="field-textarea"
        rows="2"
        :placeholder="t('playerGoals.create.placeholder')"
      />
      <div class="goal-create-row">
        <label class="field-label goal-priority-label">{{ t('playerGoals.create.priority') }}</label>
        <input
          v-model.number="newGoalPriority"
          type="number"
          min="1"
          max="5"
          class="field-input goal-priority-input"
        />
        <UiButton
          variant="primary"
          size="sm"
          class="goal-add-btn"
          :disabled="goalActionBusy === 'create' || !newGoalContent.trim()"
          @click="handleCreateGoal"
        >{{ t('playerGoals.create.submit') }}</UiButton>
      </div>
    </div>

    <div v-if="goalsLoading" class="goals-empty">{{ t('common.state.loading') }}</div>
    <div v-else-if="goals.length === 0" class="goals-empty">
      {{ t('playerGoals.empty') }}
    </div>
    <div v-else class="goal-list">
      <div
        v-for="goal in goals"
        :key="goal.id"
        :class="['goal-card', `goal-status-${goal.status}`]"
      >
        <div class="goal-body">
          <div class="goal-content">{{ goal.content }}</div>
          <div class="goal-meta">
            <span :class="['goal-badge', `badge-${goal.status}`]">{{ t(STATUS_LABEL_KEY[goal.status]) }}</span>
            <span class="goal-badge badge-priority">{{ t('playerGoals.priorityBadge', { priority: goal.priority }) }}</span>
            <span class="goal-badge badge-origin">{{ goal.origin === 'llm_review' ? t('playerGoals.origin.ai') : t('playerGoals.origin.manual') }}</span>
          </div>
          <div v-if="goal.review_notes" class="goal-notes">{{ goal.review_notes }}</div>
        </div>
        <div class="goal-actions">
          <button
            v-if="goal.status !== 'active'"
            class="goal-action-btn"
            :disabled="goalActionBusy === goal.id"
            :title="t('playerGoals.actions.markActive')"
            @click="handleUpdateGoalStatus(goal, 'active')"
          >▶</button>
          <button
            v-if="goal.status === 'active'"
            class="goal-action-btn"
            :disabled="goalActionBusy === goal.id"
            :title="t('playerGoals.actions.pause')"
            @click="handleUpdateGoalStatus(goal, 'paused')"
          >⏸</button>
          <button
            v-if="goal.status !== 'done'"
            class="goal-action-btn"
            :disabled="goalActionBusy === goal.id"
            :title="t('playerGoals.actions.markDone')"
            @click="handleUpdateGoalStatus(goal, 'done')"
          >✓</button>
          <button
            class="goal-action-btn goal-delete-btn"
            :disabled="goalActionBusy === goal.id"
            :title="t('common.actions.delete')"
            @click="handleDeleteGoal(goal)"
          >×</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.goals-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.goals-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}
.goals-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin: 0;
}
.goals-empty {
  padding: 18px 12px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.02);
  border: 1px dashed var(--color-border);
  border-radius: 8px;
}
.intent-card {
  padding: 10px 12px;
  background: rgba(107, 153, 178, 0.1);
  border: 1px solid rgba(107, 153, 178, 0.35);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.intent-label {
  font-size: 11px;
  font-weight: 600;
  color: #8cb4cc;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.intent-text {
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-text);
}
.intent-foot {
  font-size: 10px;
  color: var(--color-text-secondary);
  font-style: italic;
}
.goal-create {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 8px;
  border: 1px solid var(--color-border);
}
.goal-create-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.goal-priority-label {
  margin: 0;
  white-space: nowrap;
}
.goal-priority-input {
  width: 56px;
  text-align: center;
}
.goal-add-btn {
  flex: 1;
}
.goal-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.goal-card {
  display: flex;
  gap: 8px;
  padding: 10px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  transition: opacity 0.2s;
}
.goal-card.goal-status-done,
.goal-card.goal-status-abandoned {
  opacity: 0.55;
}
.goal-card.goal-status-paused {
  opacity: 0.8;
}
.goal-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.goal-content {
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-text);
  word-break: break-word;
}
.goal-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.goal-badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-secondary);
}
.badge-active {
  background: rgba(72, 159, 115, 0.2);
  color: #7dc49a;
}
.badge-paused {
  background: rgba(230, 162, 60, 0.2);
  color: #e6a23c;
}
.badge-done {
  background: rgba(107, 153, 178, 0.2);
  color: #8cb4cc;
}
.badge-abandoned {
  background: rgba(200, 80, 80, 0.18);
  color: #d58a8a;
}
.badge-priority {
  background: rgba(183, 93, 63, 0.18);
  color: #d89680;
}
.goal-notes {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  padding: 6px 8px;
  background: rgba(0, 0, 0, 0.18);
  border-radius: 4px;
  font-style: italic;
}
.goal-actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
}
.goal-action-btn {
  width: 26px;
  height: 26px;
  border-radius: 4px;
  border: none;
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s, color 0.2s;
}
.goal-action-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.12);
  color: var(--color-text);
}
.goal-action-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.goal-delete-btn:hover:not(:disabled) {
  background: rgba(231, 76, 60, 0.25);
  color: #ff8a75;
}
</style>
