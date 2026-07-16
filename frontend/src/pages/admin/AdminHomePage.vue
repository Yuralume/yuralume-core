<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { UiCard, UiBadge } from '@/components/ui'
import { getActiveModelPreference, getFeatureModelGroups, listProviders } from '@/utils/api/system'
import { countRealProviders, resolveNeedsRoutingSetup } from '@/utils/providerSetup'
import { useAuth } from '@/composables/useAuth'

interface AdminPanelEntry {
  to: string
  titleKey: string
  descriptionKey: string
  group: 'character' | 'media' | 'behavior' | 'ops'
  /**
   * Marks a developer-facing card that should stay hidden unless the
   * deployment owner flipped ``KOKORO_DEBUG_UI_ENABLED`` on. Public
   * builds get a tighter player-facing surface; the underlying admin
   * API stays reachable for curl-based exports either way.
   */
  debugOnly?: boolean
  cloudLocked?: boolean
}

const { t } = useI18n()
const { cloudMode, debugUiEnabled } = useAuth()
const runtimeProviders = ref<string[]>([])
const providersLoaded = ref(false)
const routingLoaded = ref(false)
const routingConfigured = ref(true)

function isVisible(entry: AdminPanelEntry): boolean {
  if (entry.cloudLocked === true && cloudMode.value === true) return false
  return entry.debugOnly !== true || debugUiEnabled.value === true
}

const entries: AdminPanelEntry[] = [
  {
    to: '/admin/characters',
    titleKey: 'admin.home.entries.characters.title',
    descriptionKey: 'admin.home.entries.characters.description',
    group: 'character',
  },
  {
    to: '/admin/memories',
    titleKey: 'admin.home.entries.memories.title',
    descriptionKey: 'admin.home.entries.memories.description',
    group: 'character',
  },
  {
    to: '/admin/channels',
    titleKey: 'admin.home.entries.channels.title',
    descriptionKey: 'admin.home.entries.channels.description',
    group: 'ops',
  },
  {
    to: '/admin/dispositions',
    titleKey: 'admin.home.entries.dispositions.title',
    descriptionKey: 'admin.home.entries.dispositions.description',
    group: 'character',
    debugOnly: true,
  },
  {
    to: '/admin/providers',
    titleKey: 'admin.home.entries.providers.title',
    descriptionKey: 'admin.home.entries.providers.description',
    group: 'media',
    cloudLocked: true,
  },
  {
    to: '/admin/models',
    titleKey: 'admin.home.entries.models.title',
    descriptionKey: 'admin.home.entries.models.description',
    group: 'media',
    cloudLocked: true,
  },
  {
    to: '/admin/image-profiles',
    titleKey: 'admin.home.entries.imageProfiles.title',
    descriptionKey: 'admin.home.entries.imageProfiles.description',
    group: 'media',
    cloudLocked: true,
  },
  {
    to: '/admin/video-profiles',
    titleKey: 'admin.home.entries.videoProfiles.title',
    descriptionKey: 'admin.home.entries.videoProfiles.description',
    group: 'media',
    cloudLocked: true,
  },
  {
    to: '/admin/voice',
    titleKey: 'admin.home.entries.voice.title',
    descriptionKey: 'admin.home.entries.voice.description',
    group: 'media',
    cloudLocked: true,
  },
  {
    to: '/admin/loras',
    titleKey: 'admin.home.entries.loras.title',
    descriptionKey: 'admin.home.entries.loras.description',
    group: 'media',
    cloudLocked: true,
  },
  {
    to: '/admin/proactive',
    titleKey: 'admin.home.entries.proactive.title',
    descriptionKey: 'admin.home.entries.proactive.description',
    group: 'behavior',
    debugOnly: true,
  },
  {
    to: '/admin/schedule',
    titleKey: 'admin.home.entries.schedule.title',
    descriptionKey: 'admin.home.entries.schedule.description',
    group: 'behavior',
  },
  {
    to: '/admin/follow-ups',
    titleKey: 'admin.home.entries.followUps.title',
    descriptionKey: 'admin.home.entries.followUps.description',
    group: 'behavior',
    debugOnly: true,
  },
  {
    to: '/admin/world',
    titleKey: 'admin.home.entries.world.title',
    descriptionKey: 'admin.home.entries.world.description',
    group: 'behavior',
  },
  {
    to: '/admin/observability',
    titleKey: 'admin.home.entries.observability.title',
    descriptionKey: 'admin.home.entries.observability.description',
    group: 'ops',
  },
  {
    to: '/admin/users',
    titleKey: 'admin.home.entries.users.title',
    descriptionKey: 'admin.home.entries.users.description',
    group: 'ops',
    cloudLocked: true,
  },
]

