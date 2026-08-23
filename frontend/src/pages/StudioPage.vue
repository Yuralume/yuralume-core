<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

import { UiButton } from '@/components/ui'
import StudioGuideModal from '@/components/studio/StudioGuideModal.vue'
import StudioTabCard from '@/components/studio/StudioTabCard.vue'
import { useAuth } from '@/composables/useAuth'
import {
  isStudioGuideCoachmarkDismissed,
  rememberStudioGuideCoachmarkDismissed,
} from '@/utils/arcDiscovery'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const { cloudMode } = useAuth()

/**
 * 場景圖是選配能力，自架站沒接圖片後端就永遠沒有圖（而前端看不到那份
 * 配置），所以承諾「會畫成圖」的句子分兩版——與 `StudioGuideModal` 的
 * `siteAware()` 同一條規則。
 */
function siteAwareCopy(key: string): string {
  return t(cloudMode.value ? key : `${key}SelfHost`)
}

const tabs = computed(() => [
  {
    routeName: 'studio-authoring',
    label: t('studio.tabs.authoring'),
    description: t('studio.tabs.authoringHint'),
    icon: '✦',
    accent: 'var(--color-primary)',
    what: t('studio.tabs.authoringWhat'),
    how: t('studio.tabs.authoringHow'),
    next: t('studio.tabs.authoringNext'),
  },
  {
    routeName: 'studio-fusion-stories',
    label: t('studio.tabs.fusion'),
    description: t('studio.tabs.fusionHint'),
    icon: '◇',
    accent: 'var(--color-primary-light)',
    what: t('studio.tabs.fusionWhat'),
    how: t('studio.tabs.fusionHow'),
    next: t('studio.tabs.fusionNext'),
  },
  {
    routeName: 'studio-branching-dramas',
    label: t('studio.tabs.branching'),
    description: t('studio.tabs.branchingHint'),
    icon: '◈',
    accent: 'var(--color-secondary)',
    what: t('studio.tabs.branchingWhat'),
    how: t('studio.tabs.branchingHow'),
    next: siteAwareCopy('studio.tabs.branchingNext'),
  },
  {
    routeName: 'studio-character-cards',
    label: t('studio.tabs.cards'),
    description: t('studio.tabs.cardsHint'),
    icon: '✧',
    accent: 'var(--color-spark)',
    what: t('studio.tabs.cardsWhat'),
    how: t('studio.tabs.cardsHow'),
    next: t('studio.tabs.cardsNext'),
  },
])

/** Guarded so SSR / privacy-mode never throws at setup. */
function getStudioStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

const guideOpen = ref(false)
const coachmarkDismissed = ref(
  isStudioGuideCoachmarkDismissed(getStudioStorage()),
)
const showCoachmark = computed(() => !coachmarkDismissed.value)

function dismissCoachmark() {
  rememberStudioGuideCoachmarkDismissed(getStudioStorage())
  coachmarkDismissed.value = true
}

/**
 * 開導覽也算「看過提示」：提示唯一的目的就是把人送到這裡，送到了就不必
 * 再問第二次。所以入口按鈕與提示本身共用同一次熄燈。
 */
function openGuide() {
  dismissCoachmark()
  guideOpen.value = true
}
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
        <UiButton
          class="studio-shell__guide glass-panel"
          variant="ghost"
          size="sm"
          @click="openGuide"
        >
          {{ t('studio.guide.openLabel') }}
        </UiButton>
      </header>

      <div v-if="showCoachmark" class="studio-coachmark" role="note">
        <span class="studio-coachmark__body">{{ t('studio.guide.coachmark') }}</span>
        <UiButton variant="chip" size="sm" @click="openGuide">
          {{ t('studio.guide.coachmarkAction') }}
        </UiButton>
        <button
          type="button"
          class="studio-coachmark__close"
          :aria-label="t('studio.guide.coachmarkDismiss')"
          @click="dismissCoachmark"
        >
          ×
        </button>
      </div>

      <nav class="studio-tabs" :aria-label="t('studio.tabs.aria')">
        <StudioTabCard
          v-for="tab in tabs"
          :key="tab.routeName"
          :route-name="tab.routeName"
          :label="tab.label"
          :description="tab.description"
          :icon="tab.icon"
          :accent="tab.accent"
          :what="tab.what"
          :how="tab.how"
          :next="tab.next"
          :active="route.name === tab.routeName"
        />
      </nav>

      <RouterView />
    </div>

    <StudioGuideModal :visible="guideOpen" @close="guideOpen = false" />
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
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--space-3);
}

.studio-shell__back,
.studio-shell__guide {
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

.studio-coachmark {
  position: relative;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: 10px 38px 10px var(--space-3);
  border-radius: 8px;
  border: 1px solid rgba(var(--color-spark-rgb), 0.32);
  background: rgba(13, 23, 34, 0.9);
}

.studio-coachmark__body {
  min-width: 0;
  flex: 1 1 240px;
  font-size: var(--font-md);
  line-height: 1.6;
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}

.studio-coachmark__close {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: rgba(255, 255, 255, 0.72);
  font: inherit;
  font-size: 17px;
  line-height: 24px;
  text-align: center;
  cursor: pointer;
}

.studio-coachmark__close:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}

.studio-tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-2);
  align-items: start;
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
