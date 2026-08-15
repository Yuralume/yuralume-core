<script setup lang="ts">
/**
 * "Back to the Yuralume account centre" exit — hosted deployments only.
 *
 * Hosted players arrive through the Portal's one-way `enter_url` hand-off and
 * previously had no way back: the SPA carried no reference to the Portal at
 * all. The URL is runtime config (`/auth/config` → `portal_url`) rather than a
 * build-time env, because the hosted SPA image is shared across environments.
 *
 * Home is the sidebar credit badge (`CloudCreditsBadge`), not the settings
 * tab: subscription, credits and account data are one subject, so the entry
 * belongs next to the ledger link inside the same expanded card rather than at
 * the bottom of a preferences pane a player has to already know about. The
 * `inline` variant is that in-card row; the default `card` variant is the
 * standalone box the badge falls back to when the balance is unknown.
 *
 * Renders nothing on self-host (no cloud mode) or when a cloud deployment did
 * not configure a Portal, so the self-host sidebar stays byte-identical.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import { portalHomeUrl } from '@/utils/creditsFormat'

const props = withDefaults(
  defineProps<{ variant?: 'card' | 'inline' }>(),
  { variant: 'card' },
)

const { t } = useI18n()
const { cloudMode, portalUrl } = useAuth()

const href = computed(() => {
  if (!cloudMode.value) return null
  return portalHomeUrl(portalUrl.value)
})
</script>

<template>
  <a
    v-if="href"
    class="portal-account-link"
    :class="`portal-account-link--${props.variant}`"
    :href="href"
    target="_self"
    :title="t('credits.portalEntry.title')"
  >
    <span class="portal-account-link__text">
      <span class="portal-account-link__title">{{ t('credits.portalEntry.title') }}</span>
      <span class="portal-account-link__hint">{{ t('credits.portalEntry.hint') }}</span>
    </span>
    <span class="portal-account-link__chevron" aria-hidden="true">›</span>
  </a>
</template>

<style scoped>
.portal-account-link {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
  color: var(--color-text);
}

.portal-account-link__text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.portal-account-link__title {
  font-weight: 600;
  color: var(--color-primary-light);
}

.portal-account-link__hint {
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.portal-account-link__chevron {
  margin-left: auto;
  flex: 0 0 auto;
  color: var(--color-text-secondary);
  transition: transform 0.2s, color 0.2s;
}

.portal-account-link:hover .portal-account-link__chevron {
  transform: translateX(2px);
  color: var(--color-primary-light);
}

/* Standalone box — used when it stands on its own in the sidebar. */
.portal-account-link--card {
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
}

.portal-account-link--card:hover {
  border-color: var(--color-primary-light);
}

.portal-account-link--card .portal-account-link__title {
  font-size: 13px;
}

.portal-account-link--card .portal-account-link__hint {
  font-size: var(--font-xs);
}

/* In-card row — a footer inside the credit card, so no second border box.
   Reads as one continuation of the card, separated only by its own rule. */
.portal-account-link--inline {
  margin-top: 2px;
  padding: 6px 0 0;
  font-size: var(--font-xs);
}

.portal-account-link--inline .portal-account-link__title {
  font-size: var(--font-xs);
  color: var(--color-text);
}

.portal-account-link--inline:hover .portal-account-link__title {
  color: var(--color-primary-light);
}

.portal-account-link--inline .portal-account-link__hint {
  font-size: 10px;
  line-height: 1.3;
}
</style>
