<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from '@/composables/useAuth'
import {
  demoSessionErrorCopy,
  type DemoSessionErrorCopy,
} from '@/utils/demoSessionErrors'
import { demoUnavailableActions } from '@/utils/demoConversionLinks'

const route = useRoute()
const router = useRouter()
const auth = useAuth()

const loading = ref(true)
const error = ref<DemoSessionErrorCopy | null>(null)

const provider = computed(() => String(route.params.provider || '').trim())
const title = computed(() => (
  loading.value ? 'Starting demo' : error.value ? error.value.title : 'Demo ready'
))

function callbackRedirectUri(): string {
  return `${window.location.origin}/demo/oauth/${provider.value}/callback`
}

function storedCodeVerifier(): string | null {
  const state = String(route.query.state || '').trim()
  if (!state) return null
  const key = `yuralume_demo_oauth_verifier:${state}`
  const verifier = sessionStorage.getItem(key)
  sessionStorage.removeItem(key)
  return verifier
}

onMounted(async () => {
  const code = String(route.query.code || '').trim()
  if (!provider.value || !code) {
    error.value = {
      title: 'Demo unavailable',
      message: 'Missing OAuth callback data.',
      actions: demoUnavailableActions(),
    }
    loading.value = false
    return
  }
  try {
    await auth.loginWithDemoSession({
      provider: provider.value,
      authorization_code: code,
      redirect_uri: callbackRedirectUri(),
      code_verifier: storedCodeVerifier(),
    })
    const redirect = typeof route.query.redirect === 'string'
      ? route.query.redirect
      : '/'
    await router.replace(redirect.startsWith('/') ? redirect : '/')
  } catch (caught) {
    error.value = demoSessionErrorCopy(caught)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <main class="demo-oauth-callback">
    <section class="panel">
      <h1>{{ title }}</h1>
      <p v-if="loading">Connecting your verified account.</p>
      <p v-else-if="error">{{ error.message }}</p>
      <p v-else>Redirecting.</p>
      <div v-if="error" class="actions">
        <a
          v-for="action in error.actions"
          :key="action.label"
          class="button"
          :class="`button-${action.variant}`"
          :href="action.href"
          :target="action.external ? '_blank' : undefined"
          :rel="action.external ? 'noopener noreferrer' : undefined"
        >
          {{ action.label }}
        </a>
        <RouterLink class="button button-ghost" to="/login">Back to login</RouterLink>
      </div>
    </section>
  </main>
</template>

<style scoped>
.demo-oauth-callback {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: #f7f3ea;
  color: #25211b;
}

.panel {
  width: min(420px, 100%);
  border: 1px solid rgba(37, 33, 27, 0.14);
  border-radius: 8px;
  background: #fffdf7;
  padding: 28px;
  box-shadow: 0 18px 45px rgba(37, 33, 27, 0.12);
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
  background: #25211b;
  color: #fffdf7;
  text-decoration: none;
}

.button-secondary,
.button-ghost {
  border: 1px solid rgba(37, 33, 27, 0.18);
  background: #fffdf7;
  color: #25211b;
}
</style>
