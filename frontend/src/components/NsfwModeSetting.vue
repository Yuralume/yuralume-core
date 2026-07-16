<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { useAuth } from '@/composables/useAuth'
import { useNsfwMode } from '@/composables/useNsfwMode'

const { t } = useI18n()
const { cloudMode } = useAuth()
const {
  status,
  active,
  configured,
  loading,
  saving,
  remainingSeconds,
  loadNsfwMode,
  enableNsfwMode,
  disableNsfwMode,
} = useNsfwMode()

const remainingMinutes = computed(() => {
  if (!active.value || remainingSeconds.value == null) return null
  return Math.max(1, Math.ceil(remainingSeconds.value / 60))
})

const toggleDisabled = computed(() => (
  cloudMode.value
  || loading.value
  || saving.value
  || !status.value
  || (!active.value && !configured.value)
))

const statusLabel = computed(() => {
  if (loading.value && !status.value) return t('nsfwModeSetting.status.loading')
  if (active.value && remainingMinutes.value != null) {
    return t('nsfwModeSetting.status.activeWithMinutes', {
      minutes: remainingMinutes.value,
    })
  }
  if (active.value) return t('nsfwModeSetting.status.active')
  if (configured.value) return t('nsfwModeSetting.status.ready')
  return t('nsfwModeSetting.status.inactive')
})

async function handleToggle(event: Event): Promise<void> {
  const checked = (event.target as HTMLInputElement).checked
  try {
    if (!checked) {
      await disableNsfwMode()
      notification.success({
        message: t('nsfwModeSetting.notifications.disabled'),
        duration: 2,
      })
      return
    }
    await enableNsfwMode()
    notification.success({
      message: t('nsfwModeSetting.notifications.enabled'),
      duration: 2,
    })
  } catch (error) {
    notification.error({
      message: t('nsfwModeSetting.errors.switchFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  }
}

onMounted(() => {
  if (!cloudMode.value) {
    void loadNsfwMode()
  }
})
</script>

<template>
  <div v-if="!cloudMode" class="nsfw-mode-setting">
    <div class="nsfw-mode-setting__header">
      <label class="nsfw-mode-setting__toggle" :class="{ 'is-saving': saving }">
        <input
          type="checkbox"
          :checked="active"
          :disabled="toggleDisabled"
          @change="handleToggle"
        />
        <span class="nsfw-mode-setting__switch" aria-hidden="true">
          <span class="nsfw-mode-setting__knob" />
        </span>
        <span class="nsfw-mode-setting__title">{{ t('nsfwModeSetting.label') }}</span>
      </label>
      <span
        class="nsfw-mode-setting__status"
        :class="{ 'is-active': active, 'is-ready': !active && configured }"
      >
        {{ statusLabel }}
      </span>
    </div>

    <p class="field-hint">
      {{
        configured
          ? t('nsfwModeSetting.hint.configured')
          : t('nsfwModeSetting.hint.unconfigured')
      }}
    </p>
  </div>
</template>

<style scoped>
.nsfw-mode-setting {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.nsfw-mode-setting__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.nsfw-mode-setting__toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
  min-width: 0;
}

.nsfw-mode-setting__toggle input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.nsfw-mode-setting__switch {
  position: relative;
  width: 32px;
  height: 18px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.08);
  flex: 0 0 auto;
  transition: background 0.15s, border-color 0.15s;
}

.nsfw-mode-setting__knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  transition: transform 0.15s, background 0.15s;
}

.nsfw-mode-setting__toggle input:checked + .nsfw-mode-setting__switch {
  border-color: rgba(235, 98, 113, 0.7);
  background: rgba(235, 98, 113, 0.24);
}

.nsfw-mode-setting__toggle input:checked + .nsfw-mode-setting__switch .nsfw-mode-setting__knob {
  transform: translateX(14px);
  background: #eb6271;
}

.nsfw-mode-setting__toggle.is-saving {
  opacity: 0.55;
  cursor: wait;
}

.nsfw-mode-setting__toggle input:disabled + .nsfw-mode-setting__switch {
  opacity: 0.55;
}

.nsfw-mode-setting__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nsfw-mode-setting__status {
  flex: 0 0 auto;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1;
}

.nsfw-mode-setting__status.is-ready {
  color: #b8c2d6;
}

.nsfw-mode-setting__status.is-active {
  color: #ff9aa5;
}
</style>
