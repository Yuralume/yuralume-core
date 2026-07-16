<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DeliveryMode, MessagingAccount } from '@/types/messaging'

const props = withDefaults(defineProps<{
  account: MessagingAccount
  telegramDeliveryMode: DeliveryMode
  effectivePublicBaseUrl?: string
  bindingCount?: number
}>(), {
  effectivePublicBaseUrl: '',
  bindingCount: 0,
})

const { t } = useI18n()

const complete = computed(() => props.bindingCount > 0)
const hasPublicBaseUrl = computed(() => props.effectivePublicBaseUrl.trim().length > 0)

const message = computed(() => {
  if (complete.value) {
    return t('channelAccountNextStep.complete.message')
  }
  if (props.account.platform === 'telegram') {
    if (props.telegramDeliveryMode === 'polling') {
      return t('channelAccountNextStep.pending.telegramPolling')
    }
    return hasPublicBaseUrl.value
      ? t('channelAccountNextStep.pending.telegramWebhookReady')
      : t('channelAccountNextStep.pending.telegramWebhookMissing')
  }
  if (props.account.platform === 'discord') {
    return t('channelAccountNextStep.pending.discordGateway')
  }
  if (props.account.platform === 'whatsapp') {
    return t('channelAccountNextStep.pending.whatsappGateway')
  }
  return hasPublicBaseUrl.value
    ? t('channelAccountNextStep.pending.lineWebhookReady')
    : t('channelAccountNextStep.pending.lineWebhookMissing')
})
</script>

<template>
  <section :class="['channel-next-step', complete ? 'complete' : 'pending']">
    <h4 class="channel-next-step__title">
      {{
        complete
          ? t('channelAccountNextStep.complete.title')
          : t('channelAccountNextStep.pending.title')
      }}
    </h4>
    <p class="channel-next-step__message">{{ message }}</p>
    <p v-if="complete" class="channel-next-step__hint">
      {{ t('channelAccountNextStep.complete.proactiveHint') }}
    </p>
  </section>
</template>

<style scoped>
.channel-next-step {
  padding: 9px 10px;
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.channel-next-step.pending {
  border: 1px solid rgba(64, 156, 255, 0.28);
  background: rgba(64, 156, 255, 0.08);
}

.channel-next-step.complete {
  border: 1px solid rgba(82, 196, 26, 0.35);
  background: rgba(82, 196, 26, 0.1);
}

.channel-next-step__title {
  margin: 0;
  font-size: 12px;
  font-weight: 650;
  color: var(--color-text);
}

.channel-next-step__message,
.channel-next-step__hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-text-secondary);
}
</style>
