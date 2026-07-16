<script setup lang="ts">
/**
 * 回憶錄 overlay — 浮在主舞台上，不離開 stage。
 *
 * 對照 KokoroGramOverlay 的設計：fixed 全屏 backdrop + 中央 frame +
 * ESC / × / backdrop click 關閉 + body scroll lock。差別是 LumeGram
 * 是窄條（max-width 480px）的 feed，回憶錄是寬版的三欄沉浸式 layout，
 * 所以 frame 寬度放到 1200px，垂直滾動由內部 content 處理。
 */
import { onBeforeUnmount, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import MemoirContent from '@/components/memoir/MemoirContent.vue'
import type { Character } from '@/types/character'

const props = defineProps<{
  open: boolean
  character: Character | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const { t } = useI18n()

function handleKey(ev: KeyboardEvent) {
  if (!props.open) return
  if (ev.key === 'Escape') emit('close')
}

function applyBodyLock(locked: boolean) {
  if (typeof document === 'undefined') return
  document.body.style.overflow = locked ? 'hidden' : ''
}

watch(() => props.open, (next) => {
  applyBodyLock(next)
})

onMounted(() => {
  window.addEventListener('keydown', handleKey)
  if (props.open) applyBodyLock(true)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKey)
  applyBodyLock(false)
})
</script>

<template>
  <Transition name="mo-fade">
    <div
      v-if="open"
      class="mo-overlay"
      role="dialog"
      aria-modal="true"
      :aria-label="t('memoir.title')"
      @click.self="emit('close')"
    >
      <div class="mo-frame">
        <header class="mo-header">
          <div class="mo-title-block">
            <span class="mo-icon" aria-hidden="true">📖</span>
            <h2 class="mo-title">{{ t('memoir.title') }}</h2>
            <div v-if="character" class="mo-character">
              <span
                v-if="character.image_urls?.[0]"
                class="mo-avatar"
                :style="{ backgroundImage: `url(${character.image_urls[0]})` }"
                aria-hidden="true"
              />
              <span class="mo-character-name">{{ character.name }}</span>
            </div>
          </div>
          <button
            type="button"
            class="mo-close"
            :aria-label="t('memoir.overlay.close')"
            @click="emit('close')"
          >×</button>
        </header>
        <div class="mo-body">
          <MemoirContent
            v-if="character"
            :key="character.id"
            :character-id="character.id"
          />
          <p v-else class="mo-empty">
            {{ t('memoir.noCharacter') }}
          </p>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.mo-overlay {
  position: fixed;
  inset: 0;
  z-index: 400;
  /* Heavy backdrop so the stage behind never bleeds through, even on
   * browsers / screenshot pipelines that drop backdrop-filter. */
  background: rgba(8, 6, 22, 0.92);
  backdrop-filter: blur(10px) saturate(1.1);
  -webkit-backdrop-filter: blur(10px) saturate(1.1);
  display: flex;
  align-items: stretch;
  justify-content: center;
  padding: 0;
}
.mo-frame {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 1280px;
  height: 100%;
  /* Solid base so the stage behind never bleeds through the page; the
   * star-field artwork lives on a child layer so we can scale / tile it
   * without affecting border / shadow geometry. */
  position: relative;
  background-color: #0c0820;
  border-left: 1px solid var(--color-border);
  border-right: 1px solid var(--color-border);
  box-shadow: 0 0 40px rgba(0, 0, 0, 0.55);
  overflow: hidden;
}
/* bg_mem 星空圖：cover 模式上下擠滿 frame；上方加一層淡紫色 ambient
 * 讓內容跟背景有對比，仍能看見星點與紫色霧氣。 */
.mo-frame::before {
  content: '';
  position: absolute;
  inset: 0;
  background-image:
    radial-gradient(circle at 50% 0%, rgba(139, 92, 246, 0.18), transparent 60%),
    url('/memoir/bg_mem.png');
  background-size: cover, cover;
  background-position: center, center;
  background-repeat: no-repeat, no-repeat;
  opacity: 0.85;
  pointer-events: none;
  z-index: 0;
}
.mo-frame > * {
  position: relative;
  z-index: 1;
}
.mo-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  padding-top: calc(var(--space-3) + var(--safe-area-top, 0px));
  border-bottom: 1px solid var(--color-border);
  background: linear-gradient(
    90deg,
    rgba(139, 92, 246, 0.12),
    rgba(91, 157, 255, 0.08)
  );
  flex-shrink: 0;
}
.mo-title-block {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex: 1;
  min-width: 0;
}
.mo-icon {
  font-size: 22px;
}
.mo-title {
  margin: 0;
  font-size: var(--font-lg, 18px);
  font-weight: 600;
  color: var(--color-text);
}
.mo-character {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding-left: var(--space-3);
  border-left: 1px solid var(--color-border);
}
.mo-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background-size: cover;
  background-position: center;
  border: 2px solid rgba(139, 92, 246, 0.45);
}
.mo-character-name {
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
.mo-close {
  appearance: none;
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 50%;
  width: 36px;
  height: 36px;
  color: var(--color-text-secondary);
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
  flex-shrink: 0;
}
.mo-close:hover {
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text);
  border-color: rgba(139, 92, 246, 0.5);
}
.mo-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5);
}
.mo-empty {
  margin: 0;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
}

.mo-fade-enter-active,
.mo-fade-leave-active {
  transition: opacity 0.18s ease;
}
.mo-fade-enter-from,
.mo-fade-leave-to {
  opacity: 0;
}
</style>
