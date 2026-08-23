<script setup lang="ts">
/**
 * 「創作區怎麼玩」導覽（BD12）。
 *
 * 純靜態內容，零 API、零狀態：四個功能各一段白話介紹、一張把它們串起來
 * 的轉化鏈，以及「哪些按鈕會扣點」。之所以做成 modal 而不是第五個分頁，
 * 是因為它不需要路由、不需要在分頁列佔一格（那四格是功能，不是說明），
 * 維護成本就只剩下文案本身。
 *
 * 紅線：這裡每一句都對應真的存在的按鈕。轉化鏈的每一步都能在
 * `FusionStoryExitHub` / `DramaExitHub` / `StudioAuthoringPage` /
 * `StudioCardsPage` 找到對應出口；扣點那段對應
 * `contracts/action-catalog.json` 裡真的會計費的 action。圖集那條分岔講
 * 的是結局畫面上的「看場景圖集」（`DramaExitHub` 的第四個出口）——它按下
 * 去會回到 `BranchingDramaPage` 展開既有的圖集面板，兩處是同一個入口。
 *
 * 版面照 `PlayerPersonaNoteModal` 的既有 overlay 先例（Teleport +
 * backdrop + role="dialog" + Esc 關閉），沒有另開一套樣式系統。
 */
import { computed, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CloseOutlined } from '@ant-design/icons-vue'

import { UiButton } from '@/components/ui'
import { useAuth } from '@/composables/useAuth'

const props = defineProps<{ visible: boolean }>()

const emit = defineEmits<{ close: [] }>()

/**
 * Teleport needs a live `body` to aim at; there is none while rendering on
 * the server (or in the SSR-based component tests), where the dialog then
 * silently renders into nothing. Falling back to rendering in place keeps
 * the markup real in both worlds.
 */
const canTeleport = typeof document !== 'undefined'

const { t } = useI18n()
const { cloudMode } = useAuth()

interface GuideFeature {
  key: string
  icon: string
  accent: string
  title: string
  body: string
}

interface GuideStep {
  key: string
  title: string
  body: string
  forks: string[]
}

/**
 * 場景圖是選配能力：hosted 站一定畫得出來，自架站沒接圖片後端就永遠沒有
 * 圖——而前端看不到那份配置。所以「會畫成圖」這類承諾不能無條件寫死，
 * 有 `SelfHost` 變體的句子一律走這裡挑鍵（cloud 用原句，非 cloud 用把
 * 圖片講成「站點有啟用的話」的條件句）。
 */
function siteAware(key: string): string {
  return t(cloudMode.value ? key : `${key}SelfHost`)
}

const features = computed<GuideFeature[]>(() => [
  {
    key: 'authoring',
    icon: '✦',
    accent: 'var(--color-primary)',
    title: t('studio.tabs.authoring'),
    body: t('studio.guide.features.authoring'),
  },
  {
    key: 'fusion',
    icon: '◇',
    accent: 'var(--color-primary-light)',
    title: t('studio.tabs.fusion'),
    body: t('studio.guide.features.fusion'),
  },
  {
    key: 'branching',
    icon: '◈',
    accent: 'var(--color-secondary)',
    title: t('studio.tabs.branching'),
    body: siteAware('studio.guide.features.branching'),
  },
  {
    key: 'cards',
    icon: '✧',
    accent: 'var(--color-spark)',
    title: t('studio.tabs.cards'),
    body: t('studio.guide.features.cards'),
  },
])

/**
 * 轉化鏈的順序與分岔在這裡定死，而不是從 i18n 物件的鍵序推出來——
 * catalogue 只准放字串葉子（陣列會被 check:i18n 擋下），而物件的鍵序不是
 * 可靠的順序來源。三語的鍵集合一致由 catalogue 檢查保證。
 */
interface ChainFork {
  id: string
  /** Copy that promises scene art, so it needs a `SelfHost` variant. */
  siteAware?: true
}

