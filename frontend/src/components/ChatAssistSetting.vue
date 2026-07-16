<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { useChatAssistPreference } from '@/composables/useChatAssistPreference'

const { t } = useI18n()
const {
  chatAssistEnabled,
  loadChatAssistPreference,
  saveChatAssistPreference,
} = useChatAssistPreference()

const saving = ref(false)

async function onToggle(event: Event) {
  const target = event.target as HTMLInputElement
  const next = target.checked
  const previous = chatAssistEnabled.value
  chatAssistEnabled.value = next
  saving.value = true
  try {
    await saveChatAssistPreference(next)
  } catch (error) {
    chatAssistEnabled.value = previous
    notification.error({
      message: t('playerSidebar.chatAssist.settingsFailedTitle'),
      description: error instanceof Error
        ? error.message
        : t('playerSidebar.chatAssist.settingsFailedDescDefault'),
      duration: 3,
    })
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadChatAssistPreference()
})
</script>

<template>
  <div class="chat-assist-setting">
    <label class="chat-assist-toggle" :class="{ 'is-saving': saving }">
      <input
        type="checkbox"
        :checked="chatAssistEnabled"
        :disabled="saving"
        @change="onToggle"
      />
      <span class="chat-assist-switch" aria-hidden="true">
        <span class="chat-assist-knob" />
      </span>
      <span class="chat-assist-label">{{ t('playerSidebar.chatAssist.label') }}</span>
    </label>
    <p class="chat-assist-hint">{{ t('playerSidebar.chatAssist.hint') }}</p>
  </div>
</template>

<style scoped>
.chat-assist-setting {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.chat-assist-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
}

.chat-assist-toggle input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.chat-assist-switch {
  position: relative;
  width: 32px;
  height: 18px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.08);
  flex: 0 0 auto;
  transition: background 0.15s, border-color 0.15s;
}

.chat-assist-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  transition: transform 0.15s, background 0.15s;
}

.chat-assist-toggle input:checked + .chat-assist-switch {
  border-color: rgba(106, 169, 240, 0.65);
  background: rgba(106, 169, 240, 0.22);
}

.chat-assist-toggle input:checked + .chat-assist-switch .chat-assist-knob {
  transform: translateX(14px);
  background: #6aa9f0;
}

.chat-assist-toggle.is-saving {
  opacity: 0.55;
  cursor: wait;
}

.chat-assist-hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}
</style>
