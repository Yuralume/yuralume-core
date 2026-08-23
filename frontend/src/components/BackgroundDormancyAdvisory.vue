<script setup lang="ts">
/**
 * "Her background life pauses when you're away" — pre-emptive, marketing
 * advisory (NF5). Not a warning about a wall the player is about to hit —
 * there is no button this blocks — it exists to say, ahead of time, why a
 * character who used to text first eventually stops: the account's tier has
 * a dormancy window, and the player never heard about it.
 *
 * Red lines this component encodes (mirrors `CharacterLimitAdvisory.vue`):
 *   - it never disables anything — there is nothing here to disable, it is
 *     one sentence and a link;
 *   - it renders *nothing* unless a loaded hosted snapshot names a real
 *     dormancy window (`background.dormancyDays`), so self-host output is
 *     unchanged to the byte, and an unloaded/degraded read says nothing too;
 *   - fail-soft throughout — `useRuntimeLimits` already collapses every
 *     unknown case to `null` for us, so this component only ever has to
 *     branch on "a day count exists" vs. "it doesn't".
 *
 * Copy shape (`NEW_PLAYER_FUNNEL_FREE_TIER_PLAN.md` §2 D5, the three-rung
 * ladder). The sentence names BOTH rungs above the player, in order, because
 * they are different things and collapsing them misprices the offer:
 *   - **binding the official LINE account = the delivery channel.** This, not
 *     background speed, is the core difference between free and 試玩: a
 *     proactive message with nowhere to be pushed is not a slower message,
 *     it is no message. True under any cadence knob setting.
 *   - **a paid plan = full pace.** Speed is the third rung, and only there.
 * Anchoring the tier difference back onto background speed alone (an earlier
 * draft of this string did) inverts D5 and sells the wrong upgrade.
 *
 * Copy red line: "完全體" (the full experience) is only ever earned on the
 * paid tier, so that word must never appear in this component — it faces the
 * free / LINE rungs. Naming LINE as the delivery channel is not the same
 * claim, and is required here.
 */
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'

import { useRuntimeLimits } from '@/composables/useRuntimeLimits'
import PortalAccountLink from './PortalAccountLink.vue'

const { t } = useI18n()
const limits = useRuntimeLimits()

const dormancyDays = computed(() => limits.backgroundDormancyDays.value)

onMounted(() => {
  void limits.ensureLoaded()
})
</script>

<template>
  <div v-if="dormancyDays !== null" class="dormancy-advisory" role="status">
    <p class="dormancy-advisory__body">
      {{ t('backgroundDormancy.advisory.body', { days: dormancyDays }) }}
    </p>
    <PortalAccountLink variant="inline" />
  </div>
</template>

<style scoped>
.dormancy-advisory {
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
}

.dormancy-advisory__body {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
  overflow-wrap: anywhere;
}
</style>
