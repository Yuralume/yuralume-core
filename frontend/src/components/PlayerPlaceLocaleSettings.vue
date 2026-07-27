<script setup lang="ts">
/**
 * "Where I live / my timezone / my language" block of the personal
 * settings panel (plan G2 §4.2).
 *
 * Two branches that never meet:
 *
 * - **self-host** keeps the exact fields it has always had — read-only
 *   timezone + language, manual country / latitude / longitude. The markup
 *   and copy below are the pre-G2 ones, moved verbatim out of
 *   `PersonalSettingsSection.vue`; there is no behavioural change and no
 *   new endpoint is ever called.
 * - **cloud** gets the player-facing treatment the plan asks for: a city
 *   search instead of coordinates (typing a latitude is a technical
 *   artefact, not a place), an optional browser-geolocation shortcut, an
 *   editable timezone / language pair behind an explicit irreversibility
 *   warning and a 2-per-30-days guardrail, and the "you seem to be
 *   somewhere else" bar next to the field its answer would change.
 *
 * All drafts and effects live in `usePlayerLocaleSettings` so the guardrail
 * behaviour is testable without a DOM; this file is the rendering half.
 */
import { computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useLocale } from '@/composables/useLocale'
import { usePlayerLocaleSettings } from '@/composables/usePlayerLocaleSettings'
import { useTimezone } from '@/composables/useTimezone'
import { formatDateTime } from '@/i18n/formatters'
import type { OperatorProfile } from '@/types/operator'
import { UiButton, UiCombobox, UiSelect } from '@/components/ui'
import CitySearchField from './CitySearchField.vue'
import LocationHintBanner from './LocationHintBanner.vue'

const props = defineProps<{
  profile: OperatorProfile | null
  disabled?: boolean
}>()

const { t } = useI18n()
const confirmDialog = useConfirmDialog()
const { locale, supported } = useLocale()
const { timezoneOptions } = useTimezone()
const { cloudMode, currentUser, locationHint } = useAuth()

const settings = usePlayerLocaleSettings({
  translate: (key, params) => (params ? t(key, params) : t(key)),
  confirm: (options) => confirmDialog(options),
  formatRetryAt: (iso) => formatDateTime(
    iso,
    locale.value,
    currentUser.value?.timezone_id ?? 'UTC',
  ),
})

const {
  countryCodeDraft,
  latitudeDraft,
  longitudeDraft,
  locationLabelDraft,
  locationSaving,
  locationFeedback,
  timezoneDraft,
  languageDraft,
  localeSaving,
  localeFeedback,
  hintBusy,
  localeLimitHint,
} = settings

const busy = computed(() => Boolean(props.disabled) || locationSaving.value)

const currentUserLanguageLabel = computed(() => {
  const language = currentUser.value?.primary_language
  if (!language) return null
  return supported.value.find((item) => item.code === language)?.label ?? language
})

const languageOptions = computed(() => supported.value.map((item) => ({
  value: item.code,
  label: item.label,
})))
const timezoneValues = computed(() => timezoneOptions.value.map((opt) => opt.value))

const currentPlaceLabel = computed(() => {
  const label = props.profile?.location_label?.trim()
  if (label) return label
  return props.profile?.country_code?.trim() || null
})

watch(
  () => props.profile,
  (profile) => settings.seedFromProfile(profile),
  { immediate: true },
)
</script>

