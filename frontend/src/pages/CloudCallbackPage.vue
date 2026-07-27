<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const { t } = useI18n()

const loading = ref(true)
const error = ref<string | null>(null)

// A hosted player who never had a Core password cannot do anything useful at
// /login — sending them there is a dead end at the exact moment entry failed.
// When the deployment advertised a Portal we send them back to re-issue an
// entry code instead; the /login fallback stays for deployments without one.
const portalHref = computed(() => {
  const base = (auth.portalUrl.value ?? '').trim().replace(/\/+$/, '')
  return base || null
})

const title = computed(() => {
  if (loading.value) return t('auth.cloudCallback.loadingTitle')
  if (error.value) return t('auth.cloudCallback.errorTitle')
  return t('auth.cloudCallback.readyTitle')
})

onMounted(async () => {
  const code = String(route.query.code || '').trim()
  if (!code) {
    error.value = t('auth.cloudCallback.missingCode')
    loading.value = false
    return
  }
  try {
    await auth.loginWithCloudSession({ code })
    await router.replace('/')
  } catch {
    error.value = t('auth.cloudCallback.failed')
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <main class="cloud-callback">
    <section class="panel">
      <h1>{{ title }}</h1>
      <p v-if="loading">{{ t('auth.cloudCallback.loading') }}</p>
      <p v-else-if="error">{{ error }}</p>
      <p v-else>{{ t('auth.cloudCallback.redirecting') }}</p>
      <div v-if="error" class="actions">
        <a v-if="portalHref" class="button button-ghost" :href="portalHref">
          {{ t('auth.cloudCallback.backToPortal') }}
        </a>
        <RouterLink v-else class="button button-ghost" to="/login">
          {{ t('auth.cloudCallback.backToLogin') }}
        </RouterLink>
      </div>
    </section>
  </main>
</template>

<style scoped>
.cloud-callback {
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
</style>
