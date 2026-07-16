<template>
  <div class="auth-screen">
    <div class="auth-card">
      <div class="auth-brand">
        <img src="/logo-mark.png" alt="Yuralume" class="auth-logo" />
        <div class="auth-wordmark">{{ t('auth.setup.brand') }}</div>
        <h1>{{ t('auth.setup.title') }}</h1>
        <p class="kizuna">{{ t('auth.setup.tagline') }}</p>
      </div>
      <p class="hint">{{ t('auth.setup.hint') }}</p>
      <form @submit.prevent="handleSubmit" class="auth-form">
        <label class="field-label" for="setup-email">{{ t('auth.setup.emailLabel') }}</label>
        <input
          id="setup-email"
          v-model="email"
          type="email"
          autocomplete="username"
          required
          class="field-input"
          :disabled="submitting"
        />

        <label class="field-label" for="setup-password">{{ t('auth.setup.passwordLabel') }}</label>
        <input
          id="setup-password"
          v-model="password"
          type="password"
          autocomplete="new-password"
          required
          minlength="6"
          class="field-input"
          :disabled="submitting"
        />

        <label class="field-label" for="setup-confirm">{{ t('auth.setup.confirmLabel') }}</label>
        <input
          id="setup-confirm"
          v-model="confirm"
          type="password"
          autocomplete="new-password"
          required
          minlength="6"
          class="field-input"
          :disabled="submitting"
        />

        <label class="field-label" for="setup-language">{{ t('auth.setup.primaryLanguageLabel') }}</label>
        <select
          id="setup-language"
          v-model="primaryLanguage"
          class="field-select"
          :disabled="submitting"
          required
        >
          <option
            v-for="opt in supported"
            :key="opt.code"
            :value="opt.code"
          >{{ opt.label }}</option>
        </select>
        <p class="field-hint setup-language-hint">{{ t('auth.setup.primaryLanguageHint') }}</p>

        <label class="field-label" for="setup-timezone">{{ t('locale.timezone.label') }}</label>
        <select
          id="setup-timezone"
          v-model="timezoneId"
          class="field-select"
          :disabled="submitting"
          required
        >
          <option
            v-for="opt in timezoneOptions"
            :key="opt.value"
            :value="opt.value"
          >{{ opt.label }}</option>
        </select>
        <p class="field-hint setup-timezone-hint">{{ t('auth.setup.timezoneHint') }}</p>

        <label class="field-label" for="setup-location-label">{{ t('locale.location.label') }}</label>
        <input
          id="setup-location-label"
          v-model="locationLabel"
          type="text"
          class="field-input"
          :placeholder="t('locale.location.labelPlaceholder')"
          :disabled="submitting"
        />
        <div class="setup-location-grid">
          <input
            v-model="countryCode"
            type="text"
            class="field-input"
            maxlength="2"
            :placeholder="t('locale.location.countryPlaceholder')"
            :disabled="submitting"
          />
          <input
            v-model="latitude"
            type="number"
            step="0.000001"
            class="field-input"
            :placeholder="t('locale.location.latitudePlaceholder')"
            :disabled="submitting"
          />
          <input
            v-model="longitude"
            type="number"
            step="0.000001"
            class="field-input"
            :placeholder="t('locale.location.longitudePlaceholder')"
            :disabled="submitting"
          />
        </div>
        <p class="field-hint setup-location-hint">{{ t('auth.setup.locationHint') }}</p>

        <button
          type="submit"
          class="auth-submit"
          :disabled="!canSubmit || submitting"
        >
          {{ submitting ? t('auth.setup.submitting') : t('auth.setup.submit') }}
        </button>

        <p v-if="error" class="auth-error">{{ error }}</p>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { SOURCE_LOCALE, coerceLocale } from '@/i18n/localeTypes'
import { normalizeCoordinateInput } from '@/utils/coordinateInput'

const router = useRouter()
const { setup } = useAuth()
const { t } = useI18n()
const { locale, supported } = useLocale()
const { browserTimezone, timezoneOptions } = useTimezone()

