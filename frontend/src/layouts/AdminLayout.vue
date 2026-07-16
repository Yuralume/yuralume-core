<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, RouterLink, RouterView } from 'vue-router'
import { useI18n } from 'vue-i18n'
import SidebarBrand from '@/components/SidebarBrand.vue'
import { useAuth } from '@/composables/useAuth'
import { useLocale } from '@/composables/useLocale'
import { buildInfoTitle, formatBuildVersion } from '@/utils/buildInfo'
import {
  isModelRoutingDiscovered,
  rememberModelRoutingDiscovered,
} from '@/utils/modelRoutingDiscovery'

interface AdminNavItem {
  /**
   * i18n key that resolves through `t(...)`. Stored as a key (not a
   * literal string) so the nav re-renders correctly when the user
   * flips the locale switcher. The render expression below pulls it
   * through `useI18n().t`.
   */
  labelKey: string
  to: string
  group: 'overview' | 'character' | 'media' | 'behavior' | 'ops'
  debugOnly?: boolean
  cloudLocked?: boolean
}

const navItems: AdminNavItem[] = [
  { labelKey: 'admin.nav.overview', to: '/admin', group: 'overview' },

  { labelKey: 'admin.nav.characters', to: '/admin/characters', group: 'character' },
  { labelKey: 'admin.nav.memories', to: '/admin/memories', group: 'character' },
  { labelKey: 'admin.nav.disposition', to: '/admin/dispositions', group: 'character', debugOnly: true },

  { labelKey: 'admin.nav.providers', to: '/admin/providers', group: 'media', cloudLocked: true },
  { labelKey: 'admin.nav.models', to: '/admin/models', group: 'media', cloudLocked: true },
  { labelKey: 'admin.nav.imageProfiles', to: '/admin/image-profiles', group: 'media', cloudLocked: true },
  { labelKey: 'admin.nav.videoProfiles', to: '/admin/video-profiles', group: 'media', cloudLocked: true },
  { labelKey: 'admin.nav.voices', to: '/admin/voice', group: 'media', cloudLocked: true },
  { labelKey: 'admin.nav.loras', to: '/admin/loras', group: 'media', cloudLocked: true },
  { labelKey: 'admin.nav.devDocs', to: '/admin/dev-docs', group: 'media' },

  { labelKey: 'admin.nav.proactive', to: '/admin/proactive', group: 'behavior', debugOnly: true },
  { labelKey: 'admin.nav.schedule', to: '/admin/schedule', group: 'behavior' },
  { labelKey: 'admin.nav.followUps', to: '/admin/follow-ups', group: 'behavior', debugOnly: true },
  { labelKey: 'admin.nav.world', to: '/admin/world', group: 'behavior' },

  { labelKey: 'admin.nav.observability', to: '/admin/observability', group: 'ops', debugOnly: true },
  { labelKey: 'admin.nav.channels', to: '/admin/channels', group: 'ops' },
  { labelKey: 'admin.nav.siteSettings', to: '/admin/site-settings', group: 'ops', cloudLocked: true },
  { labelKey: 'admin.nav.characterFreeze', to: '/admin/character-freeze', group: 'ops' },
  { labelKey: 'admin.nav.users', to: '/admin/users', group: 'ops', cloudLocked: true },
]

const groupLabelKeys: Record<AdminNavItem['group'], string> = {
  overview: 'admin.nav.groupOverview',
  character: 'admin.nav.groupCharacter',
  media: 'admin.nav.groupMedia',
  behavior: 'admin.nav.groupBehavior',
  ops: 'admin.nav.groupOps',
}

const { cloudMode, debugUiEnabled, buildInfo } = useAuth()
const buildVersionLabel = computed(() => formatBuildVersion(buildInfo.value))
const buildVersionTitle = computed(() => buildInfoTitle(buildInfo.value))

const groupedNav = computed(() => {
  const groups: Record<AdminNavItem['group'], AdminNavItem[]> = {
    overview: [],
    character: [],
    media: [],
    behavior: [],
    ops: [],
  }
  for (const item of navItems) {
    if (item.debugOnly && !debugUiEnabled.value) continue
    if (item.cloudLocked && cloudMode.value) continue
    groups[item.group].push(item)
  }
  return groups
})

const route = useRoute()
const { t } = useI18n()
const { locale, supported } = useLocale()

// One-time coachmark dot next to the "LLM routing" nav item: hidden as
// soon as the user visits /admin/models once, persisted in
// localStorage so it stays hidden across sessions.
const modelRoutingDiscovered = ref(isModelRoutingDiscovered(window.localStorage))
watch(
  () => route.path,
  path => {
    if (modelRoutingDiscovered.value) return
    if (path === '/admin/models' || path.startsWith('/admin/models/')) {
      rememberModelRoutingDiscovered(window.localStorage)
      modelRoutingDiscovered.value = true
    }
  },
  { immediate: true },
)

const breadcrumb = computed(() => {
  const path = route.path
  const matched = navItems.find(item => {
    if (item.to === '/admin') return path === '/admin'
    return path === item.to || path.startsWith(item.to + '/')
  })
  return matched ? t(matched.labelKey) : path
})
</script>

