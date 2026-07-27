<script setup lang="ts">
/**
 * Blocking first-login confirmation of place / timezone / language
 * (plan G2 §4.2 — the main line of defence).
 *
 * A hosted player's locale is guessed from GeoIP, and the guess is
 * sometimes simply wrong (VPN, travelling, a shared exit node). Correcting
 * it *here* is free: nothing has been generated yet, so there is no history
 * to reinterpret and the 30-day guardrail is not spent. Everything after
 * this point is a guarded change, which is exactly why this screen blocks.
 *
 * Rendered as a root-level fixed overlay rather than a route or a
 * `<Teleport>`: no redirect bookkeeping, no "where do I send them back to"
 * problem, and the whole app stays mounted underneath. `App.vue` withholds
 * it on `meta.public` routes (login / setup / OAuth callbacks) — blocking
 * there would deadlock the very flow that produces the identity we need.
 *
 * Self-host renders nothing at all: `needsLocaleConfirmation` is gated on
 * cloud mode and the backend never sets the flag.
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import { useFirstLoginLocaleDraft } from '@/composables/useFirstLoginLocaleDraft'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import {
  localeConfirmFailureCopy,
  shouldBlockForLocaleConfirmation,
} from '@/utils/localeLifecycle'
import { confirmLocale } from '@/utils/api/playerLocale'
import { UiButton, UiCombobox, UiSelect } from '@/components/ui'
import CitySearchField from './CitySearchField.vue'

const props = withDefaults(defineProps<{ publicRoute?: boolean }>(), {
  publicRoute: false,
})

const { t } = useI18n()
const { cloudMode, currentUser, needsLocaleConfirmation, refreshMe } = useAuth()
const { supported } = useLocale()
const { timezoneOptions } = useTimezone()

const visible = computed(() => shouldBlockForLocaleConfirmation({
  cloudMode: cloudMode.value,
  authenticated: Boolean(currentUser.value),
  needsConfirmation: needsLocaleConfirmation.value,
  publicRoute: props.publicRoute,
}))

// Drafts + payload assembly live in the composable; this file is the
// rendering and submitting half.
const draft = useFirstLoginLocaleDraft()
const { placeQuery, timezoneId, primaryLanguage } = draft
const submitting = ref(false)
const failure = ref<string | null>(null)

const languageOptions = computed(() => supported.value.map((item) => ({
  value: item.code,
  label: item.label,
})))
const timezoneValues = computed(() => timezoneOptions.value.map((opt) => opt.value))

watch(
  [visible, () => currentUser.value?.id],
  ([isVisible]) => {
    if (!isVisible) return
    draft.seed()
    failure.value = null
  },
  { immediate: true },
)

async function submit(): Promise<void> {
  if (submitting.value) return
  submitting.value = true
  failure.value = null
  try {
    await confirmLocale(draft.confirmPayload())
    // The gate closes because the server says so, never because we assumed
    // the write landed.
    await refreshMe()
  } catch (err) {
    // A structured refusal has its own next step ("change it in settings",
    // "try again in a moment"); only everything else is a plain failure.
    const copy = localeConfirmFailureCopy(err)
    if (copy) {
      failure.value = t(copy.key, copy.params ?? {})
      // Already-confirmed means this gate is running on a stale flag: the
      // server has moved on, so re-read rather than hold the player behind
      // a screen that can no longer succeed.
      if (copy.staleGate) await refreshMe()
    } else {
      failure.value = err instanceof Error
        ? t('common.errorWithDetail', {
          message: t('playerLocale.confirm.failed'),
          detail: err.message,
        })
        : t('playerLocale.confirm.failed')
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div v-if="visible" class="locale-gate" role="dialog" aria-modal="true">
    <div class="locale-gate__card">
      <h2 class="locale-gate__title">{{ t('playerLocale.confirm.title') }}</h2>
      <p class="locale-gate__intro">{{ t('playerLocale.confirm.intro') }}</p>

      <CitySearchField
        v-model="placeQuery"
        input-id="locale-gate-city"
        :label="t('playerLocale.confirm.placeLabel')"
        :disabled="submitting"
        @select="draft.onPlaceSelected"
        @deselect="draft.onPlaceCleared"
      />
      <p class="locale-gate__hint">{{ t('playerLocale.city.hint') }}</p>

      <div class="locale-gate__field">
        <label class="field-label" for="locale-gate-timezone">
          {{ t('playerLocale.confirm.timezoneLabel') }}
        </label>
        <UiCombobox
          v-model="timezoneId"
          input-id="locale-gate-timezone"
          :options="timezoneValues"
          :allow-custom="false"
          :clearable="false"
          :disabled="submitting"
        />
      </div>

      <UiSelect
        v-model="primaryLanguage"
        select-id="locale-gate-language"
        :label="t('playerLocale.confirm.languageLabel')"
        :options="languageOptions"
        :disabled="submitting"
      />

      <div class="locale-gate__actions">
        <UiButton
          variant="primary"
          size="md"
          block
          :loading="submitting"
          @click="submit"
        >
          {{ submitting ? t('playerLocale.confirm.submitting') : t('playerLocale.confirm.submit') }}
        </UiButton>
      </div>
      <p v-if="failure" class="locale-gate__error">{{ failure }}</p>
    </div>
  </div>
</template>

<style scoped>
.locale-gate {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4, 16px);
  background: rgba(8, 10, 16, 0.82);
  backdrop-filter: blur(3px);
  overflow-y: auto;
}

.locale-gate__card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: min(420px, 100%);
  padding: 20px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-surface);
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.45);
}

.locale-gate__title {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text);
}

.locale-gate__intro,
.locale-gate__hint,
.locale-gate__error {
  margin: 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-text-secondary);
}

.locale-gate__error {
  color: #f4a3a3;
}

.locale-gate__field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.locale-gate__actions {
  margin-top: 4px;
}
</style>
