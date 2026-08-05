<script setup lang="ts">
/**
 * "What this button costs" — hosted price hint, cloud mode only (plan AP2/AP3).
 *
 * A fixed price is only worth having if the player sees it *before* pressing
 * the button, so this sits next to the button rather than in a pricing page
 * nobody opens. It is deliberately quiet: one muted line, no colour, no
 * layout of its own.
 *
 * Renders nothing at all when the price cannot be stated honestly — self-host,
 * a deployment that still bills by usage, an unreadable price list, or a table
 * whose tiers disagree (see `useActionPricing`). A hint that guesses is worse
 * than no hint: the number would be a promise the ledger then breaks.
 */
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import { useActionPricing } from '@/composables/useActionPricing'
import { usePlayerCopy } from '@/composables/usePlayerCopy'
import { creditAmountText } from '@/utils/creditsFormat'

const props = withDefaults(defineProps<{
  /** An `action_key` from `contracts/action-catalog.json`. */
  actionKey: string
  /** i18n key of the plain-language explanation shown on hover / focus. */
  tooltipKey?: string
  variant?: 'chip' | 'inline'
}>(), {
  tooltipKey: '',
  variant: 'inline',
})

const { t } = useI18n()
const { pt } = usePlayerCopy()
const { cloudMode } = useAuth()
const pricing = useActionPricing()

/** Metering unit → the phrase that says it in the player's language. */
const UNIT_COPY_KEYS: Readonly<Record<string, string>> = {
  per_message: 'credits.price.perMessage',
  per_image: 'credits.price.perImage',
  per_portrait: 'credits.price.perPortrait',
  per_draft: 'credits.price.perDraft',
  per_post: 'credits.price.perPost',
  per_iteration: 'credits.price.perIteration',
  per_scene: 'credits.price.perScene',
}

const price = computed(() => (
  cloudMode.value ? pricing.priceOf(props.actionKey) : null
))

const label = computed(() => {
  const current = price.value
  if (!current) return null
  const copyKey = UNIT_COPY_KEYS[current.unit] ?? 'credits.price.each'
  return pt(copyKey, { amount: creditAmountText(t, current.price_cr) })
})

const tooltip = computed(() => (
  props.tooltipKey ? pt(props.tooltipKey) : pt('credits.price.ariaLabel')
))

onMounted(() => {
  if (!cloudMode.value) return
  void pricing.ensureLoaded()
})
</script>

<template>
  <span
    v-if="label"
    class="action-price"
    :class="`action-price--${variant}`"
    :title="tooltip"
    :aria-label="pt('credits.price.ariaLabel')"
  >
    <span class="action-price__icon" aria-hidden="true">✦</span>
    <span class="action-price__text">{{ label }}</span>
  </span>
</template>

<style scoped>
.action-price {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.4;
  white-space: nowrap;
  cursor: help;
}

.action-price__icon {
  color: #f0b96b;
  font-size: 10px;
}

/* Chip: for dense rows (next to a generate button) where the hint needs an
   edge to sit against. Inline: for a quiet line under an input. */
.action-price--chip {
  padding: 2px 8px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
}
</style>