const email = ref('')
const password = ref('')
const confirm = ref('')
// Default the language picker to the current UI locale (browser-
// detected) so the user only has to override when they want something
// different. coerceLocale defends against a stale or unsupported value.
const primaryLanguage = ref<string>(coerceLocale(String(locale.value)) || SOURCE_LOCALE)
const timezoneId = ref<string>(browserTimezone())
const countryCode = ref('')
// Vue auto-casts v-model on <input type="number"> to a JS number even
// without the .number modifier, so these can hold a number after manual
// entry despite being seeded with ''. See normalizeCoordinateInput.
const latitude = ref<string | number>('')
const longitude = ref<string | number>('')
const locationLabel = ref('')
const submitting = ref(false)
const error = ref('')

const canSubmit = computed(
  () =>
    email.value &&
    password.value.length >= 6 &&
    password.value === confirm.value &&
    timezoneId.value,
)

function setupLocationPayload() {
  const payload: {
    country_code?: string
    latitude?: number
    longitude?: number
    location_label?: string
  } = {}
  if (countryCode.value.trim()) payload.country_code = countryCode.value.trim()
  const parsedLatitude = normalizeCoordinateInput(latitude.value)
  const parsedLongitude = normalizeCoordinateInput(longitude.value)
  if (parsedLatitude !== null) payload.latitude = parsedLatitude
  if (parsedLongitude !== null) payload.longitude = parsedLongitude
  if (locationLabel.value.trim()) payload.location_label = locationLabel.value.trim()
  return Object.keys(payload).length > 0 ? payload : undefined
}

async function handleSubmit() {
  if (password.value !== confirm.value) {
    error.value = t('auth.setup.passwordsMismatch')
    return
  }
  submitting.value = true
  error.value = ''
  try {
    await setup(
      email.value.trim(),
      password.value,
      primaryLanguage.value,
      timezoneId.value,
      setupLocationPayload(),
    )
    router.replace('/')
  } catch (err: unknown) {
    type AxiosError = { response?: { status?: number; data?: { detail?: string } } }
    const e = err as AxiosError
    if (e?.response?.status === 409) {
      error.value = t('auth.errors.setupAlreadyComplete')
      setTimeout(() => router.replace('/login'), 1500)
    } else if (e?.response?.status === 503) {
      error.value = t('auth.errors.setupNotAllowed')
    } else if (e?.response?.data?.detail) {
      error.value = String(e.response.data.detail)
    } else if (err instanceof Error && err.message) {
      // Not an axios error shape (e.g. a client-side bug thrown while building
      // the request, such as the location payload) — surface err.message
      // instead of masquerading as a vague server error. Still logged for
      // diagnosability since first-time setup has no other error channel.
      console.error('[SetupPage] handleSubmit failed', err)
      error.value = t('common.errors.saveFailed', { reason: err.message })
    } else {
      error.value = t('common.errors.generic')
    }
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.auth-screen {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}
.auth-card {
  width: min(100%, 380px);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 32px 24px;
  background: var(--color-surface);
}
.auth-brand {
  text-align: center;
  margin-bottom: 20px;
}
.auth-logo {
  width: 72px;
  height: 72px;
  object-fit: contain;
  margin: 0 auto 8px;
  display: block;
}
.auth-wordmark {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 500;
  letter-spacing: 0.05em;
  color: var(--color-text);
  margin-bottom: 14px;
}
.auth-card h1 {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 400;
  letter-spacing: 0.02em;
  color: var(--color-text-secondary);
}
.kizuna {
  margin: 0;
  font-size: 11px;
  letter-spacing: 0.32em;
  color: var(--color-secondary);
}
.hint {
  margin: 0 0 16px;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.5;
  text-align: center;
}
.auth-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.auth-submit {
  margin-top: 12px;
  padding: 10px 12px;
  background: var(--color-primary);
  color: #1a1f24;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
  font-weight: 600;
  transition: background 0.15s;
}
.auth-submit:hover:not(:disabled) {
  background: var(--color-primary-light);
}
.auth-submit:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.auth-error {
  margin: 8px 0 0;
  color: #ff8a75;
  font-size: 13px;
}
.setup-language-hint,
.setup-timezone-hint,
.setup-location-hint {
  margin: 4px 0 0;
  line-height: 1.5;
}
.setup-location-grid {
  display: grid;
  grid-template-columns: 0.8fr 1fr 1fr;
  gap: 8px;
}
</style>
