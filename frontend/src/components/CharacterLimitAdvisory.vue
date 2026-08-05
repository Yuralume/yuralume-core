<script setup lang="ts">
/**
 * "You probably cannot create a character right now" — advisory only.
 *
 * Every path that creates a character (the sidebar button, the create modal,
 * importing a character card) hits the same hosted cap, so all of them show
 * the same sentence from one place instead of three near-identical hints
 * drifting apart.
 *
 * Red lines this component encodes:
 *   - it never disables anything. It is a sentence, next to a control that
 *     stays pressable — the server owns the actual refusal;
 *   - it renders *nothing* unless a loaded hosted snapshot positively says
 *     the ceiling is reached, so self-host output is unchanged to the byte.
 */
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'

import { useRuntimeLimits } from '@/composables/useRuntimeLimits'

withDefaults(defineProps<{
  /**
   * `inline` sits under a button; `banner` is the boxed warning used at the
   * top of the create modal (matching its existing `.intake-warning`).
   */
  variant?: 'inline' | 'banner'
}>(), { variant: 'inline' })

const { t } = useI18n()
const limits = useRuntimeLimits()

const message = computed<string | null>(() => {
  const blocked = limits.characterCreationBlocked.value
  // Both walls at once (the demo profile's everyday state: 1 slot, 1
  // create/day) must NOT get the slots-only sentence — its "say goodbye to
  // make room" promise is false while the daily ledger is spent, and a
  // deletion here is irreversible.
  if (blocked === 'slots_and_daily') {
    return t('characterCreate.limits.slotsFullToday', {
      limit: limits.characterSlots.value?.limit ?? 0,
    })
  }
  if (blocked === 'slots_full') {
    return t('characterCreate.limits.slotsFull', {
      limit: limits.characterSlots.value?.limit ?? 0,
    })
  }
  if (blocked === 'daily_exhausted') {
    return t('characterCreate.limits.dailyExhausted', {
      limit: limits.characterDailyCreates.value?.limit ?? 0,
    })
  }
  return null
})

onMounted(() => {
  void limits.ensureLoaded()
})
</script>

<template>
  <p
    v-if="message"
    :class="['limit-advisory', `limit-advisory--${variant}`]"
    role="status"
  >{{ message }}</p>
</template>

<style scoped>
.limit-advisory {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.limit-advisory--banner {
  padding: 8px 10px;
  border: 1px solid rgba(var(--color-spark-rgb), 0.26);
  border-radius: 6px;
  background: rgba(var(--color-spark-rgb), 0.07);
  font-size: 12px;
  line-height: 1.5;
}
</style>