const CHAIN_STEPS: ReadonlyArray<{ key: string, forks: readonly ChainFork[] }> = [
  { key: 'chat', forks: [] },
  { key: 'fusion', forks: [] },
  {
    key: 'fusionExits',
    forks: [
      { id: 'continueStory' },
      { id: 'perform' },
      { id: 'branch' },
      { id: 'takeAway' },
    ],
  },
  { key: 'arc', forks: [] },
  { key: 'drama', forks: [] },
  {
    key: 'dramaExits',
    forks: [
      { id: 'replay' },
      { id: 'adapt' },
      { id: 'transcript' },
      { id: 'gallery', siteAware: true },
    ],
  },
  { key: 'card', forks: [] },
]

const chainSteps = computed<GuideStep[]>(() => CHAIN_STEPS.map(step => ({
  key: step.key,
  title: t(`studio.guide.chain.steps.${step.key}.title`),
  body: t(`studio.guide.chain.steps.${step.key}.body`),
  forks: step.forks.map((fork) => {
    const key = `studio.guide.chain.steps.${step.key}.forks.${fork.id}`
    return fork.siteAware ? siteAware(key) : t(key)
  }),
})))

watch(() => props.visible, (visible) => {
  if (visible) bindEscape()
  else unbindEscape()
}, { immediate: true })

onBeforeUnmount(unbindEscape)

function bindEscape() {
  if (typeof window === 'undefined') return
  window.addEventListener('keydown', handleWindowKeydown)
}

function unbindEscape() {
  if (typeof window === 'undefined') return
  window.removeEventListener('keydown', handleWindowKeydown)
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (!props.visible) return
  if (event.key === 'Escape') emit('close')
}
</script>

<template>
  <Teleport to="body" :disabled="!canTeleport">
    <div
      v-if="visible"
      class="studio-guide__backdrop"
      @click.self="emit('close')"
    >
      <section
        class="studio-guide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="studio-guide-title"
      >
        <header class="studio-guide__header">
          <div class="studio-guide__heading">
            <p class="spark-label">{{ t('studio.eyebrow') }}</p>
            <h2 id="studio-guide-title" class="display-title display-title--gradient">
              {{ t('studio.guide.title') }}
            </h2>
          </div>
          <UiButton
            variant="ghost"
            size="sm"
            :aria-label="t('common.actions.close')"
            @click="emit('close')"
          >
            <CloseOutlined aria-hidden="true" />
          </UiButton>
        </header>

        <div class="studio-guide__body">
          <p class="studio-guide__intro">{{ t('studio.guide.intro') }}</p>

          <section class="studio-guide__section">
            <h3>{{ t('studio.guide.featuresTitle') }}</h3>
            <ul class="studio-guide__features">
              <li
                v-for="feature in features"
                :key="feature.key"
                class="guide-feature"
                :style="{ '--guide-feature-accent': feature.accent }"
              >
                <span class="guide-feature__icon" aria-hidden="true">{{ feature.icon }}</span>
                <div class="guide-feature__copy">
                  <h4>{{ feature.title }}</h4>
                  <p>{{ feature.body }}</p>
                </div>
              </li>
            </ul>
          </section>

          <section class="studio-guide__section">
            <h3>{{ t('studio.guide.chain.title') }}</h3>
            <p class="studio-guide__lead">{{ t('studio.guide.chain.lead') }}</p>
            <ol class="guide-chain" :aria-label="t('studio.guide.chain.ariaLabel')">
              <li v-for="step in chainSteps" :key="step.key" class="guide-chain__step">
                <span class="guide-chain__node" aria-hidden="true" />
                <div class="guide-chain__copy">
                  <h4>{{ step.title }}</h4>
                  <p>{{ step.body }}</p>
                  <ul v-if="step.forks.length" class="guide-chain__forks">
                    <li v-for="(fork, index) in step.forks" :key="index">{{ fork }}</li>
                  </ul>
                </div>
              </li>
            </ol>
          </section>

          <section class="studio-guide__section">
            <h3>{{ t('studio.guide.credits.title') }}</h3>
            <template v-if="cloudMode">
              <p>{{ t('studio.guide.credits.charged') }}</p>
              <p>{{ t('studio.guide.credits.priced') }}</p>
              <p>{{ t('studio.guide.credits.free') }}</p>
            </template>
            <p v-else>{{ t('studio.guide.credits.selfHost') }}</p>
          </section>
        </div>

        <footer class="studio-guide__footer">
          <UiButton variant="primary" size="sm" @click="emit('close')">
            {{ t('studio.guide.close') }}
          </UiButton>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.studio-guide__backdrop {
  position: fixed;
  inset: 0;
  height: 100dvh;
  z-index: 920;
  display: flex;
  align-items: center;
  justify-content: center;
  padding:
    max(24px, var(--safe-area-top))
    max(16px, var(--safe-area-right))
    max(24px, var(--safe-area-bottom))
    max(16px, var(--safe-area-left));
  background: rgba(0, 0, 0, 0.62);
  backdrop-filter: blur(5px);
}

