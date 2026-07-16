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
        <RouterLink class="button button-ghost" to="/login">
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
