<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { useSceneAccessHintPreference } from '@/composables/useSceneAccessHintPreference'

const { t } = useI18n()
const {
  sceneAccessHintEnabled,
  loadSceneAccessHintPreference,
  saveSceneAccessHintPreference,
} = useSceneAccessHintPreference()

const saving = ref(false)

async function onToggle(event: Event) {
  const target = event.target as HTMLInputElement
  const next = target.checked
  const previous = sceneAccessHintEnabled.value
  sceneAccessHintEnabled.value = next
  saving.value = true
  try {
    await saveSceneAccessHintPreference(next)
  } catch (error) {
    sceneAccessHintEnabled.value = previous
    notification.error({
      message: t('playerSidebar.sceneAccessHint.settingsFailedTitle'),
      description: error instanceof Error
        ? error.message
        : t('playerSidebar.sceneAccessHint.settingsFailedDescDefault'),
      duration: 3,
    })
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadSceneAccessHintPreference()
})
</script>

<template>
  <div class="scene-access-hint-setting">
    <label class="scene-access-hint-toggle" :class="{ 'is-saving': saving }">
      <input
        type="checkbox"
        :checked="sceneAccessHintEnabled"
        :disabled="saving"
        @change="onToggle"
      />
      <span class="scene-access-hint-switch" aria-hidden="true">
        <span class="scene-access-hint-knob" />
      </span>
      <span class="scene-access-hint-label">{{ t('playerSidebar.sceneAccessHint.label') }}</span>
    </label>
    <p class="scene-access-hint-hint">{{ t('playerSidebar.sceneAccessHint.hint') }}</p>
  </div>
</template>

<style scoped>
.scene-access-hint-setting {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.scene-access-hint-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
}

.scene-access-hint-toggle input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.scene-access-hint-switch {
  position: relative;
  width: 32px;
  height: 18px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.08);
  flex: 0 0 auto;
  transition: background 0.15s, border-color 0.15s;
}

.scene-access-hint-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  transition: transform 0.15s, background 0.15s;
}

.scene-access-hint-toggle input:checked + .scene-access-hint-switch {
  border-color: rgba(106, 169, 240, 0.65);
  background: rgba(106, 169, 240, 0.22);
}

.scene-access-hint-toggle input:checked + .scene-access-hint-switch .scene-access-hint-knob {
  transform: translateX(14px);
  background: #6aa9f0;
}

.scene-access-hint-toggle.is-saving {
  opacity: 0.55;
  cursor: wait;
}

.scene-access-hint-hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}
</style>
