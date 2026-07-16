<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import type { Character, ProactiveRhythm } from '@/types/character'
import { characterDisplayRef } from '@/utils/characterDisplay'
import {
  updateCharacter,
  updateCharacterProactiveRhythm,
} from '@/utils/api/characters'

const props = defineProps<{
  character: Character
}>()

const emit = defineEmits<{
  updated: [character: Character]
}>()

const { t } = useI18n()
const RHYTHM_OPTIONS: ProactiveRhythm[] = ['quiet', 'balanced', 'lively']
const characterName = computed(() => characterDisplayRef(props.character, t('common.character')))
const enabled = ref(props.character.proactive_enabled ?? true)
const rhythm = ref<ProactiveRhythm>(props.character.proactive_rhythm ?? 'balanced')
const savingEnabled = ref(false)
const savingRhythm = ref(false)

watch(
  () => [
    props.character.id,
    props.character.proactive_enabled,
    props.character.proactive_rhythm,
  ] as const,
  () => {
    if (!savingEnabled.value && !savingRhythm.value) {
      enabled.value = props.character.proactive_enabled ?? true
      rhythm.value = props.character.proactive_rhythm ?? 'balanced'
    }
  },
  { immediate: true },
)

async function onToggle(event: Event) {
  const next = (event.target as HTMLInputElement).checked
  const previous = enabled.value
  enabled.value = next
  savingEnabled.value = true
  try {
    const updated = await updateCharacter(props.character.id, {
      proactive_enabled: next,
    })
    enabled.value = updated.proactive_enabled
    emit('updated', updated)
  } catch (error) {
    enabled.value = previous
    notification.error({
      message: t('playerSidebar.proactiveMessages.settingsFailedTitle'),
      description: error instanceof Error
        ? error.message
        : t('playerSidebar.proactiveMessages.settingsFailedDescDefault'),
      duration: 3,
    })
  } finally {
    savingEnabled.value = false
  }
}

async function selectRhythm(next: ProactiveRhythm) {
  if (next === rhythm.value || savingRhythm.value) return
  const previous = rhythm.value
  rhythm.value = next
  savingRhythm.value = true
  try {
    const updated = await updateCharacterProactiveRhythm(props.character.id, next)
    rhythm.value = updated.proactive_rhythm ?? next
    emit('updated', updated)
  } catch (error) {
    rhythm.value = previous
    notification.error({
      message: t('playerSidebar.proactiveMessages.settingsFailedTitle'),
      description: error instanceof Error
        ? error.message
        : t('playerSidebar.proactiveMessages.settingsFailedDescDefault'),
      duration: 3,
    })
  } finally {
    savingRhythm.value = false
  }
}
</script>

<template>
  <div class="proactive-message-setting">
    <label class="proactive-message-toggle" :class="{ 'is-saving': savingEnabled }">
      <input
        type="checkbox"
        :checked="enabled"
        :disabled="savingEnabled"
        @change="onToggle"
      />
      <span class="proactive-message-switch" aria-hidden="true">
        <span class="proactive-message-knob" />
      </span>
      <span class="proactive-message-label">
        {{ t('playerSidebar.proactiveMessages.label', { name: characterName }) }}
      </span>
    </label>
    <p class="proactive-message-hint">
      {{ t('playerSidebar.proactiveMessages.hint', { name: characterName }) }}
    </p>

    <div class="proactive-rhythm">
      <div class="proactive-rhythm-head">
        <span class="proactive-rhythm-title">
          {{ t('playerSidebar.proactiveMessages.rhythmLabel', { name: characterName }) }}
        </span>
        <span v-if="savingRhythm" class="proactive-rhythm-saving">
          {{ t('common.state.saving') }}
        </span>
      </div>
      <div
        class="proactive-rhythm-options"
        role="radiogroup"
        :aria-label="t('playerSidebar.proactiveMessages.rhythmLabel', { name: characterName })"
      >
        <button
          v-for="option in RHYTHM_OPTIONS"
          :key="option"
          type="button"
          :class="['proactive-rhythm-option', { active: rhythm === option }]"
          role="radio"
          :aria-checked="rhythm === option"
          :disabled="savingRhythm"
          @click="selectRhythm(option)"
        >
          <span class="proactive-rhythm-option__label">
            {{ t(`playerSidebar.proactiveMessages.rhythmOptions.${option}.label`) }}
          </span>
          <span class="proactive-rhythm-option__hint">
            {{ t(`playerSidebar.proactiveMessages.rhythmOptions.${option}.hint`) }}
          </span>
        </button>
      </div>
      <p class="proactive-message-hint">
        {{ t('playerSidebar.proactiveMessages.rhythmHint', { name: characterName }) }}
      </p>
    </div>
  </div>
</template>

<style scoped>
.proactive-message-setting {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.proactive-message-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
}

.proactive-message-toggle input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.proactive-message-switch {
  position: relative;
  width: 32px;
  height: 18px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.08);
  flex: 0 0 auto;
  transition: background 0.15s, border-color 0.15s;
}

.proactive-message-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  transition: transform 0.15s, background 0.15s;
}

.proactive-message-toggle input:checked + .proactive-message-switch {
  border-color: rgba(106, 169, 240, 0.65);
  background: rgba(106, 169, 240, 0.22);
}

.proactive-message-toggle input:checked + .proactive-message-switch .proactive-message-knob {
  transform: translateX(14px);
  background: #6aa9f0;
}

.proactive-message-toggle.is-saving {
  opacity: 0.55;
  cursor: wait;
}

.proactive-message-hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.proactive-rhythm {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.proactive-rhythm-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.proactive-rhythm-title {
  color: var(--color-text);
  font-size: 12px;
  font-weight: 600;
}

.proactive-rhythm-saving {
  color: var(--color-text-secondary);
  font-size: 11px;
}

.proactive-rhythm-options {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
}

.proactive-rhythm-option {
  min-width: 0;
  min-height: 62px;
  padding: 8px 6px;
  border: 1px solid var(--color-border);
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-secondary);
  cursor: pointer;
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 4px;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}

.proactive-rhythm-option:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text);
}

.proactive-rhythm-option.active {
  border-color: rgba(106, 169, 240, 0.7);
  background: rgba(106, 169, 240, 0.16);
  color: var(--color-text);
}

.proactive-rhythm-option:disabled {
  opacity: 0.55;
  cursor: wait;
}

.proactive-rhythm-option__label {
  font-size: 12px;
  font-weight: 700;
  line-height: 1.2;
}

.proactive-rhythm-option__hint {
  color: var(--color-text-secondary);
  font-size: 10px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
</style>
