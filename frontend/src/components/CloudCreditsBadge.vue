<script setup lang="ts">
/**
 * Hosted account corner of the player sidebar — cloud mode only.
 *
 * Shows the credit ("螢火") *total* only (owner decision, plan §8-3); the
 * split between the monthly allowance and purchased credits appears in the
 * expandable card, matching how the Portal words it. Every per-charge detail
 * stays in the Portal ledger — this badge answers exactly one question ("how
 * much is left?") and links out for the rest.
 *
 * That expanded card is also where the account-centre entry lives
 * (`PortalAccountLink`). It used to sit at the bottom of the settings tab,
 * which meant a player had to already know it existed; balance, ledger and
 * account are one subject, so one expansion now opens all three.
 *
 * The balance half renders nothing when the balance is unknown: a fabricated
 * `0` would read as "you are out of credits" and is worse than no badge
 * (plan §1-4). The account entry does *not* inherit that condition — losing
 * the only way back to the Portal because a balance read degraded would be a
 * worse failure than showing no number — so it stands alone in this slot in
 * that case. On self-host the whole component stays silent.
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import { useCloudCredits } from '@/composables/useCloudCredits'
import {
  creditAmountText,
  portalCreditsUrl,
  portalHomeUrl,
} from '@/utils/creditsFormat'
import PortalAccountLink from './PortalAccountLink.vue'

const { t } = useI18n()
const { cloudMode, portalUrl } = useAuth()
const credits = useCloudCredits()

const expanded = ref(false)

/** Mirrors `PortalAccountLink`'s own guard, so the fallback branch never
 *  renders an empty container on a cloud deployment without a Portal. */
const hasPortal = computed(() => portalHomeUrl(portalUrl.value) !== null)
const visible = computed(
  () => cloudMode.value && (credits.hasBalance.value || hasPortal.value),
)
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
    <template v-if="credits.hasBalance.value">
      <button
        type="button"
        class="credits-badge__trigger"
        :class="{
          'is-low': credits.lowBalance.value,
          'is-stale': credits.stale.value,
          'is-open': expanded,
        }"
        :title="tooltip"
        :aria-label="t('credits.badge.ariaLabel')"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <span class="credits-badge__icon" aria-hidden="true">✦</span>
        <span class="credits-badge__amount">{{ totalText }}</span>
        <span class="credits-badge__caret" aria-hidden="true">▾</span>
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

        <!-- Both links come from the same `portal_url`, so one guard covers
             the footer: without a Portal there is nothing to link to. -->
        <div v-if="topUpHref" class="credits-card__actions">
          <a
            class="credits-card__link"
            :href="topUpHref"
          >{{ t('credits.badge.detailLink') }}</a>
          <PortalAccountLink variant="inline" />
        </div>
      </div>
    </template>

    <!-- Balance unknown (degraded read, or no ledger at all): no number, but
         the way back to the account centre must not disappear with it. -->
    <PortalAccountLink v-else />
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

/* Open state reads as "this is the panel you just opened", so the card below
   looks attached to the pill rather than floating under it. */
.credits-badge__trigger.is-open {
  background: rgba(255, 255, 255, 0.08);
  border-color: var(--color-primary-light);
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

/* Discoverability: the account centre now lives behind this expansion, so the
   pill has to look expandable instead of like a read-only chip. */
.credits-badge__caret {
  margin-left: auto;
  flex: 0 0 auto;
  font-size: 9px;
  line-height: 1;
  color: var(--color-text-secondary);
  transition: transform 0.2s;
}

.credits-badge__trigger.is-open .credits-badge__caret {
  transform: rotate(180deg);
}

.credits-card {
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
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

/* Footer group: "where do I go to deal with my account?" — the ledger and the
   account centre, one rule away from the numbers above them. */
.credits-card__actions {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--color-border);
}

.credits-card__link {
  font-size: var(--font-xs);
  color: var(--color-primary);
  text-decoration: none;
}

.credits-card__link:hover {
  text-decoration: underline;
}
</style>
