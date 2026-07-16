<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import {
  getNotificationPreferences,
  updateNotificationPreferences,
  type NotificationPreferences,
} from '@/utils/api/push'
import {
  disableWebPushSubscription,
  enableWebPushSubscription,
  getCurrentPushSubscription,
  PushSubscriptionIncompleteError,
  resolvePushSupportState,
  type PushSupportState,
} from '@/utils/pushNotifications'

const { t } = useI18n()

const rootEl = ref<HTMLElement | null>(null)
const flashing = ref(false)
const enabled = ref(false)
const saving = ref(false)
const supportState = ref<PushSupportState>('unsupported')
let loadPromise: Promise<void> | null = null
let flashTimer: ReturnType<typeof setTimeout> | null = null
const preferences = ref<NotificationPreferences>({
  proactive_enabled: true,
  feed_reply_enabled: true,
  feed_post_enabled: false,
  studio_enabled: true,
  content_preview_enabled: true,
  suppress_when_external_delivered: true,
})

const supportMessage = computed(() => {
  if (supportState.value === 'unsupported') {
    return t('playerSidebar.webNotifications.unsupported')
  }
  if (supportState.value === 'unconfigured') {
    return t('playerSidebar.webNotifications.unconfigured')
  }
  if (supportState.value === 'denied') {
    return t('playerSidebar.webNotifications.denied')
  }
  return enabled.value
    ? t('playerSidebar.webNotifications.enabledHint')
    : t('playerSidebar.webNotifications.disabledHint')
})

async function load() {
  try {
    const [state, pref, subscription] = await Promise.all([
      resolvePushSupportState(),
      getNotificationPreferences(),
      getCurrentPushSubscription(),
    ])
    supportState.value = state
    preferences.value = pref
    enabled.value = subscription !== null
  } catch {
    supportState.value = 'unsupported'
  }
}

async function onMasterToggle(event: Event) {
  const target = event.target as HTMLInputElement
  const next = target.checked
  const previous = enabled.value
  enabled.value = next
  saving.value = true
  try {
    if (next) {
      const state = await enableWebPushSubscription()
      supportState.value = state
      enabled.value = state === 'supported'
      if (state !== 'supported') {
        notification.warning({
          message: t('playerSidebar.webNotifications.enableFailedTitle'),
          description: supportMessage.value,
          duration: 4,
        })
      }
    } else {
      await disableWebPushSubscription()
      enabled.value = false
    }
  } catch (error) {
    enabled.value = previous
    notification.error({
      message: t('playerSidebar.webNotifications.settingsFailedTitle'),
      description: error instanceof PushSubscriptionIncompleteError
        ? t('playerSidebar.webNotifications.subscriptionIncomplete')
        : error instanceof Error
          ? error.message
          : t('playerSidebar.webNotifications.settingsFailedDescDefault'),
      duration: 3,
    })
  } finally {
    saving.value = false
  }
}

async function updatePreference(key: keyof NotificationPreferences, value: boolean) {
  const previous = { ...preferences.value }
  preferences.value = { ...preferences.value, [key]: value }
  saving.value = true
  try {
    preferences.value = await updateNotificationPreferences(preferences.value)
  } catch (error) {
    preferences.value = previous
    notification.error({
      message: t('playerSidebar.webNotifications.settingsFailedTitle'),
      description: error instanceof Error
        ? error.message
        : t('playerSidebar.webNotifications.settingsFailedDescDefault'),
      duration: 3,
    })
  } finally {
    saving.value = false
  }
}

function onPreferenceToggle(key: keyof NotificationPreferences, event: Event) {
  const target = event.target as HTMLInputElement
  void updatePreference(key, target.checked)
}

function ensureLoaded(): Promise<void> {
  if (!loadPromise) loadPromise = load()
  return loadPromise
}

// 由 PostCreateChannelGuide 略過綁定通道時觸發：捲到設定並閃一下，提醒可開啟系統推播。
// 等狀態載入完成再判斷；已訂閱就不再打擾，只滑到位置。
async function flashReminder() {
  await ensureLoaded()
  await nextTick()
  rootEl.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  if (enabled.value) return
  if (flashTimer) clearTimeout(flashTimer)
  flashing.value = true
  flashTimer = setTimeout(() => {
    flashing.value = false
    flashTimer = null
  }, 1700)
}

onMounted(() => {
  void ensureLoaded()
})