const groupLabelKeys: Record<AdminPanelEntry['group'], string> = {
  character: 'admin.nav.groupCharacter',
  media: 'admin.nav.groupMedia',
  behavior: 'admin.nav.groupBehavior',
  ops: 'admin.nav.groupOps',
}

const groups = computed(() =>
  (['character', 'media', 'behavior', 'ops'] as const)
    .map(g => ({
      key: g,
      label: t(groupLabelKeys[g]),
      items: entries.filter(e => e.group === g && isVisible(e)),
    }))
    // Hide the group header entirely when the debug-only filter
    // emptied it (e.g. "ops" with observability hidden would otherwise
    // render a lonely "users" card under an empty heading).
    .filter(g => g.items.length > 0),
)

const needsProviderSetup = computed(() =>
  !cloudMode.value
  && providersLoaded.value
  && countRealProviders(runtimeProviders.value) === 0,
)

// Mutually exclusive with `needsProviderSetup`: only offer the routing
// next-step once at least one real provider exists. `routingLoaded`
// starts false so the card stays hidden until the parallel API calls
// below resolve, avoiding a flash before we know the real state.
const needsRoutingSetup = computed(() =>
  !cloudMode.value
  && providersLoaded.value
  && routingLoaded.value
  && countRealProviders(runtimeProviders.value) > 0
  && !routingConfigured.value,
)

onMounted(async () => {
  if (cloudMode.value) {
    providersLoaded.value = true
    routingLoaded.value = true
    return
  }
  try {
    const [providerIds, groupsPref, activeModel] = await Promise.all([
      listProviders(),
      getFeatureModelGroups().catch(() => null),
      getActiveModelPreference().catch(() => null),
    ])
    runtimeProviders.value = providerIds
    if (groupsPref) {
      routingConfigured.value = !resolveNeedsRoutingSetup({
        cloudMode: cloudMode.value,
        providerIds,
        groups: groupsPref.groups,
        activeModel: activeModel ?? groupsPref.active_model,
      })
    }
  } finally {
    providersLoaded.value = true
    routingLoaded.value = true
  }
})
</script>

<template>
  <div class="admin-home">
    <header class="admin-home__header">
      <h1>{{ t('admin.home.title') }}</h1>
      <p class="admin-home__lead">{{ t('admin.home.subtitle') }}</p>
    </header>

    <RouterLink
      v-if="needsProviderSetup"
      to="/admin/providers"
      class="admin-home__setup"
    >
      <UiCard hoverable>
        <template #header>
          <div class="admin-home__entry-header">
            <h2 class="admin-home__setup-title">{{ t('admin.home.providerSetup.title') }}</h2>
            <UiBadge variant="warning">{{ t('admin.home.providerSetup.badge') }}</UiBadge>
          </div>
        </template>
        <p class="admin-home__entry-desc">{{ t('admin.home.providerSetup.description') }}</p>
      </UiCard>
    </RouterLink>

    <RouterLink
      v-if="needsRoutingSetup"
      to="/admin/models"
      class="admin-home__setup"
    >
      <UiCard hoverable>
        <template #header>
          <div class="admin-home__entry-header">
            <h2 class="admin-home__setup-title">{{ t('admin.home.routingSetup.title') }}</h2>
            <UiBadge variant="primary">{{ t('admin.home.routingSetup.badge') }}</UiBadge>
          </div>
        </template>
        <p class="admin-home__entry-desc">{{ t('admin.home.routingSetup.description') }}</p>
      </UiCard>
    </RouterLink>

    <section v-for="group in groups" :key="group.key" class="admin-home__group">
      <h2 class="admin-home__group-title">{{ group.label }}</h2>
      <div class="admin-home__grid">
        <RouterLink
          v-for="entry in group.items"
          :key="entry.to"
          :to="entry.to"
          class="admin-home__entry"
        >
          <UiCard hoverable>
            <template #header>
              <div class="admin-home__entry-header">
                <h3 class="admin-home__entry-title">{{ t(entry.titleKey) }}</h3>
              </div>
            </template>
            <p class="admin-home__entry-desc">{{ t(entry.descriptionKey) }}</p>
          </UiCard>
        </RouterLink>
      </div>
    </section>
  </div>
</template>

<style scoped>
.admin-home {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  max-width: 1100px;
}
.admin-home__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.admin-home__lead {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.admin-home__setup {
  color: inherit;
  text-decoration: none;
}
.admin-home__setup-title {
  margin: 0;
  font-size: var(--font-md);
}
.admin-home__group-title {
  margin: 0 0 var(--space-3);
  font-size: var(--font-md);
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.admin-home__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--space-3);
}
.admin-home__entry {
  text-decoration: none;
  color: inherit;
  display: block;
}
.admin-home__entry-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  width: 100%;
}
.admin-home__entry-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.admin-home__entry-desc {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
</style>
