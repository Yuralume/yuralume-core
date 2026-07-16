<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  buildDemoOAuthAuthorizeUrl,
  supportedDemoOAuthProvider,
} from '@/utils/demoOAuth'
import { getDemoOAuthConfig } from '@/utils/api/auth'
import {
  demoUnavailableActions,
  type DemoConversionAction,
} from '@/utils/demoConversionLinks'

const route = useRoute()
const router = useRouter()
const loading = ref(true)
const error = ref<string | null>(null)
const actions = ref<DemoConversionAction[]>([])

onMounted(async () => {
  const provider = supportedDemoOAuthProvider(String(route.params.provider || ''))
  if (!provider) {
    error.value = 'Unsupported demo provider.'
    actions.value = demoUnavailableActions()
    loading.value = false
    return
  }
  try {
    // Fetch the public client id at runtime so it is not baked into the SPA
    // build; buildDemoOAuthAuthorizeUrl still fails fast if it is missing.
    const clientIds = await getDemoOAuthConfig()
    window.location.assign(
      await buildDemoOAuthAuthorizeUrl(provider, { clientIds }),
    )
  } catch {
    error.value = 'The live demo is not available right now.'
    actions.value = demoUnavailableActions()
    loading.value = false
  }
})

function backToLogin(): void {
  router.replace({ name: 'login' })
}
</script>

<template>
  <main class="demo-oauth-start">
    <section class="panel">
      <h1>{{ loading ? 'Opening demo' : 'Demo unavailable' }}</h1>
      <p v-if="loading">Redirecting to your verified sign-in.</p>
      <p v-else>{{ error }}</p>
      <div v-if="!loading" class="actions">
        <a
          v-for="action in actions"
          :key="action.label"
          class="button"
          :class="`button-${action.variant}`"
          :href="action.href"
          :target="action.external ? '_blank' : undefined"
          :rel="action.external ? 'noopener noreferrer' : undefined"
        >
          {{ action.label }}
        </a>
        <button class="button button-ghost" type="button" @click="backToLogin">
          Back to login
        </button>
      </div>
    </section>
  </main>
</template>

<style scoped>
.demo-oauth-start {
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
  min-height: 40px;
  border: 0;
  border-radius: 8px;
  background: #25211b;
  color: #fffdf7;
  padding: 0 14px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  text-decoration: none;
}

.button-secondary,
.button-ghost {
  border: 1px solid rgba(37, 33, 27, 0.18);
  background: #fffdf7;
  color: #25211b;
}
</style>
