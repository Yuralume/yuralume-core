<script setup lang="ts">
/**
 * Searchable single-value combobox for long option lists (e.g. a
 * provider's hundreds of models). An `<input>` filters an anchored
 * dropdown by case-insensitive substring; the operator can also type a
 * value that isn't in the list (``allowCustom``), which matters because
 * the fetched model list can be incomplete for providers like
 * OpenRouter. Dark-theme safe: the panel uses a solid surface colour so
 * it never falls back to the white-on-white native `<option>` trap.
 */
import { computed, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { filterOptions } from '@/utils/comboboxFilter'

const props = withDefaults(
  defineProps<{
    modelValue?: string | null
    options?: string[]
    placeholder?: string
    disabled?: boolean
    loading?: boolean
    clearable?: boolean
    allowCustom?: boolean
    maxVisible?: number
    inputId?: string
    ariaLabel?: string
  }>(),
  {
    modelValue: '',
    options: () => [],
    disabled: false,
    loading: false,
    clearable: true,
    allowCustom: true,
    maxVisible: 100,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const { t } = useI18n()

const autoId = useId()
const inputId = computed(() => props.inputId ?? `ui-combobox-${autoId}`)

const wrapperRef = ref<HTMLElement | null>(null)
const listboxId = computed(() => `${inputId.value}-listbox`)

/** Text shown in the input. Mirrors ``modelValue`` unless the operator
 * is actively editing (dropdown open). */
const inputText = ref(props.modelValue ?? '')
const open = ref(false)
/** True once the operator types, so focus-open shows the whole list but
 * subsequent keystrokes filter it. */
const filtering = ref(false)
const highlighted = ref(-1)

watch(
  () => props.modelValue,
  (next) => {
    if (!open.value) inputText.value = next ?? ''
  },
)

const filtered = computed(() =>
  filtering.value ? filterOptions(props.options, inputText.value) : [...props.options],
)
const visible = computed(() => filtered.value.slice(0, props.maxVisible))
const overflowCount = computed(() =>
  Math.max(0, filtered.value.length - visible.value.length),
)

function openDropdown(): void {
  if (props.disabled) return
  open.value = true
  filtering.value = false
  highlighted.value = -1
}

function onInput(event: Event): void {
  inputText.value = (event.target as HTMLInputElement).value
  filtering.value = true
  open.value = true
  highlighted.value = filtered.value.length > 0 ? 0 : -1
}

function commit(value: string): void {
  const next = value
  inputText.value = next
  open.value = false
  filtering.value = false
  highlighted.value = -1
  if (next !== (props.modelValue ?? '')) emit('update:modelValue', next)
}

function selectOption(option: string): void {
  // `@mousedown.prevent` on the option keeps focus on the input, so we
  // don't refocus here — doing so would re-fire ``@focus`` and reopen the
  // just-closed dropdown.
  commit(option)
}

function clear(): void {
  commit('')
}

/** Close the dropdown when focus/pointer leaves. When ``allowCustom`` we
 * accept the typed text; otherwise we revert to the last committed value
 * so the field can never hold an invalid model id. */
function close(): void {
  open.value = false
  filtering.value = false
  highlighted.value = -1
  if (props.allowCustom) {
    const typed = inputText.value.trim()
    if (typed !== (props.modelValue ?? '')) emit('update:modelValue', typed)
    inputText.value = typed
  } else {
    inputText.value = props.modelValue ?? ''
  }
}

function onFocusOut(event: FocusEvent): void {
  const nextTarget = event.relatedTarget as Node | null
  if (nextTarget && wrapperRef.value?.contains(nextTarget)) return
  close()
}

function moveHighlight(delta: number): void {
  if (!open.value) {
    openDropdown()
    return
  }
  const count = visible.value.length
  if (count === 0) return
  const start = highlighted.value < 0 ? (delta > 0 ? -1 : 0) : highlighted.value
  highlighted.value = (start + delta + count) % count
}

function onEnter(): void {
  if (open.value && highlighted.value >= 0 && visible.value[highlighted.value] !== undefined) {
    selectOption(visible.value[highlighted.value])
    return
  }
  if (props.allowCustom) {
    commit(inputText.value.trim())
  }
}

function onEscape(): void {
  inputText.value = props.modelValue ?? ''
  open.value = false
  filtering.value = false
  highlighted.value = -1
}
</script>

<template>
  <div
    ref="wrapperRef"
    class="ui-combobox"
    :class="{ 'is-disabled': disabled }"
    @focusout="onFocusOut"
  >
    <div class="ui-combobox__field">
      <input
        :id="inputId"
        :value="inputText"
        type="text"
        class="field-input ui-combobox__input"
        role="combobox"
        aria-autocomplete="list"
        :aria-expanded="open"
        :aria-controls="listboxId"
        :aria-label="ariaLabel"
        :placeholder="placeholder"
        :disabled="disabled"
        autocomplete="off"
        spellcheck="false"
        @focus="openDropdown"
        @input="onInput"
        @keydown.down.prevent="moveHighlight(1)"
        @keydown.up.prevent="moveHighlight(-1)"
        @keydown.enter.prevent="onEnter"
        @keydown.esc.prevent="onEscape"
      />
      <button
        v-if="clearable && !disabled && inputText"
        type="button"
        class="ui-combobox__clear"
        :title="t('common.combobox.clear')"
        :aria-label="t('common.combobox.clear')"
        @mousedown.prevent="clear"
      >×</button>
    </div>

    <ul
      v-if="open"
      :id="listboxId"
      class="ui-combobox__panel"
      role="listbox"
    >
      <li v-if="loading" class="ui-combobox__status">
        {{ t('common.state.loading') }}
      </li>
      <li
        v-else-if="visible.length === 0"
        class="ui-combobox__status"
      >
        {{ t('common.combobox.noResults') }}
      </li>
      <template v-else>
        <li
          v-for="(option, index) in visible"
          :key="option"
          class="ui-combobox__option"
          :class="{
            'is-active': index === highlighted,
            'is-selected': option === (modelValue ?? ''),
          }"
          role="option"
          :aria-selected="option === (modelValue ?? '')"
          @mousedown.prevent="selectOption(option)"
          @mouseenter="highlighted = index"
        >{{ option }}</li>
        <li v-if="overflowCount > 0" class="ui-combobox__status">
          {{ t('common.combobox.moreHint', { count: overflowCount }) }}
        </li>
      </template>
    </ul>
  </div>
</template>

<style scoped>
.ui-combobox {
  position: relative;
  width: 100%;
}
.ui-combobox__field {
  position: relative;
  display: flex;
  align-items: center;
}
.ui-combobox__input {
  padding-right: 28px;
}
.ui-combobox__clear {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 15px;
  line-height: 1;
}
.ui-combobox__clear:hover {
  color: var(--color-text);
  background: rgba(255, 255, 255, 0.08);
}
.ui-combobox__panel {
  position: absolute;
  z-index: 40;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  margin: 0;
  padding: 4px;
  list-style: none;
  max-height: 260px;
  overflow-y: auto;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}
.ui-combobox__option {
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ui-combobox__option.is-active {
  background: var(--color-surface-light);
}
.ui-combobox__option.is-selected {
  color: var(--color-primary-light);
}
.ui-combobox__status {
  padding: 6px 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
</style>
