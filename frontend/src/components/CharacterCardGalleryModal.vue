<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CloseOutlined, LeftOutlined, RightOutlined } from '@ant-design/icons-vue'
import CharacterCardFace from '@/components/CharacterCardFace.vue'
import CharacterCardThumb from '@/components/CharacterCardThumb.vue'
import { UiButton } from '@/components/ui'
import type { CharacterCardPreview } from '@/utils/api/characters'

const props = withDefaults(
  defineProps<{
    visible: boolean
    mode: 'browse' | 'preview'
    cards: CharacterCardPreview[]
    activeIndex?: number
    loading?: boolean
    error?: string | null
    actionLoading?: boolean
    translateEnabled?: boolean
    translateLoading?: boolean
    translateError?: string | null
  }>(),
  {
    activeIndex: 0,
    loading: false,
    error: null,
    actionLoading: false,
    translateEnabled: false,
    translateLoading: false,
    translateError: null,
  },
)

const emit = defineEmits<{
  close: []
  change: [index: number]
  confirm: [card: CharacterCardPreview]
  'translate-change': [enabled: boolean]
}>()

const { t } = useI18n()

const slideDirection = ref<'prev' | 'next'>('next')

const activeCard = computed(() => props.cards[props.activeIndex] ?? null)
// SillyTavern-only notice: shown in preview mode when the upload was a
// converted ST card, so the importer knows the persona was AI-normalised
// and which fields did not cross over (D7).
const isSillyTavernCard = computed(() => (
  props.mode === 'preview' && activeCard.value?.source_format === 'sillytavern'
))
const droppedFields = computed(() => activeCard.value?.dropped_fields ?? [])
const canNavigate = computed(() => props.mode === 'browse' && props.cards.length > 1)
const prevIndex = computed(() => (
  props.activeIndex === 0 ? props.cards.length - 1 : props.activeIndex - 1
))
const nextIndex = computed(() => (
  props.activeIndex >= props.cards.length - 1 ? 0 : props.activeIndex + 1
))
const prevCard = computed(() => (canNavigate.value ? props.cards[prevIndex.value] ?? null : null))
const nextCard = computed(() => (canNavigate.value ? props.cards[nextIndex.value] ?? null : null))
const slideName = computed(() => `character-card-slide-${slideDirection.value}`)
const titleKey = computed(() => (
  props.mode === 'browse'
    ? 'playerSidebar.characterCards.gallery.title'
    : 'playerSidebar.characterCards.preview.title'
))
const actionLabel = computed(() => (
  props.mode === 'browse'
    ? t('playerSidebar.characterCards.installAction')
    : t('playerSidebar.characterCards.preview.confirmImport')
))
const canShowTranslate = computed(() => (
  !props.loading && !props.error && props.cards.length > 0
))

watch(() => props.visible, (visible) => {
  if (visible) {
    window.addEventListener('keydown', handleWindowKeydown)
  } else {
    window.removeEventListener('keydown', handleWindowKeydown)
  }
}, { immediate: true })

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleWindowKeydown)
})

function previous() {
  if (!canNavigate.value) return
  slideDirection.value = 'prev'
  emit('change', prevIndex.value)
}

function next() {
  if (!canNavigate.value) return
  slideDirection.value = 'next'
  emit('change', nextIndex.value)
}

function confirmActive() {
  if (!activeCard.value) return
  emit('confirm', activeCard.value)
}

