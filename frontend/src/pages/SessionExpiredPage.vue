<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'

const route = useRoute()
const auth = useAuth()
const { t } = useI18n()

/**
 * The Portal is where a hosted player's session actually comes from: they
 * press "enter game" there and Core exchanges the one-time code. So the exit
 * is the Portal root, not a deep link — the entry CTA lives on its dashboard.
 */
const portalHref = computed(() => {
  const base = (auth.portalUrl.value ?? '').trim().replace(/\/+$/, '')
  return base || null
})

/**
 * Deployments reach this screen only when a Portal exists, but the password
 * form stays one click away: a Cloud account that *does* have a password (an
 * operator rather than an OAuth player) can still sign in directly.
 */
const loginTarget = computed(() => {
  const redirect = route.query.redirect
  return {
    path: '/login',
    query: typeof redirect === 'string' && redirect ? { redirect } : {},
  }
})
</script>

<template>
  <main class="session-expired">
    <section class="panel">
      <h1>{{ t('auth.sessionExpired.title') }}</h1>
      <p>{{ t('auth.sessionExpired.body') }}</p>
      <div class="actions">
        <a v-if="portalHref" class="button button-primary" :href="portalHref">
          {{ t('auth.sessionExpired.backToPortal') }}
        </a>
        <RouterLink class="button button-ghost" :to="loginTarget">
          {{ t('auth.sessionExpired.usePassword') }}
        </RouterLink>
      </div>
    </section>
  </main>
</template>

<style scoped>
.session-expired {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}

.panel {
  width: min(420px, 100%);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-surface);
  padding: 32px 24px;
}

h1 {
  margin: 0 0 12px;
  font-size: 24px;
  line-height: 1.2;
}

p {
  margin: 0;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.actions {
  margin-top: 20px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.button {
  display: inline-flex;
  align-items: center;
  min-height: 40px;
  padding: 0 14px;
  border-radius: 8px;
  text-decoration: none;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
}

.button-primary {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #1a1f24;
  font-weight: 600;
}
</style>
