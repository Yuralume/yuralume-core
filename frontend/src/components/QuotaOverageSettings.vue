<script setup lang="ts">
/**
 * "Buying past your daily limit" — the AP4 quota-overage switches.
 *
 * Hosted-only surface: the backend 404s outside cloud mode, so on self-host
 * this component renders no nodes at all and the settings pane is unchanged.
 *
 * Three things travel with every switch, because this is the one place in the
 * product where credits may be spent on work the player did not press a
 * button for:
 *   1. what exactly it authorises, in plain language;
 *   2. the current price, read live from the published list — never a number
 *      typed into this file, and never a guess when the list is unreadable;
 *   3. the daily ceiling, so "unlimited spending" is visibly not on offer.
 *
 * The LumeGram switch additionally asks for confirmation on the way *on*
 * (never on the way off): it is the only one that funds an autonomous
 * background post, which is the exact case the standing promise —
 * "your credits never shrink while you sleep" — carves out.
 *
 * Because consent is to a *price*, a switch with no price to show cannot be
 * turned on at all — the backend refuses the write, so the control is disabled
 * with the reason spelled out rather than left to spring back. Turning one off
 * is never blocked, whatever the tier and the price list are doing.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import {
  ACTION_CHAT_IMAGE_OVERAGE,
  ACTION_FEED_POST_OVERAGE,
  useActionPricing,
} from '@/composables/useActionPricing'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import {
  useOverageSettings,
  type OverageItem,
} from '@/composables/useOverageSettings'
import { usePlayerCopy } from '@/composables/usePlayerCopy'
import { OverageConsentError } from '@/utils/api/overageSettings'
import { creditAmountText } from '@/utils/creditsFormat'

const { t } = useI18n()
const { pt } = usePlayerCopy()
const { cloudMode, currentUser } = useAuth()
const pricing = useActionPricing()
const confirmDialog = useConfirmDialog()
const overage = useOverageSettings()

const savingItem = ref<OverageItem | null>(null)
const feedback = ref<string | null>(null)

const settings = overage.settings
// Only render once the real switch states are in hand: a section drawn from
// defaults would tell the player their consent is off when it may be on.
const visible = computed(() => cloudMode.value && overage.ready.value)

const dailyLimitText = computed(() => {
  const limit = settings.value?.daily_limit ?? 0
  return limit > 0 ? pt('credits.overage.dailyLimit', { count: limit }) : null
})

/**
 * What one purchase costs this player, or `null`.
 *
 * The settings payload wins over the shared price list: it is resolved against
 * the tier the player is actually on, which the public list alone cannot
 * identify. The list is the fallback for the moment before the payload's
 * prices exist (an older backend), and `null` means "cannot be stated" — which
 * is exactly when the switch may not be armed.
 */
function priceCrOf(item: OverageItem): number | null {
  const quoted = item === 'chat_image'
    ? settings.value?.chat_image_price_cr
    : settings.value?.feed_post_price_cr
  if (typeof quoted === 'number') return quoted
  const listed = pricing.priceOf(
    item === 'chat_image' ? ACTION_CHAT_IMAGE_OVERAGE : ACTION_FEED_POST_OVERAGE,
  )
  return listed ? listed.price_cr : null
}

function priceTextFor(item: OverageItem): string {
  const priceCr = priceCrOf(item)
  if (priceCr === null) return pt('credits.overage.priceUnknown')
  return pt('credits.overage.price', {
    amount: creditAmountText(t, priceCr),
  })
}

const chatImagePriceText = computed(() => priceTextFor('chat_image'))
const feedPostPriceText = computed(() => priceTextFor('feed_post'))

/** The tier half of the gate: no plan, no ceiling, nothing to agree to. */
const tierSellsOverage = computed(() => Boolean(
  settings.value?.tier_overage_enabled && (settings.value?.daily_limit ?? 0) > 0,
))

function canEnable(item: OverageItem): boolean {
  return tierSellsOverage.value && priceCrOf(item) !== null
}

/** Why this switch cannot be armed, in the player's words. `null` = it can. */
function blockedReason(item: OverageItem): string | null {
  if (canEnable(item)) return null
  return tierSellsOverage.value
    ? pt('credits.overage.blockedNoPrice')
    : pt('credits.overage.blockedTierClosed')
}

const chatImageBlocked = computed(() => blockedReason('chat_image'))
const feedPostBlocked = computed(() => blockedReason('feed_post'))

/**
 * A control the player cannot move is disabled, not silently inert — but only
 * in the direction that is actually shut. A switch already on must stay
 * clickable however closed the plan is: revoking consent is never blocked.
 */
function toggleDisabled(item: OverageItem, on: boolean): boolean {
  return savingItem.value !== null || (!on && !canEnable(item))
}

async function confirmBackgroundSpend(): Promise<boolean> {
  return confirmDialog({
    title: pt('credits.overage.confirmTitle'),
    content: pt('credits.overage.confirmBody', {
      price: feedPostPriceText.value,
      limit: dailyLimitText.value ?? '',
    }),
    okText: pt('credits.overage.confirmAccept'),
  })
}

