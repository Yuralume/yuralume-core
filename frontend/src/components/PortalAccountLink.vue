<script setup lang="ts">
/**
 * "Back to the Yuralume account centre" exit — hosted deployments only.
 *
 * Hosted players arrive through the Portal's one-way `enter_url` hand-off and
 * previously had no way back: the SPA carried no reference to the Portal at
 * all. The URL is runtime config (`/auth/config` → `portal_url`) rather than a
 * build-time env, because the hosted SPA image is shared across environments.
 *
 * Renders nothing on self-host (no cloud mode) or when a cloud deployment did
 * not configure a Portal, so the self-host settings pane is byte-identical.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'

const { t } = useI18n()
const { cloudMode, portalUrl } = useAuth()

const href = computed(() => {
  if (!cloudMode.value) return null
  const base = (portalUrl.value ?? '').trim().replace(/\/+$/, '')
  return base || null
})
</script>

<template>
  <a
    v-if="href"
    class="portal-account-link"
    :href="href"
    target="_self"
    :title="t('credits.portalEntry.title')"
  >
    <span class="portal-account-link__title">{{ t('credits.portalEntry.title') }}</span>
    <span class="portal-account-link__hint">{{ t('credits.portalEntry.hint') }}</span>
  </a>
</template>

<style scoped>
.portal-account-link {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  text-decoration: none;
  color: var(--color-text);
}

.portal-account-link:hover {
  border-color: var(--color-primary-light);
}

.portal-account-link__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
}

.portal-account-link__hint {
  font-size: 11px;
  color: var(--color-text-secondary);
}
</style>