function handleTranslateChange(event: Event) {
  const input = event.target as HTMLInputElement
  emit('translate-change', input.checked)
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (!props.visible) return
  if (event.key === 'Escape') {
    emit('close')
    return
  }
  if (event.key === 'ArrowLeft') {
    previous()
    return
  }
  if (event.key === 'ArrowRight') {
    next()
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="character-card-gallery__backdrop"
      @click.self="emit('close')"
    >
      <section
        class="character-card-gallery"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="`character-card-gallery-title-${mode}`"
      >
        <header class="character-card-gallery__header">
          <div class="character-card-gallery__header-main">
            <div class="character-card-gallery__copy">
              <h3
                :id="`character-card-gallery-title-${mode}`"
                class="character-card-gallery__title"
              >
                {{ t(titleKey) }}
              </h3>
              <p class="character-card-gallery__hint">
                {{ mode === 'browse'
                  ? t('playerSidebar.characterCards.gallery.hint')
                  : t('playerSidebar.characterCards.preview.hint') }}
              </p>
            </div>
            <UiButton
              variant="ghost"
              size="sm"
              class="character-card-gallery__close"
              :aria-label="t('common.actions.close')"
              @click="emit('close')"
            >
              <CloseOutlined aria-hidden="true" />
            </UiButton>
          </div>

        <div
          v-if="canShowTranslate"
          class="character-card-gallery__translate"
        >
          <label class="character-card-gallery__translate-label field-label">
            <input
              type="checkbox"
              class="field-checkbox"
              :checked="translateEnabled"
              :disabled="translateLoading || actionLoading"
              @change="handleTranslateChange"
            />
            <span>{{ t('playerSidebar.characterCards.translate.label') }}</span>
          </label>
          <span
            v-if="translateLoading"
            class="character-card-gallery__translate-state"
            role="status"
            aria-live="polite"
          >
            {{ t('playerSidebar.characterCards.translate.loading') }}
          </span>
          <span v-else-if="translateError" class="character-card-gallery__translate-error">
            {{ translateError }}
          </span>
        </div>
        </header>

        <p v-if="loading" class="character-card-gallery__state">
          {{ t('common.state.loading') }}
        </p>
        <p v-else-if="error" class="character-card-gallery__error">
          {{ error }}
        </p>
        <p v-else-if="cards.length === 0" class="character-card-gallery__state">
          {{ t('playerSidebar.characterCards.emptyPacks') }}
        </p>

        <div
          v-if="!loading && !error && cards.length > 0"
          class="character-card-gallery__stage"
          :class="{ 'character-card-gallery__stage--single': !canNavigate }"
        >
          <div
            v-if="canNavigate"
            class="character-card-gallery__side character-card-gallery__side--prev"
          >
            <CharacterCardThumb
              v-if="prevCard"
              :card="prevCard"
              side="prev"
              :nav-label="t('playerSidebar.characterCards.gallery.previous')"
              @select="previous"
            />
            <UiButton
              variant="ghost"
              size="sm"
              class="character-card-gallery__nav"
              :aria-label="t('playerSidebar.characterCards.gallery.previous')"
              @click="previous"
            >
              <LeftOutlined aria-hidden="true" />
            </UiButton>
          </div>

          <Transition :name="slideName" mode="out-in">
            <CharacterCardFace
              v-if="activeCard"
              :key="activeIndex"
              :card="activeCard"
              :action-label="actionLabel"
              :action-loading="actionLoading"
              :action-disabled="actionLoading"
              @action="confirmActive"
            />
          </Transition>

          <div
            v-if="canNavigate"
            class="character-card-gallery__side character-card-gallery__side--next"
          >
            <CharacterCardThumb
              v-if="nextCard"
              :card="nextCard"
              side="next"
              :nav-label="t('playerSidebar.characterCards.gallery.next')"
              @select="next"
            />
            <UiButton
              variant="ghost"
              size="sm"
              class="character-card-gallery__nav"
              :aria-label="t('playerSidebar.characterCards.gallery.next')"
              @click="next"
            >
              <RightOutlined aria-hidden="true" />
            </UiButton>
          </div>
        </div>

        <div
          v-if="isSillyTavernCard"
          class="character-card-gallery__st-notice"
          role="note"
        >
          <p class="character-card-gallery__st-title">
            {{ t('playerSidebar.characterCards.sillytavern.title') }}
          </p>
          <p class="character-card-gallery__st-line">
            {{ t('playerSidebar.characterCards.sillytavern.normalized') }}
          </p>
          <p
            v-if="droppedFields.length"
            class="character-card-gallery__st-line"
          >
            {{ t('playerSidebar.characterCards.sillytavern.droppedIntro') }}
            <span
              v-for="(dropped, index) in droppedFields"
              :key="dropped"
            >{{ index > 0 ? t('common.listSeparator') : '' }}{{ t(`playerSidebar.characterCards.sillytavern.dropped.${dropped}`) }}</span>
          </p>
        </div>

        <footer v-if="mode === 'browse' && cards.length > 1" class="character-card-gallery__footer">
          {{ t('playerSidebar.characterCards.gallery.page', {
            current: activeIndex + 1,
            total: cards.length,
          }) }}
        </footer>

        <footer v-else-if="mode === 'preview'" class="character-card-gallery__footer">
          <UiButton
            variant="secondary"
            size="sm"
            :disabled="actionLoading"
            @click="emit('close')"
          >
            {{ t('common.actions.cancel') }}
          </UiButton>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.character-card-gallery__backdrop {
  position: fixed;
  inset: 0;
  /* 用動態視窗高：排除手機瀏覽器網址列佔的高度，modal 才不會頂進被網址列遮住的區域。
     不支援 dvh 的舊瀏覽器會忽略本行，回退到上面的 inset:0（layout viewport）。 */
  height: 100dvh;
  z-index: 910;
  display: flex;
  align-items: center;
  justify-content: center;
  /* 避讓瀏海 / 圓角 / home indicator，避免 modal 邊緣（含關閉鈕）被裁掉。 */
  padding:
    max(24px, var(--safe-area-top))
    max(24px, var(--safe-area-right))
    max(24px, var(--safe-area-bottom))
    max(24px, var(--safe-area-left));
  background: rgba(0, 0, 0, 0.62);
  backdrop-filter: blur(5px);
  -webkit-backdrop-filter: blur(5px);
}

.character-card-gallery {
  width: min(760px, calc(100vw - 32px));
  max-height: min(92vh, 820px);
  max-height: min(92dvh, 820px);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  border: 1px solid rgba(64, 156, 255, 0.34);
  border-radius: 8px;
  padding: var(--space-4);
  background: rgba(24, 33, 50, 0.98);
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.46);
  overflow-y: auto;
}

/* 黏頂標題列：內容捲動時關閉鈕永遠在視窗內可點。負邊距把列拉到容器內距邊緣，
   自身 padding 補回，視覺與原本一致。 */
.character-card-gallery__header {
  position: sticky;
  top: 0;
  z-index: 3;
  margin: calc(-1 * var(--space-4)) calc(-1 * var(--space-4)) 0;
  padding: var(--space-4) var(--space-4) var(--space-3);
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-3);
  background: rgba(24, 33, 50, 0.98);
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
}

