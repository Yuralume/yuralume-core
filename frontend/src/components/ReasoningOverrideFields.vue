<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  emptyReasoningOverride,
  hasReasoningOverride,
  type FeatureReasoningOverride,
} from '@/utils/api/system'

/** Compact editor for one routing entry's reasoning override.
 *
 * Used by ``FeatureModelsPicker`` on group rows and advanced feature
 * rows. Emits ``null`` when the operator clears every field so the
 * parent entry collapses back to "inherit connection settings" —
 * mirroring the backend's write-side normalisation. */
const props = defineProps<{
  modelValue: FeatureReasoningOverride | null | undefined
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: FeatureReasoningOverride | null): void
}>()

const { t } = useI18n()

const active = computed(() => hasReasoningOverride(props.modelValue))

function current(): FeatureReasoningOverride {
  return props.modelValue
    ? { ...props.modelValue }
    : emptyReasoningOverride()
}

function push(next: FeatureReasoningOverride) {
  emit('update:modelValue', hasReasoningOverride(next) ? next : null)
}

function onDisableChange(checked: boolean) {
  push({ ...current(), disable_reasoning: checked })
}

function onEffortChange(value: string) {
  push({ ...current(), reasoning_effort: value.trim() ? value : null })
}

function onBudgetChange(value: string) {
  const parsed = Number.parseInt(value, 10)
  push({
    ...current(),
    thinking_budget_tokens:
      Number.isFinite(parsed) && parsed > 0 ? parsed : null,
  })
}
</script>

<template>
  <details class="reasoning-fields" :open="active">
    <summary class="reasoning-summary">
      {{ t('featureModelsPicker.reasoning.title') }}
      <span v-if="active" class="reasoning-badge">
        {{ t('featureModelsPicker.reasoning.activeBadge') }}
      </span>
    </summary>
    <p class="reasoning-hint">{{ t('featureModelsPicker.reasoning.hint') }}</p>
    <label class="reasoning-checkbox">
      <input
        type="checkbox"
        :checked="modelValue?.disable_reasoning ?? false"
        @change="onDisableChange(($event.target as HTMLInputElement).checked)"
      />
      {{ t('featureModelsPicker.reasoning.disableLabel') }}
    </label>
    <div class="reasoning-inputs">
      <label class="reasoning-field">
        <span class="field-label">
          {{ t('featureModelsPicker.reasoning.effortLabel') }}
        </span>
        <input
          type="text"
          class="field-input"
          :value="modelValue?.reasoning_effort ?? ''"
          :placeholder="t('featureModelsPicker.reasoning.effortPlaceholder')"
          @input="onEffortChange(($event.target as HTMLInputElement).value)"
        />
      </label>
      <label class="reasoning-field">
        <span class="field-label">
          {{ t('featureModelsPicker.reasoning.budgetLabel') }}
        </span>
        <input
          type="number"
          class="field-input"
          min="1"
          :value="modelValue?.thinking_budget_tokens ?? ''"
          :placeholder="t('featureModelsPicker.reasoning.budgetPlaceholder')"
          @input="onBudgetChange(($event.target as HTMLInputElement).value)"
        />
      </label>
    </div>
  </details>
</template>

<style scoped>
.reasoning-fields {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.reasoning-summary {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
}

.reasoning-badge {
  border: 1px solid var(--color-primary);
  border-radius: 999px;
  padding: 0 6px;
  color: var(--color-primary);
  font-size: 11px;
  line-height: 16px;
}

.reasoning-hint {
  margin: 0;
  line-height: 1.45;
}

.reasoning-checkbox {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}

.reasoning-inputs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.reasoning-field {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 180px;
}
</style>
