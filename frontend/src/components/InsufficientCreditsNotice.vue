<script setup lang="ts">
/**
 * The one "out of credits" card, shared by every player entry point that can
 * hit the hosted credit ceiling (chat, image generation, TTS).
 *
 * Two promises are load-bearing and must not be reworded per surface:
 *   1. the action did **not** run — nothing was charged, nothing half-happened;
 *   2. the character's autonomous daily life keeps going regardless.
 * Both mirror the Portal's `credits.promiseBody` wording.
 *
 * The top-up CTA only appears when the deployment advertised a Portal; with
 * no Portal (self-host, which can never reach this state anyway) the card
 * degrades to the explanation alone rather than a dead link.
 *
 * `requiredCr` turns the same card into the AP2 *pre-check* card: when a
 * fixed action price is published and the known balance cannot cover it, the
 * surface refuses before sending and states the shortfall. Both promises above
 * hold verbatim in that case — nothing ran, nothing was charged — so the card
 * gains one line and changes nothing else. Omitting the prop leaves the
 * refusal card byte-identical to what every existing caller renders.
 */
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import { refreshCloudCreditsAfterAction } from '@/composables/useCloudCredits'
import { creditAmountText, portalCreditsUrl } from '@/utils/creditsFormat'

const props = withDefaults(defineProps<{
  /** Price of the action that was refused up-front, in credits. */
  requiredCr?: number | null
}>(), {
  requiredCr: null,
})

const { t } = useI18n()
const { portalUrl } = useAuth()

const topUpHref = computed(() => portalCreditsUrl(portalUrl.value))

const requiredText = computed(() => {
  const amount = props.requiredCr
  if (typeof amount !== 'number' || !Number.isFinite(amount) || amount <= 0) {
    return null
  }
  return t('credits.insufficient.required', {
    amount: creditAmountText(t, amount),
  })
})

// The refusal is itself fresh evidence about the balance — pull the real
// number so the badge stops showing a stale, comfortable-looking figure.
onMounted(() => {
  refreshCloudCreditsAfterAction()
})
</script>

<template>
  <div class="credits-notice" role="status">
    <div class="credits-notice__title">
      <span class="credits-notice__icon" aria-hidden="true">✦</span>
      {{ t('credits.insufficient.title') }}
    </div>
    <p class="credits-notice__body">{{ t('credits.insufficient.body') }}</p>
    <p v-if="requiredText" class="credits-notice__body">{{ requiredText }}</p>
    <a
      v-if="topUpHref"
      class="credits-notice__cta"
      :href="topUpHref"
    >{{ t('credits.insufficient.cta') }}</a>
  </div>
</template>

<style scoped>
.credits-notice {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
  border: 1px solid rgba(217, 144, 63, 0.45);
  border-radius: 8px;
  background: rgba(217, 144, 63, 0.1);
}

.credits-notice__title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--font-sm);
  font-weight: 600;
  color: #f0b96b;
}

.credits-notice__icon {
  font-size: 13px;
}

.credits-notice__body {
  margin: 0;
  font-size: var(--font-xs);
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.credits-notice__cta {
  align-self: flex-start;
  margin-top: 2px;
  padding: 6px 12px;
  border: 1px solid var(--color-primary);
  border-radius: 6px;
  color: var(--color-primary-light);
  font-size: var(--font-xs);
  text-decoration: none;
}

.credits-notice__cta:hover {
  background: rgba(183, 93, 63, 0.18);
}
</style>