.character-card-gallery__header-main {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.character-card-gallery__copy {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.character-card-gallery__title {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-xl);
  line-height: 1.35;
  font-weight: 650;
}

.character-card-gallery__hint,
.character-card-gallery__state,
.character-card-gallery__error,
.character-card-gallery__footer {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  line-height: 1.6;
}

.character-card-gallery__error {
  color: #f4a3a3;
}

.character-card-gallery__translate {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
}

.character-card-gallery__translate-label {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  cursor: pointer;
}

.character-card-gallery__translate-state,
.character-card-gallery__translate-error {
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.5;
}

.character-card-gallery__translate-error {
  color: #f4a3a3;
}

.character-card-gallery__close {
  flex-shrink: 0;
}

.character-card-gallery__stage {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 320px) minmax(0, 1fr);
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
}

.character-card-gallery__stage--single {
  grid-template-columns: minmax(0, 320px);
}

/* 左右側欄：桌機顯示上 / 下一張迷你卡，窄螢幕退回箭頭按鈕。 */
.character-card-gallery__side {
  display: flex;
  align-items: center;
  min-width: 0;
}

.character-card-gallery__side--prev {
  justify-content: flex-end;
}

.character-card-gallery__side--next {
  justify-content: flex-start;
}

/* 卡片後方的柔和聚光，讓卡牌像被打光的展示品。 */
.character-card-gallery__stage::before {
  content: "";
  position: absolute;
  inset: -6% 10%;
  z-index: 0;
  background: radial-gradient(
    60% 60% at 50% 42%,
    rgba(139, 109, 255, 0.28),
    rgba(255, 209, 128, 0.1) 48%,
    transparent 72%
  );
  filter: blur(8px);
  pointer-events: none;
}

