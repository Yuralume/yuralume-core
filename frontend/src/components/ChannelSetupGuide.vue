<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePlayerCopy } from '@/composables/usePlayerCopy'
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
const { pt, cloudMode } = usePlayerCopy()

/**
 * Official back-office each platform sends the player to (plan U1-C-3).
 *
 * Hosted only: adding the row on self-host would change an operator
 * surface that the plan promises stays byte-identical. The labels live in
 * the catalog; only the URLs — which are not prose — are kept here.
 */
const PLATFORM_CONSOLE_URLS: Record<MessagingPlatform, string> = {
  telegram: 'https://t.me/BotFather',
  line: 'https://developers.line.biz/console/',
  discord: 'https://discord.com/developers/applications',
  whatsapp: 'https://faq.whatsapp.com/',
}

const platformLabel = computed(() => (
  props.platform === 'telegram'
    ? 'Telegram'
    : props.platform === 'discord'
      ? 'Discord'
      : props.platform === 'whatsapp'
        ? 'WhatsApp'
        : 'LINE'
))

const consoleUrl = computed(() => PLATFORM_CONSOLE_URLS[props.platform])
const consoleLinkLabel = computed(() => (
  t(`channelSetupGuide.consoleLinks.${props.platform}`)
))

const modeLabel = computed(() => (
  props.telegramDeliveryMode === 'polling'
    ? t('channelBindingsPanel.deliveryMode.polling')
    : t('channelBindingsPanel.deliveryMode.webhook')
))

/**
 * Hosted collapses the four per-platform delivery lines into one
 * "the platform handles it" sentence; the `*Cloud` siblings all carry
 * that same wording, so the key stays per-platform and self-host keeps
 * its exact text.
 */
const modeKey = computed(() => {
  if (props.platform === 'telegram') return 'channelSetupGuide.telegramMode'
  if (props.platform === 'line') return 'channelSetupGuide.lineMode'
  if (props.platform === 'whatsapp') return 'channelSetupGuide.whatsappMode'
  return 'channelSetupGuide.discordMode'
})

const modeText = computed(() => pt(modeKey.value, { mode: modeLabel.value }))

const requiresWebhook = computed(() => (
  props.platform === 'line'
  || (props.platform === 'telegram' && props.telegramDeliveryMode === 'webhook')
))

const hasPublicBaseUrl = computed(() => props.effectivePublicBaseUrl.trim().length > 0)

const steps = computed(() => {
  if (props.platform === 'telegram') {
    return [
      pt('channelSetupGuide.steps.telegramBotFather'),
      pt('channelSetupGuide.steps.telegramNewBot'),
      pt('channelSetupGuide.steps.telegramPasteToken'),
      props.telegramDeliveryMode === 'polling'
        ? pt('channelSetupGuide.steps.telegramPollingFirstMessage')
        : pt('channelSetupGuide.steps.telegramWebhookRegister'),
      pt('channelSetupGuide.steps.autoBinding'),
    ]
  }
  if (props.platform === 'discord') {
    return [
      pt('channelSetupGuide.steps.discordCreateApp'),
      pt('channelSetupGuide.steps.discordAddBot'),
      pt('channelSetupGuide.steps.discordPasteToken'),
      pt('channelSetupGuide.steps.discordInviteBot'),
      pt('channelSetupGuide.steps.discordFirstMessage'),
      pt('channelSetupGuide.steps.autoBinding'),
    ]
  }
  if (props.platform === 'whatsapp') {
    return [
      pt('channelSetupGuide.steps.whatsappStartSidecar'),
      pt('channelSetupGuide.steps.whatsappScanQr'),
      pt('channelSetupGuide.steps.whatsappPasteSession'),
      pt('channelSetupGuide.steps.whatsappFirstMessage'),
      pt('channelSetupGuide.steps.autoBinding'),
    ]
  }
  return [
    pt('channelSetupGuide.steps.lineCreateChannel'),
    pt('channelSetupGuide.steps.linePasteCredentials'),
    pt('channelSetupGuide.steps.lineWebhookRegister'),
    pt('channelSetupGuide.steps.lineSendTest'),
    pt('channelSetupGuide.steps.autoBinding'),
  ]
})

const note = computed(() => {
  // Hosted keeps only the actionable warning: the informational notes all
  // describe how the *site* is wired (polling vs webhook, Gateway,
  // sidecar), which a hosted player can neither read nor act on.
  if (requiresWebhook.value && !hasPublicBaseUrl.value) {
    return pt('channelSetupGuide.notes.webhookMissing')
  }
  if (cloudMode.value) return ''
  if (props.platform === 'telegram' && props.telegramDeliveryMode === 'polling') {
    return t('channelSetupGuide.notes.telegramPolling')
  }
  if (props.platform === 'discord') {
    return t('channelSetupGuide.notes.discordGateway')
  }
  if (props.platform === 'whatsapp') {
    return t('channelSetupGuide.notes.whatsappGateway')
  }
  if (requiresWebhook.value) {
    return t('channelSetupGuide.notes.webhookReady', {
      url: props.effectivePublicBaseUrl,
    })
  }
  return ''
})
</script>

<template>
  <section :class="['channel-setup-guide', { compact }]">
    <div class="channel-setup-guide__header">
      <span class="channel-setup-guide__platform">{{ platformLabel }}</span>
      <span class="channel-setup-guide__mode">{{ modeText }}</span>
    </div>

    <ol class="channel-setup-guide__steps">
      <li v-for="step in steps" :key="step">{{ step }}</li>
    </ol>

    <!-- Hosted only: self-host operators already know where these consoles
         are, and the plan forbids changing their rendering. -->
    <a
      v-if="cloudMode"
      class="channel-setup-guide__console-link"
      :href="consoleUrl"
      target="_blank"
      rel="noopener noreferrer"
    >{{ consoleLinkLabel }} ↗</a>

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

.channel-setup-guide__console-link {
  align-self: flex-start;
  color: var(--color-primary-light);
  font-size: 12px;
  text-decoration: none;
}

.channel-setup-guide__console-link:hover {
  text-decoration: underline;
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
