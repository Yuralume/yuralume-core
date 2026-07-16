<script setup lang="ts">
import { computed, useId } from 'vue'

export interface UiSelectOption {
  value: string | number
  label: string
  disabled?: boolean
}

const props = withDefaults(
  defineProps<{
    modelValue?: string | number | null
    options?: UiSelectOption[]
    label?: string
    hint?: string
    placeholder?: string
    disabled?: boolean
    required?: boolean
    selectId?: string
  }>(),
  {
    modelValue: '',
    options: () => [],
    disabled: false,
    required: false,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  change: [event: Event]
}>()

const autoId = useId()
const selectId = computed(() => props.selectId ?? `ui-select-${autoId}`)

function onChange(event: Event) {
  const target = event.target as HTMLSelectElement
  emit('update:modelValue', target.value)
  emit('change', event)
}
</script>

<template>
  <div class="ui-select">
    <label v-if="label" :for="selectId" class="field-label">
      {{ label }}
      <span v-if="required" class="ui-select__required" aria-hidden="true">*</span>
    </label>
    <select
      :id="selectId"
      :value="modelValue ?? ''"
      :disabled="disabled"
      :required="required"
      class="field-select"
      @change="onChange"
    >
      <option v-if="placeholder" value="" disabled>{{ placeholder }}</option>
      <slot>
        <option
          v-for="opt in options"
          :key="opt.value"
          :value="opt.value"
          :disabled="opt.disabled"
        >{{ opt.label }}</option>
      </slot>
    </select>
    <div v-if="hint" class="field-hint">{{ hint }}</div>
  </div>
</template>

<style scoped>
.ui-select {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  width: 100%;
}
.ui-select__required {
  color: #f4a3a3;
  margin-left: 2px;
}
</style>
