<script setup lang="ts">
/**
 * Unread service-notice dot — cloud mode only (AN1).
 *
 * Sits beside the credit badge and answers exactly one question: is there
 * something new on the notice board? Reading happens in the Portal, so this
 * carries no announcement text and needs no translations of its own beyond the
 * three words on the link.
 *
 * Renders nothing when there is nothing unread. A permanent "notices" entry
 * would be a second navigation item competing with the game for a sidebar that
 * is already full; the Portal link in the account entry is the way in when a
 * player goes looking on their own.
 */
import { computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import { useCloudAnnouncements } from '@/composables/useCloudAnnouncements'
import { portalPath } from '@/utils/creditsFormat'

const { t } = useI18n()
const { cloudMode, portalUrl } = useAuth()
const announcements = useCloudAnnouncements()

const href = computed(() => portalPath(portalUrl.value, '/announcements'))
// Without a Portal to send them to, a dot would only tell a player that
// something exists which they cannot reach.
const visible = computed(
  () => cloudMode.value && announcements.hasUnread.value && !!href.value,
)

// Coming back to the tab is the moment the dot is most likely wrong: the most
// common way to clear it is to go and read the board in the Portal.
function onVisibilityChange() {
  if (document.visibilityState === 'visible') {
    announcements.refreshOnReturn()
  }
}

onMounted(() => {
  if (!cloudMode.value) return
  void announcements.ensureLoaded()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <a
    v-if="visible"
    class="notice-dot"
    :href="href ?? undefined"
    :title="t('announcements.dot.tooltip')"
    :aria-label="t('announcements.dot.ariaLabel')"
  >
    <span class="notice-dot__mark" aria-hidden="true" />
    <span class="notice-dot__label">{{ t('announcements.dot.label') }}</span>
  </a>
</template>

<style scoped>
.notice-dot {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 6px 12px 0;
  padding: 5px 10px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.3;
  text-decoration: none;
  transition: background 0.2s, color 0.2s;
}

.notice-dot:hover {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text);
}

.notice-dot__mark {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-primary-light);
}

.notice-dot__label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
