<template>
  <div class="auth-screen">
    <div class="auth-card">
      <div class="auth-brand">
        <img src="/logo-mark.png" alt="Yuralume" class="auth-logo" />
        <div class="auth-wordmark">{{ t('auth.login.brand') }}</div>
        <h1>{{ t('auth.login.title') }}</h1>
        <p class="kizuna">{{ t('auth.login.tagline') }}</p>
      </div>
      <form @submit.prevent="handleSubmit" class="auth-form">
        <label class="field-label" for="login-email">{{ t('auth.login.emailLabel') }}</label>
        <input
          id="login-email"
          v-model="email"
          type="email"
          autocomplete="username"
          required
          class="field-input"
          :disabled="submitting"
        />

        <label class="field-label" for="login-password">{{ t('auth.login.passwordLabel') }}</label>
        <input
          id="login-password"
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
          class="field-input"
          :disabled="submitting"
        />

        <button
          type="submit"
          class="auth-submit"
          :disabled="submitting || !email || !password"
        >
          {{ submitting ? t('auth.login.submitting') : t('auth.login.submit') }}
        </button>

        <p v-if="error" class="auth-error">{{ error }}</p>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'

const router = useRouter()
const route = useRoute()
const { login } = useAuth()
const { t } = useI18n()

const email = ref('')
const password = ref('')
const submitting = ref(false)
const error = ref('')

async function handleSubmit() {
  submitting.value = true
  error.value = ''
  try {
    await login(email.value.trim(), password.value)
    // Honour ?redirect=... if guard set it; otherwise go home.
    const next = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.replace(next)
  } catch (err: unknown) {
    type AxiosError = { response?: { status?: number; data?: { detail?: string } } }
    const e = err as AxiosError
    if (e?.response?.status === 401) {
      error.value = t('auth.errors.invalidCredentials')
    } else if (e?.response?.data?.detail) {
      error.value = String(e.response.data.detail)
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
  width: min(100%, 360px);
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
</style>