onUnmounted(() => {
  if (flashTimer) clearTimeout(flashTimer)
})

defineExpose({ flashReminder })
</script>

<template>
  <div ref="rootEl" class="web-notification-setting" :class="{ 'is-flashing': flashing }">
    <label class="web-notification-toggle" :class="{ 'is-saving': saving }">
      <input
        type="checkbox"
        :checked="enabled"
        :disabled="saving || supportState === 'unsupported' || supportState === 'unconfigured'"
        @change="onMasterToggle"
      />
      <span class="web-notification-switch" aria-hidden="true">
        <span class="web-notification-knob" />
      </span>
      <span class="web-notification-label">
        {{ t('playerSidebar.webNotifications.masterLabel') }}
      </span>
    </label>
    <p class="web-notification-hint">{{ supportMessage }}</p>

    <div class="web-notification-options">
      <label class="web-notification-option">
        <input
          type="checkbox"
          :checked="preferences.proactive_enabled"
          :disabled="saving"
          @change="onPreferenceToggle('proactive_enabled', $event)"
        />
        <span>{{ t('playerSidebar.webNotifications.proactive') }}</span>
      </label>
      <label class="web-notification-option">
        <input
          type="checkbox"
          :checked="preferences.feed_reply_enabled"
          :disabled="saving"
          @change="onPreferenceToggle('feed_reply_enabled', $event)"
        />
        <span>{{ t('playerSidebar.webNotifications.feedReply') }}</span>
      </label>
      <label class="web-notification-option">
        <input
          type="checkbox"
          :checked="preferences.feed_post_enabled"
          :disabled="saving"
          @change="onPreferenceToggle('feed_post_enabled', $event)"
        />
        <span>{{ t('playerSidebar.webNotifications.feedPost') }}</span>
      </label>
      <label class="web-notification-option">
        <input
          type="checkbox"
          :checked="preferences.studio_enabled"
          :disabled="saving"
          @change="onPreferenceToggle('studio_enabled', $event)"
        />
        <span>{{ t('playerSidebar.webNotifications.studio') }}</span>
      </label>
      <label class="web-notification-option">
        <input
          type="checkbox"
          :checked="preferences.content_preview_enabled"
          :disabled="saving"
          @change="onPreferenceToggle('content_preview_enabled', $event)"
        />
        <span>{{ t('playerSidebar.webNotifications.contentPreview') }}</span>
      </label>
      <label class="web-notification-option">
        <input
          type="checkbox"
          :checked="preferences.suppress_when_external_delivered"
          :disabled="saving"
          @change="onPreferenceToggle('suppress_when_external_delivered', $event)"
        />
        <span>{{ t('playerSidebar.webNotifications.suppressExternal') }}</span>
      </label>
    </div>
  </div>
</template>

<style scoped>
.web-notification-setting {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-radius: 8px;
}

/* 略過綁定通道後的提醒：用 outline 做脈衝高亮，不撐版面、不位移。 */
.web-notification-setting.is-flashing {
  animation: web-notification-flash 0.85s ease-in-out 2;
}

@keyframes web-notification-flash {
  0%, 100% {
    outline: 2px solid rgba(106, 169, 240, 0);
    outline-offset: 4px;
    background: transparent;
  }
  50% {
    outline: 2px solid rgba(106, 169, 240, 0.6);
    outline-offset: 4px;
    background: rgba(106, 169, 240, 0.1);
  }
}

.web-notification-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
}

.web-notification-toggle input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.web-notification-switch {
  position: relative;
  width: 32px;
  height: 18px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.08);
  flex: 0 0 auto;
  transition: background 0.15s, border-color 0.15s;
}

.web-notification-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  transition: transform 0.15s, background 0.15s;
}

.web-notification-toggle input:checked + .web-notification-switch {
  border-color: rgba(106, 169, 240, 0.65);
  background: rgba(106, 169, 240, 0.22);
}

.web-notification-toggle input:checked + .web-notification-switch .web-notification-knob {
  transform: translateX(14px);
  background: #6aa9f0;
}

.web-notification-toggle.is-saving {
  opacity: 0.55;
  cursor: wait;
}

.web-notification-hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.web-notification-options {
  display: grid;
  gap: 6px;
  padding-left: 2px;
}

.web-notification-option {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 22px;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.3;
  cursor: pointer;
}

.web-notification-option input {
  accent-color: #6aa9f0;
}
</style>