.character-card-gallery__stage > * {
  position: relative;
  z-index: 1;
}

.character-card-gallery__stage > .character-card-face {
  justify-self: center;
}

.character-card-gallery__nav {
  /* 桌機用迷你卡導覽，箭頭只在窄螢幕（無空間放迷你卡）出現。 */
  display: none;
  width: 40px;
  height: 40px;
  padding: 0;
  border-radius: 50%;
  border: 1px solid rgba(255, 255, 255, 0.16);
  background: rgba(255, 255, 255, 0.05);
  backdrop-filter: blur(2px);
  transition: background 0.18s ease, border-color 0.18s ease, transform 0.18s ease;
}

.character-card-gallery__nav:hover {
  background: rgba(255, 209, 128, 0.16);
  border-color: rgba(255, 209, 128, 0.5);
  transform: scale(1.08);
}

.character-card-gallery__footer {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
}

.character-card-gallery__st-notice {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3);
  border: 1px solid rgba(147, 197, 253, 0.28);
  border-radius: 8px;
  background: rgba(37, 99, 235, 0.1);
}

.character-card-gallery__st-title {
  margin: 0;
  color: #bfdbfe;
  font-size: var(--font-sm);
  font-weight: 600;
}

.character-card-gallery__st-line {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
}

/* 主卡切換動畫：依方向左右滑入 / 滑出。out-in 避免不同高度卡片重疊。 */
.character-card-slide-next-enter-active,
.character-card-slide-next-leave-active,
.character-card-slide-prev-enter-active,
.character-card-slide-prev-leave-active {
  transition: transform 0.24s cubic-bezier(0.22, 0.61, 0.36, 1), opacity 0.24s ease;
}

.character-card-slide-next-enter-from {
  opacity: 0;
  transform: translateX(38px) scale(0.96);
}

.character-card-slide-next-leave-to {
  opacity: 0;
  transform: translateX(-38px) scale(0.96);
}

.character-card-slide-prev-enter-from {
  opacity: 0;
  transform: translateX(-38px) scale(0.96);
}

.character-card-slide-prev-leave-to {
  opacity: 0;
  transform: translateX(38px) scale(0.96);
}

/* 迷你卡放不下的窄螢幕：隱藏 peek，改回箭頭按鈕。 */
@media (max-width: 600px) {
  .character-card-gallery__stage {
    grid-template-columns: 38px minmax(0, 1fr) 38px;
    gap: var(--space-2);
  }

  .character-card-gallery__stage--single {
    grid-template-columns: minmax(0, min(320px, 100%));
  }

  .character-card-gallery__side {
    justify-content: center;
  }

  .character-card-thumb {
    display: none;
  }

  .character-card-gallery__nav {
    display: inline-flex;
  }
}

@media (prefers-reduced-motion: reduce) {
  .character-card-slide-next-enter-active,
  .character-card-slide-next-leave-active,
  .character-card-slide-prev-enter-active,
  .character-card-slide-prev-leave-active {
    transition: opacity 0.18s ease;
  }

  .character-card-slide-next-enter-from,
  .character-card-slide-next-leave-to,
  .character-card-slide-prev-enter-from,
  .character-card-slide-prev-leave-to {
    transform: none;
  }
}

@media (max-width: 560px) {
  .character-card-gallery__backdrop {
    align-items: flex-end;
    padding:
      max(12px, var(--safe-area-top))
      max(12px, var(--safe-area-right))
      max(12px, var(--safe-area-bottom))
      max(12px, var(--safe-area-left));
  }

  .character-card-gallery {
    width: 100%;
    max-height: 92vh;
    max-height: 92dvh;
    padding: var(--space-3);
  }

  /* 容器內距變小，黏頂列負邊距同步縮，避免頂端露出空隙。 */
  .character-card-gallery__header {
    margin: calc(-1 * var(--space-3)) calc(-1 * var(--space-3)) 0;
    padding: var(--space-3) var(--space-3) var(--space-2);
  }

  .character-card-gallery__stage {
    grid-template-columns: 34px minmax(0, 1fr) 34px;
  }

  .character-card-gallery__nav {
    width: 32px;
    height: 32px;
  }
}
</style>