<template>
  <!-- Hosted: player-facing place + guarded locale editing. -->
  <template v-if="cloudMode">
    <LocationHintBanner
      v-if="locationHint"
      :hint="locationHint"
      :busy="hintBusy || locationSaving"
      @accept="settings.acceptHint"
      @dismiss="settings.dismissHint"
    />
    <div class="locale-field">
      <UiSelect
        v-model="languageDraft"
        select-id="operator-primary-language"
        :label="t('playerLocale.change.languageLabel')"
        :options="languageOptions"
        :disabled="localeSaving || busy"
      />
      <label class="field-label" for="operator-timezone">
        {{ t('playerLocale.change.timezoneLabel') }}
      </label>
      <UiCombobox
        v-model="timezoneDraft"
        input-id="operator-timezone"
        :options="timezoneValues"
        :allow-custom="false"
        :clearable="false"
        :disabled="localeSaving || busy"
      />
      <p class="locale-field__hint">{{ localeLimitHint }}</p>
      <div class="locale-actions">
        <UiButton
          variant="primary"
          size="sm"
          :loading="localeSaving"
          :disabled="busy"
          @click="settings.saveLocale"
        >
          {{ localeSaving ? t('playerLocale.change.saving') : t('playerLocale.change.save') }}
        </UiButton>
      </div>
      <p v-if="localeFeedback" class="locale-feedback">{{ localeFeedback }}</p>
    </div>
    <div class="location-field">
      <CitySearchField
        v-model="locationLabelDraft"
        input-id="operator-city-search"
        :label="t('playerLocale.city.label')"
        :disabled="locationSaving || busy"
        @select="settings.onPlaceSelected"
        @deselect="settings.onPlaceCleared"
      />
      <p class="location-hint">{{ t('playerLocale.city.hint') }}</p>
      <p class="location-hint">
        <span v-if="currentPlaceLabel">
          {{ t('playerLocale.location.current', { place: currentPlaceLabel }) }}
        </span>
        <span v-else>{{ t('playerLocale.location.empty') }}</span>
      </p>
      <div class="location-actions">
        <UiButton
          variant="primary"
          size="sm"
          :loading="locationSaving"
          :disabled="busy"
          @click="settings.saveLocation"
        >
          {{ locationSaving ? t('playerSidebar.location.saving') : t('playerSidebar.location.save') }}
        </UiButton>
        <UiButton
          variant="secondary"
          size="sm"
          :disabled="locationSaving || busy"
          @click="settings.useCurrentLocation"
        >
          {{ t('playerLocale.location.useCurrent') }}
        </UiButton>
        <UiButton
          variant="ghost"
          size="sm"
          :disabled="locationSaving || busy"
          @click="settings.clearLocation"
        >
          {{ t('playerSidebar.location.clear') }}
        </UiButton>
      </div>
      <p v-if="locationFeedback" class="location-feedback">
        {{ locationFeedback }}
      </p>
    </div>
  </template>

  <!-- Self-host: unchanged from before G2 — read-only locale, manual
       coordinates, no city search and no new endpoints. -->
  <template v-else>
    <div class="identity-section__row">
      <span>{{ t('locale.primaryLanguage.label') }}</span>
      <strong>{{ currentUserLanguageLabel }}</strong>
    </div>
    <div class="identity-section__row">
      <span>{{ t('locale.timezone.label') }}</span>
      <strong>{{ currentUser?.timezone_id }}</strong>
    </div>
    <p class="identity-section__hint">
      {{ t('locale.timezone.readonlyExplain') }}
    </p>
    <div class="location-field">
      <label class="field-label" for="operator-location-label">
        {{ t('playerSidebar.location.label') }}
      </label>
      <input
        id="operator-location-label"
        v-model="locationLabelDraft"
        type="text"
        class="field-input"
        :placeholder="t('locale.location.labelPlaceholder')"
        :disabled="locationSaving || disabled"
      />
      <div class="location-grid">
        <input
          v-model="countryCodeDraft"
          type="text"
          class="field-input"
          maxlength="2"
          :placeholder="t('locale.location.countryPlaceholder')"
          :disabled="locationSaving || disabled"
        />
        <input
          v-model="latitudeDraft"
          type="number"
          step="0.000001"
          class="field-input"
          :placeholder="t('locale.location.latitudePlaceholder')"
          :disabled="locationSaving || disabled"
        />
        <input
          v-model="longitudeDraft"
          type="number"
          step="0.000001"
          class="field-input"
          :placeholder="t('locale.location.longitudePlaceholder')"
          :disabled="locationSaving || disabled"
        />
      </div>
      <div class="location-actions">
        <UiButton
          variant="primary"
          size="sm"
          :loading="locationSaving"
          :disabled="disabled"
          @click="settings.saveLocation"
        >
          {{ locationSaving ? t('playerSidebar.location.saving') : t('playerSidebar.location.save') }}
        </UiButton>
        <UiButton
          variant="ghost"
          size="sm"
          :disabled="locationSaving || disabled"
          @click="settings.clearLocation"
        >
          {{ t('playerSidebar.location.clear') }}
        </UiButton>
      </div>
      <p class="location-hint">{{ t('playerSidebar.location.hint') }}</p>
      <p v-if="locationFeedback" class="location-feedback">
        {{ locationFeedback }}
      </p>
    </div>
  </template>
</template>

<style scoped>
.identity-section__row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.identity-section__row strong {
  color: var(--color-text);
  font-weight: 600;
}
.identity-section__hint {
  margin: 2px 0 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.5;
}

.location-field,
.locale-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 8px;
  border-top: 1px dashed var(--color-border);
}

.location-grid {
  display: grid;
  grid-template-columns: 0.8fr 1fr 1fr;
  gap: 8px;
}

.location-actions,
.locale-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.location-hint,
.location-feedback,
.locale-field__hint,
.locale-feedback {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.location-feedback,
.locale-feedback {
  color: #7dc49a;
}
</style>
