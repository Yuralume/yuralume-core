<script setup lang="ts">
/**
 * "You seem to be somewhere else now" bar (plan G2 §4.1-1).
 *
 * A re-login from a different country produces a *hint*, never an
 * overwrite — the stored place may be a deliberate manual choice, and a
 * business trip must not silently relocate a character's world. So this is
 * a question with two answers, presented next to the place field the
 * answer would change.
 *
 * Purely presentational: the parent owns both the accept (write the place)
 * and the dismiss (clear the hint) side effects.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import type { LocationHint } from '@/types/playerLocale'
import { UiButton } from '@/components/ui'

const props = defineProps<{ hint: LocationHint; busy?: boolean }>()

defineEmits<{ accept: []; dismiss: [] }>()

const { t } = useI18n()

/** Country code is the only guaranteed field; the city label is nicer when
 * the geocoder gave us one. */
const place = computed(() => props.hint.label?.trim() || props.hint.country_code)
</script>

<template>
  <div class="location-hint-banner">
    <p class="location-hint-banner__body">
      {{ t('playerLocale.hint.body', { place }) }}
    </p>
    <div class="location-hint-banner__actions">
      <UiButton
        variant="primary"
        size="sm"
        :disabled="busy"
        @click="$emit('accept')"
      >
        {{ t('playerLocale.hint.accept') }}
      </UiButton>
      <UiButton
        variant="ghost"
        size="sm"
        :disabled="busy"
        @click="$emit('dismiss')"
      >
        {{ t('playerLocale.hint.dismiss') }}
      </UiButton>
    </div>
  </div>
</template>

<style scoped>
.location-hint-banner {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid rgba(232, 155, 133, 0.35);
  border-radius: 6px;
  background: rgba(232, 155, 133, 0.08);
}

.location-hint-banner__body {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text);
}

.location-hint-banner__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
