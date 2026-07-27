<script setup lang="ts">
/**
 * City picker for the hosted "where I live" surfaces (plan G2).
 *
 * Shared by the first-login confirmation gate and the settings panel so
 * both spend the same debounce, the same fail-soft empty list, and the
 * same wording. A player types a place name; picking a suggestion emits
 * the ready-to-store facts (label, coordinates, country, suggested
 * timezone) — latitude / longitude never reach the player's eyes.
 *
 * The text and those facts must never drift apart. Because the coordinates
 * are invisible, a player who picks "Taipei" and then types over the field
 * has no way to see that Taipei's latitude is still attached — so every
 * keystroke emits `deselect` and the parent drops what it was holding.
 * Typing a place name without picking a suggestion stays supported; it just
 * travels as a bare label, which is the honest representation of "I typed
 * this myself".
 *
 * Dropdown behaviour mirrors `UiCombobox`: options commit on
 * `mousedown.prevent` so the blur-close cannot race the click.
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { CITY_SEARCH_MIN_LENGTH, useCitySearch } from '@/composables/useCitySearch'
import type { GeoSearchResult } from '@/types/playerLocale'

const props = withDefaults(
  defineProps<{
    modelValue?: string
    label?: string
    placeholder?: string
    disabled?: boolean
    inputId?: string
  }>(),
  {
    modelValue: '',
    disabled: false,
    inputId: 'player-city-search',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  select: [place: GeoSearchResult]
  /** The text no longer stands for a picked place — drop its facts. */
  deselect: []
}>()

const { t } = useI18n()
const { results, searching, searched, search, reset } = useCitySearch()

const wrapperRef = ref<HTMLElement | null>(null)
const open = ref(false)

function onInput(event: Event): void {
  const value = (event.target as HTMLInputElement).value
  // Emitted on every keystroke rather than only when this field remembers
  // having emitted a `select`: the parent can be holding facts that never
  // came from here — the first-login gate seeds label *and* coordinates
  // straight off the GeoIP guess. Local "is something selected" state would
  // therefore be a lie, so the parent owns the facts and this field only
  // reports that the text stopped standing for them.
  emit('update:modelValue', value)
  emit('deselect')
  open.value = true
  search(value)
}

function choose(place: GeoSearchResult): void {
  emit('update:modelValue', place.label)
  emit('select', place)
  open.value = false
  reset()
}

function onFocusOut(event: FocusEvent): void {
  const next = event.relatedTarget as Node | null
  if (next && wrapperRef.value?.contains(next)) return
  open.value = false
}

const showPanel = computed(() => open.value && !props.disabled)
const needsMoreInput = computed(
  () => (props.modelValue ?? '').trim().length < CITY_SEARCH_MIN_LENGTH,
)
</script>

<template>
  <div ref="wrapperRef" class="city-search" @focusout="onFocusOut">
    <label v-if="label" class="field-label" :for="inputId">{{ label }}</label>
    <input
      :id="inputId"
      :value="modelValue"
      type="text"
      class="field-input"
      autocomplete="off"
      spellcheck="false"
      :placeholder="placeholder ?? t('playerLocale.city.placeholder')"
      :disabled="disabled"
      @input="onInput"
      @focus="open = true"
    />
    <ul v-if="showPanel" class="city-search__panel">
      <li v-if="searching" class="city-search__status">
        {{ t('playerLocale.city.searching') }}
      </li>
      <li
        v-else-if="!searched && needsMoreInput"
        class="city-search__status"
      >
        {{ t('playerLocale.city.typeMore') }}
      </li>
      <li v-else-if="results.length === 0" class="city-search__status">
        {{ t('playerLocale.city.noResults') }}
      </li>
      <template v-else>
        <li
          v-for="place in results"
          :key="`${place.label}:${place.latitude}:${place.longitude}`"
          class="city-search__option"
          @mousedown.prevent="choose(place)"
        >{{ place.label }}</li>
      </template>
    </ul>
  </div>
</template>

<style scoped>
.city-search {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
}

.city-search__panel {
  position: absolute;
  z-index: 40;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  margin: 0;
  padding: 4px;
  list-style: none;
  max-height: 240px;
  overflow-y: auto;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}

.city-search__option {
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
}

.city-search__option:hover {
  background: var(--color-surface-light);
}

.city-search__status {
  padding: 6px 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
</style>
