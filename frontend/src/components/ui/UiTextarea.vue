<script setup lang="ts">
import { computed, useId } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue?: string | null
    label?: string
    hint?: string
    placeholder?: string
    disabled?: boolean
    readonly?: boolean
    required?: boolean
    rows?: number
    maxlength?: number
    textareaId?: string
  }>(),
  {
    modelValue: '',
    disabled: false,
    readonly: false,
    required: false,
    rows: 3,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  blur: [event: FocusEvent]
  focus: [event: FocusEvent]
}>()

const autoId = useId()
const textareaId = computed(() => props.textareaId ?? `ui-textarea-${autoId}`)

function onInput(event: Event) {
  const target = event.target as HTMLTextAreaElement
  emit('update:modelValue', target.value)
}
</script>

<template>
  <div class="ui-textarea">
    <label v-if="label" :for="textareaId" class="field-label">
      {{ label }}
      <span v-if="required" class="ui-textarea__required" aria-hidden="true">*</span>
    </label>
    <textarea
      :id="textareaId"
      :value="modelValue ?? ''"
      :placeholder="placeholder"
      :disabled="disabled"
      :readonly="readonly"
      :required="required"
      :rows="rows"
      :maxlength="maxlength"
      class="field-textarea"
      @input="onInput"
      @blur="emit('blur', $event)"
      @focus="emit('focus', $event)"
    />
    <div v-if="hint" class="field-hint">{{ hint }}</div>
  </div>
</template>

<style scoped>
.ui-textarea {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  width: 100%;
}
.ui-textarea__required {
  color: #f4a3a3;
  margin-left: 2px;
}
</style>
