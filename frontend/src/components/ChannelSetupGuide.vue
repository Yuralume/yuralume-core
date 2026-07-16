<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DeliveryMode, MessagingPlatform } from '@/types/messaging'

const props = withDefaults(defineProps<{
  platform: MessagingPlatform
  telegramDeliveryMode: DeliveryMode
  effectivePublicBaseUrl?: string
  compact?: boolean
}>(), {
  effectivePublicBaseUrl: '',
  compact: false,
})

const { t } = useI18n()

const platformLabel = computed(() => (
  props.platform === 'telegram'
    ? 'Telegram'
    : props.platform === 'discord'
      ? 'Discord'
      : props.platform === 'whatsapp'
        ? 'WhatsApp'
        : 'LINE'
))

const modeLabel = computed(() => (
  props.telegramDeliveryMode === 'polling'
    ? t('channelBindingsPanel.deliveryMode.polling')
    : t('channelBindingsPanel.deliveryMode.webhook')
))

const requiresWebhook = computed(() => (
  props.platform === 'line'
  || (props.platform === 'telegram' && props.telegramDeliveryMode === 'webhook')
))

const hasPublicBaseUrl = computed(() => props.effectivePublicBaseUrl.trim().length > 0)

const steps = computed(() => {
  if (props.platform === 'telegram') {
    return [
      t('channelSetupGuide.steps.telegramBotFather'),
      t('channelSetupGuide.steps.telegramNewBot'),
      t('channelSetupGuide.steps.telegramPasteToken'),
      props.telegramDeliveryMode === 'polling'
        ? t('channelSetupGuide.steps.telegramPollingFirstMessage')
        : t('channelSetupGuide.steps.telegramWebhookRegister'),
      t('channelSetupGuide.steps.autoBinding'),
    ]
  }
  if (props.platform === 'discord') {
    return [
      t('channelSetupGuide.steps.discordCreateApp'),
      t('channelSetupGuide.steps.discordAddBot'),
      t('channelSetupGuide.steps.discordPasteToken'),
      t('channelSetupGuide.steps.discordInviteBot'),
      t('channelSetupGuide.steps.discordFirstMessage'),
      t('channelSetupGuide.steps.autoBinding'),
    ]
  }
  if (props.platform === 'whatsapp') {
    return [
      t('channelSetupGuide.steps.whatsappStartSidecar'),
      t('channelSetupGuide.steps.whatsappScanQr'),
      t('channelSetupGuide.steps.whatsappPasteSession'),
      t('channelSetupGuide.steps.whatsappFirstMessage'),
      t('channelSetupGuide.steps.autoBinding'),
    ]
  }
  return [
    t('channelSetupGuide.steps.lineCreateChannel'),
    t('channelSetupGuide.steps.linePasteCredentials'),
    t('channelSetupGuide.steps.lineWebhookRegister'),
    t('channelSetupGuide.steps.lineSendTest'),
    t('channelSetupGuide.steps.autoBinding'),
  ]
})

const note = computed(() => {
  if (props.platform === 'telegram' && props.telegramDeliveryMode === 'polling') {
    return t('channelSetupGuide.notes.telegramPolling')
  }
  if (props.platform === 'discord') {
    return t('channelSetupGuide.notes.discordGateway')
  }
  if (props.platform === 'whatsapp') {
    return t('channelSetupGuide.notes.whatsappGateway')
  }
  if (requiresWebhook.value && hasPublicBaseUrl.value) {
    return t('channelSetupGuide.notes.webhookReady', {
      url: props.effectivePublicBaseUrl,
    })
  }
  if (requiresWebhook.value) {
    return t('channelSetupGuide.notes.webhookMissing')
  }
  return ''
})
</script>

<template>
  <section :class="['channel-setup-guide', { compact }]">
    <div class="channel-setup-guide__header">
      <span class="channel-setup-guide__platform">{{ platformLabel }}</span>
      <span
        v-if="platform === 'telegram'"
        class="channel-setup-guide__mode"
      >
        {{ t('channelSetupGuide.telegramMode', { mode: modeLabel }) }}
      </span>
      <span v-else-if="platform === 'line'" class="channel-setup-guide__mode">
        {{ t('channelSetupGuide.lineMode') }}
      </span>
      <span v-else-if="platform === 'whatsapp'" class="channel-setup-guide__mode">
        {{ t('channelSetupGuide.whatsappMode') }}
      </span>
      <span v-else class="channel-setup-guide__mode">
        {{ t('channelSetupGuide.discordMode') }}
      </span>
    </div>

    <ol class="channel-setup-guide__steps">
      <li v-for="step in steps" :key="step">{{ step }}</li>
    </ol>

    <p
      v-if="note"
      :class="[
        'channel-setup-guide__note',
        requiresWebhook && !hasPublicBaseUrl ? 'warning' : 'info',
      ]"
    >
      {{ note }}
    </p>
  </section>
</template>

<style scoped>
.channel-setup-guide {
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 6px;
  padding: 10px 12px;
  background: rgba(255, 255, 255, 0.03);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.channel-setup-guide.compact {
  padding: 8px 10px;
}

.channel-setup-guide__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.channel-setup-guide__platform {
  font-size: 13px;
  font-weight: 650;
  color: var(--color-primary-light);
}

.channel-setup-guide__mode {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.channel-setup-guide__steps {
  margin: 0;
  padding-left: 18px;
  color: var(--color-text);
  font-size: 12px;
  line-height: 1.6;
}

.channel-setup-guide__note {
  margin: 0;
  padding: 7px 8px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.55;
}

.channel-setup-guide__note.info {
  color: #9dcaf0;
  background: rgba(64, 156, 255, 0.1);
  border: 1px solid rgba(64, 156, 255, 0.25);
}

.channel-setup-guide__note.warning {
  color: #ffd48a;
  background: rgba(250, 173, 20, 0.12);
  border: 1px solid rgba(250, 173, 20, 0.35);
}
</style>
