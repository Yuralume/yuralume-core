<script setup lang="ts">
import axios from 'axios'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'
import type { DeliveryMode, MessagingSettingsResponse } from '@/types/messaging'
import {
  getMessagingSettings,
  updateMessagingSettings,
} from '@/utils/api/messaging'

const { t } = useI18n()

const publicBaseUrl = ref('')
const effectivePublicBaseUrl = ref('')
const source = ref<MessagingSettingsResponse['source']>('empty')
const telegramDeliveryMode = ref<DeliveryMode>('polling')
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref<string | null>(null)
const feedback = ref<string | null>(null)

const sourceLabel = computed(() => {
  if (source.value === 'preference') {
    return t('channelBindingsPanel.publicBaseUrl.source.preference')
  }
  if (source.value === 'env') {
    return t('channelBindingsPanel.publicBaseUrl.source.env')
  }
  return t('channelBindingsPanel.publicBaseUrl.source.empty')
})

onMounted(() => {
  void loadSettings()
})

function applySettings(settings: MessagingSettingsResponse) {
  publicBaseUrl.value = settings.public_base_url
  effectivePublicBaseUrl.value = settings.effective_public_base_url
  source.value = settings.source
  telegramDeliveryMode.value = settings.telegram_delivery_mode
}

async function loadSettings() {
  loading.value = true
  errorMessage.value = null
  try {
    applySettings(await getMessagingSettings())
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.loadSettingsFailed'))
  } finally {
    loading.value = false
  }
}

async function saveSettings() {
  saving.value = true
  errorMessage.value = null
  feedback.value = null
  try {
    const trimmed = publicBaseUrl.value.trim().replace(/\/+$/, '')
    applySettings(await updateMessagingSettings({
      public_base_url: trimmed || null,
      telegram_delivery_mode: telegramDeliveryMode.value,
    }))
    feedback.value = t('channelBindingsPanel.siteTelegramMode.saved')
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.saveSettingsFailed'))
  } finally {
    saving.value = false
  }
}

function readError(err: unknown, fallback: string): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return fallback
}
</script>

<template>
  <section class="messaging-public-url">
    <label class="field-label">{{ t('channelBindingsPanel.siteTelegramMode.label') }}</label>
    <select
      v-model="telegramDeliveryMode"
      class="field-select messaging-public-url__select"
      :disabled="loading || saving"
    >
      <option value="polling">{{ t('channelBindingsPanel.deliveryMode.polling') }}</option>
      <option value="webhook">{{ t('channelBindingsPanel.deliveryMode.webhook') }}</option>
    </select>
    <p class="field-hint">{{ t('channelBindingsPanel.siteTelegramMode.hint') }}</p>

    <label class="field-label">{{ t('channelBindingsPanel.publicBaseUrl.label') }}</label>
    <div class="messaging-public-url__row">
      <input
        v-model="publicBaseUrl"
        class="field-input"
        placeholder="https://your-tunnel-host.example"
        :disabled="loading || saving"
        @keyup.enter="saveSettings()"
      />
      <UiButton
        variant="primary"
        :loading="saving"
        :disabled="loading"
        @click="saveSettings"
      >{{ t('channelBindingsPanel.actions.saveSettings') }}</UiButton>
    </div>
    <p class="field-hint">{{ t('channelBindingsPanel.publicBaseUrl.hint') }}</p>
    <p class="field-hint">
      {{ t('channelBindingsPanel.publicBaseUrl.effective', {
        url: effectivePublicBaseUrl || t('common.fallback.notSet'),
        source: sourceLabel,
      }) }}
    </p>
    <div v-if="errorMessage" class="messaging-public-url__error">{{ errorMessage }}</div>
    <div v-if="feedback" class="messaging-public-url__feedback">{{ feedback }}</div>
  </section>
</template>

<style scoped>
.messaging-public-url {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-width: 760px;
}

.messaging-public-url__row {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}

.messaging-public-url__row .field-input {
  flex: 1;
}

.messaging-public-url__select {
  max-width: 280px;
}

.messaging-public-url__error,
.messaging-public-url__feedback {
  padding: var(--space-2);
  border-radius: 6px;
  font-size: var(--font-sm);
  line-height: 1.5;
}

.messaging-public-url__error {
  color: #ff9b9d;
  background: rgba(255, 77, 79, 0.12);
  border: 1px solid rgba(255, 77, 79, 0.5);
}

.messaging-public-url__feedback {
  color: #8bdc7d;
  background: rgba(82, 196, 26, 0.12);
  border: 1px solid rgba(82, 196, 26, 0.45);
}

@media (max-width: 640px) {
  .messaging-public-url__row {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
