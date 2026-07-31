<script setup lang="ts">
/**
 * Hosted credit ("螢火") balance badge — cloud mode only.
 *
 * Shows the *total* only (owner decision, plan §8-3); the split between the
 * monthly allowance and purchased credits appears in the expandable card,
 * matching how the Portal words it. Every per-charge detail stays in the
 * Portal ledger — this badge answers exactly one question ("how much is
 * left?") and links out for the rest.
 *
 * Renders nothing at all when the deployment is self-host, or when the
 * balance is unknown: a fabricated `0` would read as "you are out of
 * credits" and is worse than no badge (plan §1-4).
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import { useCloudCredits } from '@/composables/useCloudCredits'
import { creditAmountText, portalCreditsUrl } from '@/utils/creditsFormat'

const { t } = useI18n()
const { cloudMode, portalUrl } = useAuth()
const credits = useCloudCredits()

const expanded = ref(false)

const visible = computed(() => cloudMode.value && credits.hasBalance.value)
const topUpHref = computed(() => portalCreditsUrl(portalUrl.value))

function amountText(value: number): string {
  return creditAmountText(t, value)
}

const totalText = computed(() => amountText(credits.total.value))
const giftText = computed(() => amountText(credits.gift.value))
const purchasedText = computed(() => amountText(credits.purchased.value))

const tooltip = computed(() => {
  if (credits.lowBalance.value) return t('credits.badge.lowTooltip')
  if (credits.stale.value) return t('credits.badge.staleTooltip')
  return t('credits.badge.tooltip')
})

onMounted(() => {
  if (!cloudMode.value) return
  void credits.ensureLoaded()
})
</script>

<template>
  <div v-if="visible" class="credits-badge">
    <button
      type="button"
      class="credits-badge__trigger"
      :class="{
        'is-low': credits.lowBalance.value,
        'is-stale': credits.stale.value,
      }"
      :title="tooltip"
      :aria-label="t('credits.badge.ariaLabel')"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <span class="credits-badge__icon" aria-hidden="true">✦</span>
      <span class="credits-badge__amount">{{ totalText }}</span>
    </button>

    <div v-if="expanded" class="credits-card">
      <div class="credits-card__title">{{ t('credits.badge.cardTitle') }}</div>
      <div class="credits-card__row">
        <span class="credits-card__label">{{ t('credits.badge.gift') }}</span>
        <span class="credits-card__value">{{ giftText }}</span>
      </div>
      <div class="credits-card__row">
        <span class="credits-card__label">{{ t('credits.badge.purchased') }}</span>
        <span class="credits-card__value">{{ purchasedText }}</span>
      </div>
      <a
        v-if="topUpHref"
        class="credits-card__link"
        :href="topUpHref"
      >{{ t('credits.badge.detailLink') }}</a>
    </div>
  </div>
</template>

<style scoped>
.credits-badge {
  position: relative;
  padding: 6px 12px 0;
}

.credits-badge__trigger {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 5px 10px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  font-size: var(--font-xs);
  line-height: 1.3;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s, opacity 0.2s;
}

.credits-badge__trigger:hover {
  background: rgba(255, 255, 255, 0.08);
}

/* Low balance: warn without alarming — the character's life is unaffected. */
.credits-badge__trigger.is-low {
  border-color: #d9903f;
  color: #f0b96b;
}

/* Last-known-good value: dim so the number reads as "approximate". */
.credits-badge__trigger.is-stale {
  opacity: 0.6;
}

.credits-badge__icon {
  font-size: 12px;
  color: var(--color-primary-light);
}

.credits-badge__amount {
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.credits-card {
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.credits-card__title {
  font-size: var(--font-xs);
  font-weight: 600;
  color: var(--color-primary-light);
}

.credits-card__row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}

.credits-card__value {
  color: var(--color-text);
  white-space: nowrap;
}

.credits-card__link {
  margin-top: 2px;
  font-size: var(--font-xs);
  color: var(--color-primary);
  text-decoration: none;
}

.credits-card__link:hover {
  text-decoration: underline;
}
</style>
