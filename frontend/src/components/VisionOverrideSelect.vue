<script setup lang="ts">
import { useI18n } from 'vue-i18n'

/** Compact tri-state selector for one routing entry's vision override.
 *
 * ``null`` = inherit the provider connection's ``supports_vision`` flag;
 * ``true`` / ``false`` pin it for calls routed through the entry (one
 * aggregator connection fronts both vision and text-only models, so the
 * connection flag alone can't be right for every route). Used on the
 * active-model row, group rows and advanced feature rows of
 * ``FeatureModelsPicker``. */
const props = defineProps<{
  modelValue: boolean | null | undefined
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean | null): void
}>()

const { t } = useI18n()

function currentValue(): string {
  if (props.modelValue === true) return 'true'
  if (props.modelValue === false) return 'false'
  return ''
}

function onChange(raw: string) {
  if (raw === 'true') emit('update:modelValue', true)
  else if (raw === 'false') emit('update:modelValue', false)
  else emit('update:modelValue', null)
}
</script>

<template>
  <label class="vision-override">
    <span class="field-label">{{ t('featureModelsPicker.vision.label') }}</span>
    <select
      class="field-select"
      :value="currentValue()"
      @change="onChange(($event.target as HTMLSelectElement).value)"
    >
      <option value="">{{ t('featureModelsPicker.vision.inherit') }}</option>
      <option value="true">{{ t('featureModelsPicker.vision.supported') }}</option>
      <option value="false">{{ t('featureModelsPicker.vision.textOnly') }}</option>
    </select>
  </label>
</template>

<style scoped>
.vision-override {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
</style>