<template>
  <div class="admin-layout">
    <aside class="admin-layout__sidebar">
      <SidebarBrand
        :to="{ path: '/' }"
        :link-title="t('admin.layout.back')"
        :subtitle="`← ${t('admin.layout.back')}`"
      />

      <nav class="admin-layout__nav">
        <div
          v-for="(items, group) in groupedNav"
          :key="group"
          class="admin-layout__nav-group"
        >
          <div class="admin-layout__nav-label">{{ t(groupLabelKeys[group]) }}</div>
          <RouterLink
            v-for="item in items"
            :key="item.to"
            :to="item.to"
            class="admin-layout__nav-link"
            :class="{ 'is-active': route.path === item.to || (item.to !== '/admin' && route.path.startsWith(item.to + '/')) }"
          >
            {{ t(item.labelKey) }}
            <span
              v-if="item.to === '/admin/models' && !modelRoutingDiscovered"
              class="admin-layout__nav-dot"
              :title="t('admin.nav.modelsDiscoveryHint')"
              :aria-label="t('admin.nav.modelsDiscoveryHint')"
            />
          </RouterLink>
        </div>
      </nav>
    </aside>

    <main class="admin-layout__main">
      <header class="admin-layout__topbar">
        <div class="admin-layout__breadcrumb">
          <RouterLink to="/admin" class="admin-layout__crumb">{{ t('admin.layout.brand') }}</RouterLink>
          <span v-if="route.path !== '/admin'" class="admin-layout__crumb-sep">/</span>
          <span v-if="route.path !== '/admin'" class="admin-layout__crumb is-current">{{ breadcrumb }}</span>
        </div>
        <div class="admin-layout__topbar-meta">
          <span
            v-if="buildVersionLabel"
            class="admin-layout__version"
            :title="buildVersionTitle"
          >
            {{ buildVersionLabel }}
          </span>
          <label class="admin-layout__locale-switcher">
            <span class="admin-layout__locale-label">{{ t('locale.switcher.label') }}</span>
            <select
              v-model="locale"
              class="field-select admin-layout__locale-select"
              :title="t('locale.switcher.hint')"
            >
              <option v-for="opt in supported" :key="opt.code" :value="opt.code">
                {{ opt.label }}
              </option>
            </select>
          </label>
          <span class="admin-layout__warn-badge" :title="t('admin.layout.warnHint')">⚠ {{ t('admin.layout.warn') }}</span>
        </div>
      </header>

      <div class="admin-layout__content">
        <RouterView />
      </div>
    </main>
  </div>
</template>

<style scoped>
.admin-layout {
  display: grid;
  grid-template-columns: var(--sidebar-width) 1fr;
  height: 100%;
  width: 100%;
  background: var(--color-bg);
  color: var(--color-text);
}

.admin-layout__sidebar {
  display: flex;
  flex-direction: column;
  background: var(--color-bg-secondary);
  border-right: 1px solid var(--color-border);
  overflow-y: auto;
}

.admin-layout__nav {
  display: flex;
  flex-direction: column;
  padding: var(--space-4) 0;
  gap: var(--space-4);
}
.admin-layout__nav-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.admin-layout__nav-label {
  padding: 0 var(--space-4);
  font-size: var(--font-sm);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--color-text-secondary);
  margin-bottom: var(--space-2);
}
.admin-layout__nav-link {
  display: block;
  padding: 9px var(--space-4);
  font-size: var(--font-md);
  color: var(--color-text);
  text-decoration: none;
  border-left: 2px solid transparent;
  transition: background-color 0.15s, border-color 0.15s, color 0.15s;
}
.admin-layout__nav-link:hover {
  background: rgba(255, 255, 255, 0.04);
}
.admin-layout__nav-link.is-active {
  background: rgba(232, 155, 133, 0.10);
  border-left-color: var(--color-primary);
  color: var(--color-primary);
}
.admin-layout__nav-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-left: var(--space-2);
  border-radius: 50%;
  background: var(--color-primary);
  vertical-align: middle;
}

.admin-layout__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}

.admin-layout__topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-2) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg-secondary);
  height: var(--header-height);
  flex-shrink: 0;
}

.admin-layout__breadcrumb {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--font-md);
  min-width: 0;
}
.admin-layout__crumb {
  color: var(--color-text-secondary);
  text-decoration: none;
}
.admin-layout__crumb.is-current {
  color: var(--color-text);
  font-weight: 500;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.admin-layout__crumb-sep {
  color: var(--color-text-secondary);
  margin: 0 var(--space-1);
}

.admin-layout__warn-badge {
  display: inline-flex;
  align-items: center;
  padding: 3px 10px;
  font-size: var(--font-xs);
  color: #ffd9a3;
  background: rgba(212, 165, 95, 0.15);
  border: 1px solid rgba(212, 165, 95, 0.4);
  border-radius: 999px;
}

.admin-layout__version {
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  white-space: nowrap;
}

.admin-layout__topbar-meta {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-width: 0;
}
.admin-layout__locale-switcher {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
.admin-layout__locale-label {
  white-space: nowrap;
}
.admin-layout__locale-select {
  /* match the existing field-select sizing but stay compact in the topbar */
  min-width: 130px;
  font-size: var(--font-sm);
  padding: 3px 8px;
}

.admin-layout__content {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-5);
}

@media (max-width: 900px) {
  .admin-layout {
    grid-template-columns: 1fr;
  }
  .admin-layout__sidebar {
    display: none;
  }
}

@media (max-width: 600px) {
  .admin-layout__topbar {
    height: auto;
    min-height: var(--header-height);
    align-items: flex-start;
    flex-wrap: wrap;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
  }

  .admin-layout__breadcrumb {
    flex: 1 1 100%;
  }

  .admin-layout__topbar-meta {
    width: 100%;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .admin-layout__locale-switcher {
    flex: 1 1 auto;
    min-width: 0;
  }

  .admin-layout__locale-label {
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .admin-layout__locale-select {
    min-width: 112px;
  }

  .admin-layout__warn-badge {
    flex: 0 0 auto;
  }
}
</style>
