<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'

const { t } = useI18n()

const props = defineProps<{
  character: Character | null
}>()

const ROTATE_INTERVAL_MS = 8000

const activeIndex = ref(0)
let rotateTimer: ReturnType<typeof setInterval> | null = null

const images = computed<string[]>(() => props.character?.image_urls ?? [])

function clearTimer() {
  if (rotateTimer !== null) {
    clearInterval(rotateTimer)
    rotateTimer = null
  }
}

function restartRotation() {
  clearTimer()
  if (images.value.length <= 1) return
  rotateTimer = setInterval(() => {
    activeIndex.value = (activeIndex.value + 1) % images.value.length
  }, ROTATE_INTERVAL_MS)
}

watch(
  () => [props.character?.id, images.value.length] as const,
  () => {
    activeIndex.value = 0
    restartRotation()
  },
  { immediate: true },
)

onBeforeUnmount(clearTimer)

function handleDotClick(index: number) {
  activeIndex.value = index
  restartRotation()
}

function step(delta: -1 | 1) {
  const total = images.value.length
  if (total <= 1) return
  activeIndex.value = (activeIndex.value + delta + total) % total
  restartRotation()
}

function goPrev() {
  step(-1)
}

function goNext() {
  step(1)
}

// Touch / pointer swipe — horizontal drag > 40px fires prev/next.
// We use pointer events so the same handler covers mouse-drag on
// desktop as well. Vertical drags are ignored so page scrolling on
// mobile still works normally.
const SWIPE_THRESHOLD_PX = 40
let swipeStartX: number | null = null
let swipeStartY: number | null = null

function handlePointerDown(event: PointerEvent) {
  if (event.pointerType === 'mouse' && event.button !== 0) return
  swipeStartX = event.clientX
  swipeStartY = event.clientY
}

function handlePointerUp(event: PointerEvent) {
  if (swipeStartX === null || swipeStartY === null) return
  const dx = event.clientX - swipeStartX
  const dy = event.clientY - swipeStartY
  swipeStartX = null
  swipeStartY = null
  // Require horizontal intent — ignore near-vertical drags.
  if (Math.abs(dx) < SWIPE_THRESHOLD_PX) return
  if (Math.abs(dy) > Math.abs(dx)) return
  step(dx > 0 ? -1 : 1)
}

function handlePointerCancel() {
  swipeStartX = null
  swipeStartY = null
}
</script>

