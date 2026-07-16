<script setup lang="ts">
import { computed, useId } from 'vue'

type InputType = 'text' | 'number' | 'email' | 'password' | 'search' | 'tel' | 'url' | 'date' | 'time' | 'datetime-local'

const props = withDefaults(
  defineProps<{
    modelValue?: string | number | null
    label?: string
    hint?: string
    placeholder?: string
    type?: InputType
    disabled?: boolean
    readonly?: boolean
    required?: boolean
    min?: number | string
    max?: number | string
    step?: number | string
    autocomplete?: string
    inputId?: string
  }>(),
  {
    modelValue: '',
    type: 'text',
    disabled: false,
    readonly: false,
    required: false,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string | number]
  blur: [event: FocusEvent]
  focus: [event: FocusEvent]
}>()

const autoId = useId()
const inputId = computed(() => props.inputId ?? `ui-input-${autoId}`)

function onInput(event: Event) {
  const target = event.target as HTMLInputElement
  const raw = target.value
  if (props.type === 'number') {
    emit('update:modelValue', raw === '' ? '' : Number(raw))
  } else {
    emit('update:modelValue', raw)
  }
}
</script>

<template>
  <div class="ui-input">
    <label v-if="label" :for="inputId" class="field-label">
      {{ label }}
      <span v-if="required" class="ui-input__required" aria-hidden="true">*</span>
    </label>
    <input
      :id="inputId"
      :type="type"
      :value="modelValue ?? ''"
      :placeholder="placeholder"
      :disabled="disabled"
      :readonly="readonly"
      :required="required"
      :min="min"
      :max="max"
      :step="step"
      :autocomplete="autocomplete"
      class="field-input"
      @input="onInput"
      @blur="emit('blur', $event)"
      @focus="emit('focus', $event)"
    />
    <div v-if="hint" class="field-hint">{{ hint }}</div>
  </div>
</template>

<style scoped>
.ui-input {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  width: 100%;
}
.ui-input__required {
  color: #f4a3a3;
  margin-left: 2px;
}
</style>
