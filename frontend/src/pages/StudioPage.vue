<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

import { UiButton } from '@/components/ui'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const tabs = computed(() => [
  {
    routeName: 'studio-authoring',
    label: t('studio.tabs.authoring'),
    description: t('studio.tabs.authoringHint'),
    icon: '✦',
    accent: 'var(--color-primary)',
  },
  {
    routeName: 'studio-fusion-stories',
    label: t('studio.tabs.fusion'),
    description: t('studio.tabs.fusionHint'),
    icon: '◇',
    accent: 'var(--color-primary-light)',
  },
  {
    routeName: 'studio-branching-dramas',
    label: t('studio.tabs.branching'),
    description: t('studio.tabs.branchingHint'),
    icon: '◈',
    accent: 'var(--color-secondary)',
  },
  {
    routeName: 'studio-character-cards',
    label: t('studio.tabs.cards'),
    description: t('studio.tabs.cardsHint'),
    icon: '✧',
    accent: 'var(--color-spark)',
  },
])
</script>

<template>
  <main class="studio-shell">
    <div class="studio-shell__inner">
      <header class="studio-shell__header">
        <UiButton class="studio-shell__back glass-panel" variant="ghost" size="sm" @click="router.push('/')">
          {{ t('studio.actions.backToStage') }}
        </UiButton>
        <div class="studio-shell__copy">
          <p class="spark-label">{{ t('studio.eyebrow') }}</p>
          <h1 class="display-title display-title--gradient">{{ t('studio.title') }}</h1>
          <p>{{ t('studio.subtitle') }}</p>
        </div>
      </header>

      <nav class="studio-tabs" :aria-label="t('studio.tabs.aria')">
        <RouterLink
          v-for="tab in tabs"
          :key="tab.routeName"
          class="studio-tab sheen-hover"
          :class="{ 'studio-tab--active': route.name === tab.routeName }"
          :to="{ name: tab.routeName }"
          :style="{ '--studio-tab-accent': tab.accent }"
        >
          <span class="studio-tab__icon" aria-hidden="true">{{ tab.icon }}</span>
          <span class="studio-tab__label">{{ tab.label }}</span>
          <small>{{ tab.description }}</small>
        </RouterLink>
      </nav>

      <RouterView />
    </div>
  </main>
</template>

<style scoped>
.studio-shell {
  position: relative;
  height: 100%;
  overflow-y: auto;
  background:
    radial-gradient(980px 420px at 30% -120px, rgba(var(--color-primary-rgb), 0.24), transparent 68%),
    radial-gradient(820px 420px at 80% 115%, rgba(var(--color-secondary-rgb), 0.18), transparent 72%),
    var(--color-bg);
  color: var(--color-text);
}

.studio-shell::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  background-image:
    radial-gradient(circle, rgba(255, 255, 255, 0.26) 0 1px, transparent 1px),
    radial-gradient(circle, rgba(var(--color-spark-rgb), 0.18) 0 1px, transparent 1px);
  background-position: 0 0, 18px 22px;
  background-size: 44px 44px, 72px 72px;
  opacity: 0.28;
}

.studio-shell__inner {
  position: relative;
  z-index: 1;
  width: min(1180px, 100%);
  margin: 0 auto;
  padding: calc(var(--safe-area-top) + var(--space-5)) var(--space-5) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.studio-shell__header {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: var(--space-3);
}

.studio-shell__back {
  border-radius: 999px;
}

.studio-shell__copy {
  min-width: 0;
  display: grid;
  gap: var(--space-1);
}

.studio-shell__copy h1,
.studio-shell__copy p {
  margin: 0;
  letter-spacing: 0;
}

.studio-shell__copy h1 {
  font-size: 48px;
}

.studio-shell__copy p:not(.spark-label) {
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.studio-tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-2);
}

.studio-tab {
  --studio-tab-accent: var(--color-primary);
  min-width: 0;
  min-height: 92px;
  padding: var(--space-3);
  border: 1px solid color-mix(in srgb, var(--studio-tab-accent) 32%, transparent);
  border-radius: 8px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.07), rgba(255, 255, 255, 0.025)),
    rgba(18, 12, 42, 0.58);
  color: var(--color-text);
  text-decoration: none;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-rows: auto 1fr;
  align-items: center;
  justify-content: center;
  column-gap: var(--space-2);
  row-gap: 4px;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.025) inset;
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
}

.studio-tab:hover {
  transform: translateY(-2px);
  border-color: color-mix(in srgb, var(--studio-tab-accent) 68%, transparent);
  box-shadow:
    0 12px 28px rgba(0, 0, 0, 0.22),
    0 0 22px color-mix(in srgb, var(--studio-tab-accent) 20%, transparent);
}

.studio-tab--active {
  border-color: color-mix(in srgb, var(--studio-tab-accent) 82%, transparent);
  background:
    linear-gradient(145deg, color-mix(in srgb, var(--studio-tab-accent) 22%, transparent), rgba(255, 255, 255, 0.03)),
    rgba(18, 12, 42, 0.66);
  box-shadow:
    0 0 0 1px color-mix(in srgb, var(--studio-tab-accent) 28%, transparent) inset,
    0 0 26px color-mix(in srgb, var(--studio-tab-accent) 20%, transparent);
}

.studio-tab__icon {
  width: 30px;
  height: 30px;
  border-radius: 999px;
  color: var(--studio-tab-accent);
  background: color-mix(in srgb, var(--studio-tab-accent) 14%, transparent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  line-height: 1;
}

.studio-tab__label {
  font-weight: 650;
  overflow-wrap: anywhere;
}

.studio-tab small {
  grid-column: 2;
  color: var(--color-text-secondary);
  line-height: 1.4;
  overflow-wrap: anywhere;
}

@media (prefers-reduced-motion: reduce) {
  .studio-tab,
  .studio-tab:hover {
    transform: none;
    transition: none;
  }
}

@media (max-width: 820px) {
  .studio-shell__inner {
    padding-inline: var(--space-3);
  }

  .studio-shell__header {
    grid-template-columns: 1fr;
    align-items: stretch;
  }

  .studio-shell__copy h1 {
    font-size: 38px;
  }

  .studio-tabs {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 480px) {
  .studio-tabs {
    grid-template-columns: 1fr;
  }
}
</style>