<template>
  <div
    class="image-stage"
    @pointerdown="handlePointerDown"
    @pointerup="handlePointerUp"
    @pointercancel="handlePointerCancel"
    @pointerleave="handlePointerCancel"
  >
    <div v-if="!character" class="stage-empty">
      <span>{{ t('imageStage.noCharacter') }}</span>
    </div>

    <template v-else>
      <div v-if="images.length === 0" class="stage-empty">
        <span>{{ t('imageStage.noImages', { name: character.name }) }}</span>
        <span class="stage-empty-hint">{{ t('imageStage.placeholderHint') }}</span>
      </div>

      <template v-else>
        <img
          v-for="(url, index) in images"
          :key="url"
          :src="url"
          :alt="character.name"
          :class="['stage-image', { active: index === activeIndex }]"
          draggable="false"
        />
      </template>

      <div class="stage-overlay">
        <div class="char-name">{{ character.name }}</div>
        <div class="char-emotion">{{ character.state.emotion }}</div>
      </div>

      <template v-if="images.length > 1">
        <button
          class="stage-nav stage-nav-prev"
          :aria-label="t('imageStage.prev')"
          @click="goPrev"
        >‹</button>
        <button
          class="stage-nav stage-nav-next"
          :aria-label="t('imageStage.next')"
          @click="goNext"
        >›</button>

        <div class="stage-dots">
          <button
            v-for="(_, index) in images"
            :key="index"
            :class="['stage-dot', { active: index === activeIndex }]"
            :aria-label="t('imageStage.dot', { n: index + 1 })"
            @click="handleDotClick(index)"
          />
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.image-stage {
  width: 100%;
  height: 100%;
  position: relative;
  background: radial-gradient(ellipse at center bottom, #1a2a4a 0%, var(--color-bg) 70%);
  overflow: hidden;
  /* Mouse drag + touch swipe should slide images, not accidentally
     select the overlay text or drag the <img> off as a ghost. */
  user-select: none;
  -webkit-user-select: none;
  touch-action: pan-y;  /* allow vertical page scroll, capture horiz swipes */
}

.stage-image {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
  object-position: center center;
  opacity: 0;
  transition: opacity 1.2s ease-in-out;
  user-select: none;
  pointer-events: none;
  /* Soft feathered edges — radial mask fades the last ~14% of each
     side into transparent so the portrait blends into the stage
     gradient instead of showing a hard rectangular cut-off. The
     horizontal + vertical masks are composited (intersect) so corners
     feather on both axes. ``-webkit-mask-*`` for Safari / iOS. */
  -webkit-mask-image:
    linear-gradient(
      to right,
      transparent 0%,
      #000 14%,
      #000 86%,
      transparent 100%
    ),
    linear-gradient(
      to bottom,
      transparent 0%,
      #000 14%,
      #000 86%,
      transparent 100%
    );
  -webkit-mask-composite: source-in;
  -webkit-mask-size: 100% 100%;
  mask-image:
    linear-gradient(
      to right,
      transparent 0%,
      #000 14%,
      #000 86%,
      transparent 100%
    ),
    linear-gradient(
      to bottom,
      transparent 0%,
      #000 14%,
      #000 86%,
      transparent 100%
    );
  mask-composite: intersect;
  mask-size: 100% 100%;
}

.stage-image.active {
  opacity: 1;
}

.stage-overlay {
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  text-align: center;
  pointer-events: none;
  z-index: 2;
}

.char-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  text-shadow: 0 1px 4px rgba(0, 0, 0, 0.8);
}

.char-emotion {
  font-size: 12px;
  color: var(--color-text-secondary);
  background: rgba(0, 0, 0, 0.5);
  padding: 2px 10px;
  border-radius: 10px;
  margin-top: 4px;
  display: inline-block;
}

.stage-empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
  font-size: 15px;
  gap: 4px;
  text-align: center;
  padding: 16px;
}

.stage-empty-hint {
  font-size: 12px;
  opacity: 0.7;
}

.stage-dots {
  position: absolute;
  top: 12px;
  right: 12px;
  display: flex;
  gap: 4px;
  z-index: 3;
}

.stage-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.3);
  cursor: pointer;
  transition: background 0.2s, transform 0.2s;
}

.stage-dot:hover {
  background: rgba(255, 255, 255, 0.5);
}

.stage-dot.active {
  background: var(--color-primary-light);
  transform: scale(1.2);
}

/* Prev / next arrows — sit vertically centred on each edge. Fade in
   on hover for desktop, stay visible on touch devices so there's
   always an affordance without needing to discover the swipe. */
.stage-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 36px;
  height: 48px;
  border: none;
  background: rgba(0, 0, 0, 0.35);
  color: white;
  font-size: 26px;
  line-height: 1;
  cursor: pointer;
  border-radius: 6px;
  z-index: 3;
  opacity: 0;
  transition: opacity 0.2s, background 0.2s;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 0 4px;  /* pull the › glyph slightly up so it centres */
}

.stage-nav-prev { left: 8px; }
.stage-nav-next { right: 8px; }

.image-stage:hover .stage-nav {
  opacity: 0.85;
}

.stage-nav:hover {
  background: rgba(0, 0, 0, 0.55);
  opacity: 1 !important;
}

/* Touch devices can't hover — always show the arrows at a softer
   opacity so the affordance is discoverable. */
@media (hover: none) {
  .stage-nav {
    opacity: 0.7;
  }
}
</style>
