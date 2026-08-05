<script lang="ts">
/**
 * The stage is the one surface that legitimately wants the original pixels —
 * it is full-screen and the 1024x1536 source is already being *scaled up*, so
 * a thumbnail here would show as blur. What it does not want is the original
 * *file*: `variant="full"` is the same pixels re-encoded as WebP, 2313 KB down
 * to 218 KB (IMAGE_DELIVERY_AND_PAGINATION_PLAN §1.5 / D1).
 *
 * The second half of ticket IV5-B is what is mounted. This component used to
 * put **every** URL in `image_urls` into an `<img>` and hide all but one with
 * `opacity: 0` — an invisible image is still a decoded bitmap, and at
 * 1024x1536x4 bytes that is 6.0 MB of resident memory per hidden image, for as
 * long as the character stays selected. Six images meant 36 MB to show one.
 *
 * Now only a window is mounted. The window is not "the active one": a
 * cross-fade is a transition *between two co-existing elements*, so both ends
 * of every move this component can make have to already be in the DOM.
 * See `stageMountedIndices`.
 */

/**
 * How many neighbours on each side of the active image stay mounted.
 *
 * One, because every automatic move is +/-1: the 8 s rotation, both arrows and
 * the pointer swipe. Keeping both neighbours means those moves find their
 * target already mounted *and* decoded, and the 1.2 s fade starts on the same
 * frame as the click — exactly as it did when all N were mounted.
 */
const NEIGHBOUR_RADIUS = 1

/** Modulo that also wraps negatives, so `prev` from index 0 lands on the last. */
function wrapIndex(index: number, total: number): number {
  return ((index % total) + total) % total
}

/**
 * The indices that need a live `<img>`, in ascending order.
 *
 * Ascending matters: the elements are absolutely stacked and paint in document
 * order, so a cross-fade's over/under relationship is decided by index. Keeping
 * the old order keeps the old look.
 *
 * `retained` carries the two indices that are outside the window but still have
 * to be on screen for one move: the image currently *fading out* (after a dot
 * jump the outgoing index is nowhere near the new window) and a jump target
 * that was mounted a frame early so that its fade-*in* has a `0` to transition
 * from. Both are single slots, so the mounted count is four in the steady state
 * — five for the two frames a second consecutive dot jump straddles — and never
 * a function of how many images the character has.
 */
export function stageMountedIndices(
  total: number,
  active: number,
  retained: readonly (number | null)[] = [],
): number[] {
  if (total <= 0) return []
  const live = new Set<number>()
  const centre = wrapIndex(active, total)
  for (let offset = -NEIGHBOUR_RADIUS; offset <= NEIGHBOUR_RADIUS; offset += 1) {
    live.add(wrapIndex(centre + offset, total))
  }
  for (const index of retained) {
    if (index === null) continue
    if (!Number.isInteger(index) || index < 0 || index >= total) continue
    live.add(index)
  }
  return [...live].sort((a, b) => a - b)
}
</script>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiImage } from '@/components/ui'
import type { Character } from '@/types/character'

const { t } = useI18n()

const props = defineProps<{
  character: Character | null
}>()

const ROTATE_INTERVAL_MS = 8000

/** The selected image. Drives the dots, the arrows and the `.active` opacity. */
const activeIndex = ref(0)
/** The image that is fading out — held mounted so the cross-fade has two ends. */
const previousIndex = ref<number | null>(null)
/** A jump target, mounted before it is activated so its fade-in can run. */
const pendingIndex = ref<number | null>(null)
let rotateTimer: ReturnType<typeof setInterval> | null = null
let pendingFrame: number | null = null

const images = computed<string[]>(() => props.character?.image_urls ?? [])

const mountedIndices = computed(() =>
  stageMountedIndices(images.value.length, activeIndex.value, [
    previousIndex.value,
    pendingIndex.value,
  ]),
)

function clearTimer() {
  if (rotateTimer !== null) {
    clearInterval(rotateTimer)
    rotateTimer = null
  }
}

function cancelPendingFrame() {
  if (pendingFrame !== null && typeof cancelAnimationFrame === 'function') {
    cancelAnimationFrame(pendingFrame)
  }
  pendingFrame = null
}

/**
 * Two frames, not one. A freshly inserted element's `opacity: 0` has to be
 * *observed* by the browser before `opacity: 1` can transition away from it;
 * a single `requestAnimationFrame` still lands inside the frame that did the
 * insertion, and the browser would collapse both styles into one recalc and
 * skip the transition entirely. Same reasoning Vue's own `<Transition>` uses.
 *
 * Outside a browser (SSR) there is nothing to animate, so it runs inline.
 */
function nextFrame(run: () => void) {
  if (typeof requestAnimationFrame !== 'function') {
    run()
    return
  }
  pendingFrame = requestAnimationFrame(() => {
    pendingFrame = requestAnimationFrame(() => {
      pendingFrame = null
      run()
    })
  })
}

function commitIndex(index: number) {
  previousIndex.value = activeIndex.value
  activeIndex.value = index
  pendingIndex.value = null
}

/**
 * The single entry point for changing image. Adjacent targets (rotation,
 * arrows, swipe) are already mounted and switch on the spot; only a dot jump
 * can land outside the window, and that one waits a frame so the arriving
 * element fades in instead of appearing at full opacity.
 */
function selectIndex(index: number) {
  cancelPendingFrame()
  pendingIndex.value = null
  if (index === activeIndex.value) return
  if (mountedIndices.value.includes(index)) {
    commitIndex(index)
    return
  }
  pendingIndex.value = index
  nextFrame(() => commitIndex(index))
}

function restartRotation() {
  clearTimer()
  if (images.value.length <= 1) return
  rotateTimer = setInterval(() => {
    selectIndex(wrapIndex(activeIndex.value + 1, images.value.length))
  }, ROTATE_INTERVAL_MS)
}

watch(
  () => [props.character?.id, images.value.length] as const,
  () => {
    cancelPendingFrame()
    activeIndex.value = 0
    previousIndex.value = null
    pendingIndex.value = null
    restartRotation()
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  clearTimer()
  cancelPendingFrame()
})

function handleDotClick(index: number) {
  selectIndex(index)
  restartRotation()
}

function step(delta: -1 | 1) {
  const total = images.value.length
  if (total <= 1) return
  selectIndex(wrapIndex(activeIndex.value + delta, total))
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
        <UiImage
          v-for="index in mountedIndices"
          :key="`${index}:${images[index]}`"
          :src="images[index]"
          variant="full"
          :alt="character.name"
          :class="['stage-image', { active: index === activeIndex }]"
          :draggable="false"
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
     feather on both axes. ``-webkit-mask-*`` for Safari / iOS.

     The two layers are not a duplicate of each other — one feathers the
     left/right edges, the other the top/bottom — so neither can be
     dropped without losing feathering on that axis. What made them
     expensive was the count: a masked element gets its own composited
     layer, and this rule used to apply to every URL the character had.
     Since IV5-B at most four elements carry it (see stageMountedIndices),
     which is where that cost went. Visuals deliberately unchanged. */
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