.studio-guide {
  width: min(720px, calc(100vw - 32px));
  max-height: min(92dvh, 860px);
  display: flex;
  flex-direction: column;
  border-radius: 12px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.28);
  background:
    radial-gradient(620px 260px at 20% -60px, rgba(var(--color-primary-rgb), 0.22), transparent 70%),
    rgba(14, 10, 32, 0.96);
  color: var(--color-text);
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.46);
}

.studio-guide__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-4) var(--space-2);
}

.studio-guide__heading {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.studio-guide__heading h2 {
  margin: 0;
  font-size: 28px;
  letter-spacing: 0;
}

.studio-guide__body {
  min-height: 0;
  overflow-y: auto;
  padding: 0 var(--space-4) var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.studio-guide__intro,
.studio-guide__lead,
.studio-guide__section p {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.7;
}

.studio-guide__section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.studio-guide__section h3 {
  margin: 0;
  font-size: var(--font-lg);
  font-weight: 650;
  letter-spacing: 0;
}

.studio-guide__features {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-2);
}

.guide-feature {
  --guide-feature-accent: var(--color-primary);
  min-width: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid color-mix(in srgb, var(--guide-feature-accent) 30%, transparent);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
}

.guide-feature__icon {
  width: 28px;
  height: 28px;
  border-radius: 999px;
  color: var(--guide-feature-accent);
  background: color-mix(in srgb, var(--guide-feature-accent) 14%, transparent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  line-height: 1;
}

.guide-feature__copy {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.guide-feature__copy h4,
.guide-chain__copy h4 {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 650;
  overflow-wrap: anywhere;
}

.guide-feature__copy p,
.guide-chain__copy p {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.7;
}

.guide-chain {
  position: relative;
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: var(--space-3);
}

/* 直的軌道線：串起每一顆節點，讓「一步接一步」不是靠文字說出來的。
   收在最後一顆節點的圓心，不會拖出一截多餘的尾巴。 */
.guide-chain::before {
  content: "";
  position: absolute;
  left: 7px;
  top: 10px;
  bottom: 10px;
  width: 2px;
  background: linear-gradient(
    rgba(var(--color-spark-rgb), 0.6),
    rgba(var(--color-primary-rgb), 0.5),
    rgba(var(--color-secondary-rgb), 0.35)
  );
}

.guide-chain__step {
  position: relative;
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr);
  align-items: start;
  gap: var(--space-3);
}

.guide-chain__node {
  position: relative;
  z-index: 1;
  width: 16px;
  height: 16px;
  margin-top: 3px;
  border-radius: 999px;
  border: 2px solid rgba(var(--color-spark-rgb), 0.72);
  background: rgba(14, 10, 32, 1);
  box-shadow: 0 0 14px rgba(var(--color-spark-rgb), 0.28);
}

.guide-chain__copy {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.guide-chain__forks {
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 6px;
}

/* 分岔用左側短線標出「從這裡岔開」，不另外畫圖。 */
.guide-chain__forks li {
  position: relative;
  padding: 6px 10px 6px 22px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.guide-chain__forks li::before {
  content: "";
  position: absolute;
  left: 8px;
  top: 50%;
  width: 8px;
  height: 2px;
  border-radius: 2px;
  background: rgba(var(--color-spark-rgb), 0.6);
}

.studio-guide__footer {
  display: flex;
  justify-content: flex-end;
  padding: var(--space-2) var(--space-4) var(--space-4);
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}

@media (max-width: 640px) {
  .studio-guide__features {
    grid-template-columns: 1fr;
  }

  .studio-guide__heading h2 {
    font-size: 24px;
  }

  .studio-guide__header,
  .studio-guide__body,
  .studio-guide__footer {
    padding-inline: var(--space-3);
  }
}
</style>
