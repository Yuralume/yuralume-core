<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import {
  getTTSPregenerationPreference,
  setTTSPregenerationPreference,
} from '@/utils/api/system'

const { t } = useI18n()

const enabled = ref(false)
const saving = ref(false)

async function load() {
  try {
    const pref = await getTTSPregenerationPreference()
    enabled.value = pref.enabled
  } catch {
    enabled.value = false
  }
}

async function onToggle(event: Event) {
  const target = event.target as HTMLInputElement
  const next = target.checked
  const previous = enabled.value
  enabled.value = next
  saving.value = true
  try {
    const pref = await setTTSPregenerationPreference({ enabled: next })
    enabled.value = pref.enabled
  } catch (error) {
    enabled.value = previous
    notification.error({
      message: t('chat.tts.settingsFailedTitle'),
      description: error instanceof Error ? error.message : t('chat.tts.settingsFailedDescDefault'),
      duration: 3,
    })
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="tts-pregen-setting">
    <label class="tts-pregen-toggle" :class="{ 'is-saving': saving }">
      <input
        type="checkbox"
        :checked="enabled"
        :disabled="saving"
        @change="onToggle"
      />
      <span class="tts-pregen-switch" aria-hidden="true">
        <span class="tts-pregen-knob" />
      </span>
      <span class="tts-pregen-label">{{ t('playerSidebar.voicePregen.label') }}</span>
    </label>
    <p class="tts-pregen-hint">{{ t('playerSidebar.voicePregen.hint') }}</p>
  </div>
</template>

<style scoped>
.tts-pregen-setting {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tts-pregen-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
}

.tts-pregen-toggle input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.tts-pregen-switch {
  position: relative;
  width: 32px;
  height: 18px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.08);
  flex: 0 0 auto;
  transition: background 0.15s, border-color 0.15s;
}

.tts-pregen-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  transition: transform 0.15s, background 0.15s;
}

.tts-pregen-toggle input:checked + .tts-pregen-switch {
  border-color: rgba(106, 169, 240, 0.65);
  background: rgba(106, 169, 240, 0.22);
}

.tts-pregen-toggle input:checked + .tts-pregen-switch .tts-pregen-knob {
  transform: translateX(14px);
  background: #6aa9f0;
}

.tts-pregen-toggle.is-saving {
  opacity: 0.55;
  cursor: wait;
}

.tts-pregen-hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}
</style>