async function onToggle(item: OverageItem, event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const next = input.checked
  if (!settings.value || savingItem.value) {
    input.checked = !next
    return
  }
  // Arming needs a price to agree to; the backend would refuse the write
  // anyway, so say so here instead of sending it.
  if (next && !canEnable(item)) {
    input.checked = false
    feedback.value = blockedReason(item)
    return
  }
  // Consent for background spending is asked for once, on the way on. A
  // player switching it *off* is never made to confirm anything.
  if (item === 'feed_post' && next && !(await confirmBackgroundSpend())) {
    input.checked = false
    return
  }
  savingItem.value = item
  feedback.value = null
  try {
    await overage.setSwitch(item, next)
  } catch (error) {
    // Restore the control by hand: the stored value did not change, so a
    // re-render alone would leave the box showing the flip that failed.
    input.checked = !next
    feedback.value = rejectionText(error) ?? pt('credits.overage.saveFailed')
  } finally {
    savingItem.value = null
  }
}

/** A deliberate refusal explains itself; anything else is a plain failure. */
function rejectionText(error: unknown): string | null {
  if (!(error instanceof OverageConsentError)) return null
  if (error.reason === 'tier_closed') {
    return pt('credits.overage.blockedTierClosed')
  }
  if (error.reason === 'price_unavailable') {
    return pt('credits.overage.blockedNoPrice')
  }
  return null
}

// The singleton drops itself when the signed-in identity changes; this pulls
// the new player's switches in without waiting for a remount.
watch(() => currentUser.value?.id, () => {
  if (!cloudMode.value) return
  void overage.ensureLoaded()
})

onMounted(() => {
  if (!cloudMode.value) return
  void pricing.ensureLoaded()
  void overage.ensureLoaded()
})
</script>

<template>
  <section v-if="visible && settings" class="overage-settings">
    <h4 class="overage-settings__title">{{ pt('credits.overage.title') }}</h4>
    <p class="overage-settings__intro">{{ pt('credits.overage.intro') }}</p>
    <p
      v-if="!settings.tier_overage_enabled"
      class="overage-settings__note"
    >{{ pt('credits.overage.tierClosed') }}</p>

    <div class="overage-item">
      <label class="overage-toggle" :class="{ 'is-saving': savingItem === 'chat_image' }">
        <input
          type="checkbox"
          :checked="settings.chat_image_enabled"
          :disabled="toggleDisabled('chat_image', settings.chat_image_enabled)"
          @change="onToggle('chat_image', $event)"
        />
        <span class="overage-switch" aria-hidden="true">
          <span class="overage-knob" />
        </span>
        <span class="overage-label">{{ pt('credits.overage.chatImage.label') }}</span>
      </label>
      <p class="overage-hint">{{ pt('credits.overage.chatImage.hint') }}</p>
      <p class="overage-meta">
        <span class="overage-meta__price">{{ chatImagePriceText }}</span>
        <span v-if="dailyLimitText" class="overage-meta__limit">{{ dailyLimitText }}</span>
      </p>
      <p
        v-if="chatImageBlocked && !settings.chat_image_enabled"
        class="overage-blocked"
      >{{ chatImageBlocked }}</p>
    </div>

    <div class="overage-item">
      <label class="overage-toggle" :class="{ 'is-saving': savingItem === 'feed_post' }">
        <input
          type="checkbox"
          :checked="settings.feed_post_enabled"
          :disabled="toggleDisabled('feed_post', settings.feed_post_enabled)"
          @change="onToggle('feed_post', $event)"
        />
        <span class="overage-switch" aria-hidden="true">
          <span class="overage-knob" />
        </span>
        <span class="overage-label">{{ pt('credits.overage.feedPost.label') }}</span>
      </label>
      <p class="overage-hint">{{ pt('credits.overage.feedPost.hint') }}</p>
      <p class="overage-meta">
        <span class="overage-meta__price">{{ feedPostPriceText }}</span>
        <span v-if="dailyLimitText" class="overage-meta__limit">{{ dailyLimitText }}</span>
      </p>
      <p
        v-if="feedPostBlocked && !settings.feed_post_enabled"
        class="overage-blocked"
      >{{ feedPostBlocked }}</p>
    </div>

    <p v-if="feedback" class="overage-feedback">{{ feedback }}</p>
  </section>
</template>

<style scoped>
.overage-settings {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.overage-settings__title {
  margin: 0;
  color: var(--color-primary-light);
  font-size: var(--font-xs);
  font-weight: 700;
}

.overage-settings__intro,
.overage-settings__note,
.overage-hint,
.overage-meta,
.overage-blocked,
.overage-feedback {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.5;
}

.overage-settings__note,
.overage-blocked {
  color: #f0b96b;
}

.overage-toggle input:disabled + .overage-switch {
  opacity: 0.45;
}

.overage-toggle:has(input:disabled) {
  cursor: not-allowed;
}

.overage-feedback {
  color: #e08a7a;
}

.overage-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
}

.overage-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
}

.overage-meta__price {
  color: var(--color-text);
}

.overage-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  user-select: none;
}

.overage-toggle input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.overage-switch {
  position: relative;
  width: 32px;
  height: 18px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.08);
  flex: 0 0 auto;
  transition: background 0.15s, border-color 0.15s;
}

.overage-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  transition: transform 0.15s, background 0.15s;
}

.overage-toggle input:checked + .overage-switch {
  border-color: rgba(240, 168, 104, 0.65);
  background: rgba(240, 168, 104, 0.22);
}

.overage-toggle input:checked + .overage-switch .overage-knob {
  transform: translateX(14px);
  background: #f0a868;
}

.overage-toggle.is-saving {
  opacity: 0.55;
  cursor: wait;
}
</style>
