<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import type { ChatMessage } from '@/types/chat'
import type { MessageAttachment } from '@/types/tool'
import {
  type OperatorFeedbackKind,
  updateTurnOperatorFeedback,
} from '@/utils/api/observability'
import { synthesizeCharacterTTS, TTSDisabledError } from '@/utils/api/tts'
import { isInsufficientCreditsError } from '@/utils/api/insufficientCredits'
import { useAuth } from '@/composables/useAuth'
import { refreshCloudCreditsAfterAction } from '@/composables/useCloudCredits'
import { revealDelaysFor, splitAssistantBubbles } from '@/utils/chatSegments'
import { clampSeedPrompt, composeMomentSeed } from '@/utils/fusionSeed'
import { stashStudioSeed } from '@/utils/studioSeedTransfer'
import { isTTSPlaybackEligible } from '@/utils/ttsAvailability'
import { UiImage } from '@/components/ui'
import { observeOffscreenRelease } from '@/utils/offscreenImageObserver'
import {
  type ImageBox,
  OFFSCREEN_IMAGE_RELEASE_MARGIN_PX,
  releasedImageSrc,
  usableImageBox,
} from '@/utils/offscreenImageRelease'

const { t } = useI18n()
const { isAdmin } = useAuth()
const router = useRouter()

type ContentSegment = { kind: 'speech' | 'action'; text: string }
type TTSStatus = 'idle' | 'loading' | 'playing' | 'unavailable' | 'error'

const props = defineProps<{
  message: ChatMessage
  characterId?: string | null
  ttsAvailable?: boolean
  animateReveal?: boolean
  textMessageMode?: boolean
}>()

const emit = defineEmits<{
  revealComplete: []
  revealProgress: []
  /**
   * Voicing this bubble was refused for lack of credits. The bubble has no
   * room for the explanation card, so the chat panel renders the one shared
   * notice for the whole conversation.
   */
  insufficientCredits: []
}>()

const imageAttachments = computed<MessageAttachment[]>(() =>
  (props.message.attachments ?? []).filter(a => a.kind === 'image'),
)

const otherAttachments = computed<MessageAttachment[]>(() =>
  (props.message.attachments ?? []).filter(a => a.kind !== 'image'),
)

// --- Chat pictures: right size, and only while they are worth holding (IV5-C)
/**
 * How wide a chat picture ever actually paints, so the browser can pick the
 * cheap candidate instead of assuming `100vw`.
 *
 * From the layout, not from a guess: the desktop chat column is 420 px
 * (`StagePage`), less the container's 32 px of padding, and `.bubble` caps at
 * 80% of that — 310 px, rounded to the 320 px the small variant already is.
 * Below that breakpoint the thread is viewport-wide, so the same 80% of
 * `100vw - 32px` is ~72vw. `.bubble-image { max-height: 360px }` usually binds
 * first (a 2:3 portrait paints 240 px wide), so this is the ceiling, not the
 * common case.
 */
const BUBBLE_IMAGE_SIZES = '(max-width: 960px) 72vw, 320px'

/** The whole picture strip is observed as one unit; a bubble rarely has two. */
const bubbleImagesEl = ref<HTMLElement | null>(null)
const imagesReleased = ref(false)
let stopObservingImages: (() => void) | null = null

/**
 * The box each picture occupied while it was loaded, keyed by URL.
 *
 * Fed straight back as the element's `width`/`height`, which is what makes
 * releasing free of layout consequences: the placeholder is the same box the
 * picture was. Recorded on load rather than assumed, because chat attachments
 * are not all the 1024x1536 this product generates — players upload their own,
 * and `MessageAttachment` carries no dimensions for us to read.
 */
const imageBoxes = ref<Record<string, ImageBox>>({})

function imageSrcFor(url: string): string {
  return releasedImageSrc(url, imagesReleased.value)
}

function rememberImageBox(url: string, event: Event) {
  const el = event.target as HTMLImageElement | null
  // `complete` + a real natural width means the bytes are in and layout has
  // settled around them; measuring earlier records the placeholder as if it
  // were the picture.
  if (!el || !el.complete || el.naturalWidth <= 0) return
  const box = usableImageBox(el.offsetWidth, el.offsetHeight)
  if (box) imageBoxes.value = { ...imageBoxes.value, [url]: box }
}

// The strip is behind `v-if` (it only renders once the text has finished
// revealing), so the element arrives after mount and can go away again —
// hence a watch rather than a one-shot `onMounted`.
watch(bubbleImagesEl, (el) => {
  stopObservingImages?.()
  stopObservingImages = null
  if (!el) return
  stopObservingImages = observeOffscreenRelease(
    el,
    OFFSCREEN_IMAGE_RELEASE_MARGIN_PX,
    (released) => { imagesReleased.value = released },
  )
})

// Split `*action*` runs out of the content so they can render with a
// distinct muted / italic style. Matches single-line runs only — if the
// model forgets to close a star or wraps across newlines we leave the
// raw text as speech to avoid swallowing half the bubble.
// Strip ``*action*`` runs out of TTS input — these are stage
// directions, not speech. Keep speech-only so the model doesn't try
// to voice "*微笑*". Falls back to raw content when there are no
// action runs (vast majority of bubbles).
const speechText = computed<string>(() => {
  const raw = props.message.content ?? ''
  if (!raw) return ''
  if (!raw.includes('*')) return raw
  return raw
    .replace(/\*[^*\n]+\*/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
})

const ttsEligible = computed(() => isTTSPlaybackEligible({
  channelAvailable: props.ttsAvailable === true,
  isAssistantMessage: props.message.role === 'assistant',
  characterId: props.characterId,
  speechText: speechText.value,
}))

const feedbackEligible = computed(
  () => isAdmin.value
    && props.message.role === 'assistant'
    && !!props.message.turn_record_id,
)

// "寫成番外": any assistant message with a known speaker can seed a fusion
// side-story. Uses the *whole* message content (not a single split bubble)
// so the moment carries the full exchange.
const writeExtraEligible = computed(
  () => props.message.role === 'assistant'
    && !!props.characterId
    && (props.message.content ?? '').trim().length > 0,
)

/**
 * Turn this chat moment into a fusion side-story seed and hand it to the
 * creator form via the in-memory stash (never the URL — chat prose is
 * private and must not reach Referer / history / access logs). Carries
 * only the current speaker; the create form nudges the user to add a
 * second cast member (min 2).
 */
function handleWriteExtra() {
  if (!writeExtraEligible.value || !props.characterId) return
  const seed = clampSeedPrompt(composeMomentSeed({
    momentText: props.message.content ?? '',
    strings: {
      momentLabel: t('chat.bubble.writeExtraMomentLabel'),
      instructionLabel: t('chat.bubble.writeExtraInstructionLabel'),
      instruction: t('chat.bubble.writeExtraInstruction'),
    },
  }))
  stashStudioSeed({ seedPrompt: seed, cast: [props.characterId] })
  void router.push({ name: 'studio-fusion-stories' })
}

const ttsStatus = ref<TTSStatus>('idle')
const ttsErrorMsg = ref<string>('')
const feedbackSaving = ref<OperatorFeedbackKind | null>(null)
const feedbackMarked = ref<OperatorFeedbackKind | null>(null)
const feedbackErrorMsg = ref<string>('')
// Cache the URL we got from the server so a replay re-uses the same
// <audio> element / file without another network round-trip.
const cachedAudioUrl = ref<string | null>(null)
let audioElement: HTMLAudioElement | null = null
let revealRunId = 0
let revealTimer: number | null = null

const visibleBubbleCount = ref(0)
const revealingBetweenSegments = ref(false)

function teardownAudio() {
  if (audioElement) {
    audioElement.onended = null
    audioElement.onerror = null
    audioElement.pause()
    audioElement = null
  }
}

function clearRevealTimer() {
  if (revealTimer !== null) {
    window.clearTimeout(revealTimer)
    revealTimer = null
  }
}

function waitForRevealDelay(ms: number): Promise<void> {
  return new Promise(resolve => {
    revealTimer = window.setTimeout(() => {
      revealTimer = null
      resolve()
    }, ms)
  })
}

function notifyRevealProgress() {
  void nextTick(() => emit('revealProgress'))
}

async function handlePlayClick() {
  if (!props.characterId || !ttsEligible.value) return
  if (ttsStatus.value === 'loading') return

  // Toggle stop while playing.
  if (ttsStatus.value === 'playing') {
    teardownAudio()
    ttsStatus.value = 'idle'
    return
  }

  let url = cachedAudioUrl.value
  if (!url) {
    ttsStatus.value = 'loading'
    ttsErrorMsg.value = ''
    try {
      const resp = await synthesizeCharacterTTS(
        props.characterId, speechText.value,
      )
      url = resp.audio_url
      cachedAudioUrl.value = url
      // Synthesis is a charged, player-initiated action (replays hit the
      // server cache and cost nothing, hence only on a fresh URL).
      refreshCloudCreditsAfterAction()
    } catch (err) {
      if (isInsufficientCreditsError(err)) {
        ttsStatus.value = 'error'
        ttsErrorMsg.value = t('credits.insufficient.title')
        emit('insufficientCredits')
        return
      }
      if (err instanceof TTSDisabledError) {
        ttsStatus.value = 'unavailable'
        ttsErrorMsg.value = err.message
        return
      }
      ttsStatus.value = 'error'
      ttsErrorMsg.value = err instanceof Error ? err.message : t('chat.bubble.ttsSynthFailed')
      return
    }
  }

  teardownAudio()
  audioElement = new Audio(url)
  audioElement.onended = () => {
    ttsStatus.value = 'idle'
    audioElement = null
  }
  audioElement.onerror = () => {
    ttsStatus.value = 'error'
    ttsErrorMsg.value = t('chat.bubble.ttsPlayFailed')
    audioElement = null
  }
  ttsStatus.value = 'playing'
  try {
    await audioElement.play()
  } catch {
    // Some browsers reject programmatic play() without a recent
    // user gesture; the click handler IS a gesture so this almost
    // never fires, but be safe.
    ttsStatus.value = 'error'
    ttsErrorMsg.value = t('chat.bubble.ttsAutoplayDenied')
    audioElement = null
  }
}

async function markFeedback(kind: OperatorFeedbackKind) {
  const turnRecordId = props.message.turn_record_id
  if (!turnRecordId || feedbackSaving.value) return
  feedbackSaving.value = kind
  feedbackErrorMsg.value = ''
  try {
    await updateTurnOperatorFeedback(turnRecordId, { kind })
    feedbackMarked.value = kind
  } catch (err) {
    feedbackErrorMsg.value = err instanceof Error
      ? err.message
      : t('chat.bubble.feedbackFailed')
  } finally {
    feedbackSaving.value = null
  }
}

onBeforeUnmount(() => {
  revealRunId += 1
  clearRevealTimer()
  teardownAudio()
  // The shared observer holds this element strongly; leaving it observed would
  // keep the whole unmounted bubble alive.
  stopObservingImages?.()
  stopObservingImages = null
})

const ttsButtonLabel = computed(() => {
  switch (ttsStatus.value) {
    case 'loading': return t('chat.bubble.ttsLoading')
    case 'playing': return t('chat.bubble.ttsStop')
    case 'unavailable': return t('chat.bubble.ttsUnavailable')
    case 'error': return t('chat.bubble.ttsRetry')
    default: return cachedAudioUrl.value ? t('chat.bubble.ttsReplay') : t('chat.bubble.ttsPlay')
  }
})

const ttsButtonIcon = computed(() => {
  switch (ttsStatus.value) {
    case 'loading': return '⏳'
    case 'playing': return '■'
    case 'unavailable': return '🔇'
    case 'error': return '⚠'
    default: return '▶'
  }
})

function splitActionSegments(raw: string): ContentSegment[] {
  if (!raw) return []
  const segments: ContentSegment[] = []
  const pattern = /\*([^*\n]+)\*/g
  let cursor = 0
  let match: RegExpExecArray | null
  while ((match = pattern.exec(raw)) !== null) {
    if (match.index > cursor) {
      segments.push({ kind: 'speech', text: raw.slice(cursor, match.index) })
    }
    segments.push({ kind: 'action', text: match[1] })
    cursor = match.index + match[0].length
  }
  if (cursor < raw.length) {
    segments.push({ kind: 'speech', text: raw.slice(cursor) })
  }
  return segments
}

const bubbleTexts = computed<string[]>(() => {
  const raw = props.message.content ?? ''
  if (!raw.trim()) return []
  if (props.message.role !== 'assistant') return [raw]
  return splitAssistantBubbles(raw, {
    stripActionNarration: !!props.textMessageMode,
  })
})

const displayedBubbleTexts = computed<string[]>(() =>
  bubbleTexts.value.slice(0, visibleBubbleCount.value),
)

const allBubblesVisible = computed<boolean>(() =>
  bubbleTexts.value.length === 0
    || (
      visibleBubbleCount.value >= bubbleTexts.value.length
      && !revealingBetweenSegments.value
    ),
)

async function runRevealAnimation(): Promise<void> {
  const runId = ++revealRunId
  clearRevealTimer()
  revealingBetweenSegments.value = false

  const texts = bubbleTexts.value
  const shouldAnimate = !!props.animateReveal
    && props.message.role === 'assistant'
    && texts.length > 1

  if (!shouldAnimate) {
    visibleBubbleCount.value = texts.length
    if (props.animateReveal) notifyRevealProgress()
    if (props.animateReveal) emit('revealComplete')
    return
  }

  visibleBubbleCount.value = 1
  notifyRevealProgress()
  const revealDelays = revealDelaysFor(texts)
  for (let nextIndex = 1; nextIndex < texts.length; nextIndex += 1) {
    revealingBetweenSegments.value = true
    notifyRevealProgress()
    await waitForRevealDelay(revealDelays[nextIndex - 1])
    if (runId !== revealRunId) return
    revealingBetweenSegments.value = false
    visibleBubbleCount.value = nextIndex + 1
    notifyRevealProgress()
  }
  emit('revealComplete')
}

watch(
  () => [
    props.message.content,
    props.message.role,
    props.animateReveal,
    props.textMessageMode,
  ] as const,
  () => {
    void runRevealAnimation()
  },
  { immediate: true },
)
</script>

<template>
  <div :class="['bubble', message.role]">
    <div
      v-for="(bubbleText, bubbleIdx) in displayedBubbleTexts"
      :key="`${bubbleIdx}-${bubbleText.length}`"
      class="bubble-row"
    >
      <div class="bubble-content">
        <template v-for="(seg, idx) in splitActionSegments(bubbleText)" :key="idx">
          <span v-if="seg.kind === 'action'" class="bubble-action">{{ seg.text }}</span>
          <template v-else>{{ seg.text }}</template>
        </template>
      </div>
      <button
        v-if="ttsEligible && allBubblesVisible && bubbleIdx === bubbleTexts.length - 1"
        type="button"
        :class="['bubble-tts', `is-${ttsStatus}`]"
        :title="ttsErrorMsg || ttsButtonLabel"
        :aria-label="ttsButtonLabel"
        :disabled="ttsStatus === 'loading' || ttsStatus === 'unavailable'"
        @click="handlePlayClick"
      >
        <span class="bubble-tts-icon" aria-hidden="true">{{ ttsButtonIcon }}</span>
      </button>
      <button
        v-if="writeExtraEligible && allBubblesVisible && bubbleIdx === bubbleTexts.length - 1"
        type="button"
        class="bubble-extra"
        :title="t('chat.bubble.writeExtra')"
        :aria-label="t('chat.bubble.writeExtra')"
        @click="handleWriteExtra"
      >
        <span class="bubble-extra-icon" aria-hidden="true">✎</span>
      </button>
      <div
        v-if="feedbackEligible && allBubblesVisible && bubbleIdx === bubbleTexts.length - 1"
        class="bubble-feedback"
      >
        <button
          type="button"
          :class="[
            'bubble-feedback-btn',
            { active: feedbackMarked === 'out_of_character' },
          ]"
          :title="feedbackErrorMsg || t('chat.bubble.feedbackOutOfCharacter')"
          :aria-label="t('chat.bubble.feedbackOutOfCharacter')"
          :disabled="feedbackSaving !== null"
          @click.stop="markFeedback('out_of_character')"
        >!</button>
        <button
          type="button"
          :class="[
            'bubble-feedback-btn',
            { active: feedbackMarked === 'felt_human' },
          ]"
          :title="feedbackErrorMsg || t('chat.bubble.feedbackFeltHuman')"
          :aria-label="t('chat.bubble.feedbackFeltHuman')"
          :disabled="feedbackSaving !== null"
          @click.stop="markFeedback('felt_human')"
        >*</button>
      </div>
    </div>

    <div v-if="revealingBetweenSegments" class="bubble-typing" aria-live="polite">
      <span class="bubble-dot" /><span class="bubble-dot" /><span class="bubble-dot" />
    </div>

    <div
      v-if="imageAttachments.length && allBubblesVisible"
      ref="bubbleImagesEl"
      class="bubble-images"
    >
      <!-- The href stays the original: "open in a new tab" means the full
           picture, not the size the thread happened to display. -->
      <a
        v-for="att in imageAttachments"
        :key="att.url"
        :href="att.url"
        target="_blank"
        rel="noopener"
        class="bubble-image-link"
      >
        <UiImage
          :src="imageSrcFor(att.url)"
          :alt="att.caption ?? t('chat.bubble.imageAlt')"
          variant="content"
          :sizes="BUBBLE_IMAGE_SIZES"
          :width="imageBoxes[att.url]?.width"
          :height="imageBoxes[att.url]?.height"
          class="bubble-image"
          @load="rememberImageBox(att.url, $event)"
        />
        <span v-if="att.caption" class="bubble-image-caption">{{ att.caption }}</span>
      </a>
    </div>

    <div v-if="otherAttachments.length && allBubblesVisible" class="bubble-files">
      <a
        v-for="att in otherAttachments"
        :key="att.url"
        :href="att.url"
        target="_blank"
        rel="noopener"
        class="bubble-file"
      >
        📎 {{ att.caption ?? att.url.split('/').pop() }}
      </a>
    </div>
  </div>
</template>

<style scoped>
.bubble {
  max-width: 80%;
  animation: fadeIn 0.2s ease;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.bubble.user {
  align-self: flex-end;
}

.bubble.assistant {
  align-self: flex-start;
}

.bubble-row {
  display: flex;
  align-items: flex-end;
  gap: 6px;
}

.user .bubble-row {
  flex-direction: row-reverse;
}

.bubble-content {
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 14px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  flex: 0 1 auto;
  min-width: 0;
}

.bubble-tts {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-secondary);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  line-height: 1;
  padding: 0;
  transition: background 0.15s, color 0.15s, transform 0.1s;
}

.bubble-tts:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.1);
  color: var(--color-text);
}

.bubble-tts:active:not(:disabled) {
  transform: scale(0.92);
}

.bubble-tts:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.bubble-tts.is-playing {
  color: #6aa9f0;
  border-color: rgba(106, 169, 240, 0.55);
  background: rgba(106, 169, 240, 0.12);
}

.bubble-tts.is-loading {
  color: var(--color-text-secondary);
}

.bubble-tts.is-error {
  color: #ff8a75;
  border-color: rgba(255, 138, 117, 0.45);
}

.bubble-tts.is-unavailable {
  opacity: 0.4;
}

.bubble-tts-icon {
  display: inline-block;
  pointer-events: none;
}

/* "寫成番外" trigger — mirrors the .bubble-tts pill so the per-message
   action row reads as one family. */
.bubble-extra {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-secondary);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  line-height: 1;
  padding: 0;
  transition: background 0.15s, color 0.15s, transform 0.1s;
}

.bubble-extra:hover {
  background: rgba(139, 92, 246, 0.14);
  color: var(--color-text);
  border-color: rgba(139, 92, 246, 0.5);
}

.bubble-extra:active {
  transform: scale(0.92);
}

.bubble-extra-icon {
  display: inline-block;
  pointer-events: none;
}

.bubble-feedback {
  display: inline-flex;
  flex-direction: column;
  gap: 4px;
}

.bubble-feedback-btn {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-secondary);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  line-height: 1;
  padding: 0;
  transition: background 0.15s, color 0.15s, transform 0.1s;
}

.bubble-feedback-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.1);
  color: var(--color-text);
}

.bubble-feedback-btn:active:not(:disabled) {
  transform: scale(0.92);
}

.bubble-feedback-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.bubble-feedback-btn.active {
  color: #4ade80;
  border-color: rgba(74, 222, 128, 0.55);
  background: rgba(74, 222, 128, 0.12);
}

.bubble-action {
  font-style: italic;
  opacity: 0.7;
  font-size: 0.92em;
}

.user .bubble-content {
  background: var(--color-user-bubble);
  color: white;
  border-bottom-right-radius: 4px;
}

.assistant .bubble-content {
  background: var(--color-assistant-bubble);
  color: var(--color-text);
  border-bottom-left-radius: 4px;
}

.bubble-typing {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  background: var(--color-assistant-bubble);
  border-radius: 12px;
  border-bottom-left-radius: 4px;
  width: fit-content;
}

.bubble-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  animation: bounce 1.4s infinite ease-in-out;
}

.bubble-dot:nth-child(2) { animation-delay: 0.2s; }
.bubble-dot:nth-child(3) { animation-delay: 0.4s; }

.bubble-images {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.bubble-image-link {
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-decoration: none;
  color: inherit;
}

.bubble-image {
  max-width: 100%;
  max-height: 360px;
  /* Not decoration — the counterpart to the measured `width`/`height` the
     released placeholder carries (IV5-C). A replaced element with *both*
     dimensions pinned ignores its ratio when `max-width` shrinks it, so a
     narrowed window would squash the picture. With the height auto, the width
     attribute (and the ratio it implies) drives both states to the same box,
     released or not. */
  height: auto;
  border-radius: 10px;
  border: 1px solid var(--color-border);
  background: rgba(0, 0, 0, 0.15);
  object-fit: contain;
  display: block;
}

.bubble-image-caption {
  font-size: 11px;
  color: var(--color-text-secondary);
  padding: 0 4px;
}

.bubble-files {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.bubble-file {
  font-size: 12px;
  padding: 6px 10px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  text-decoration: none;
  word-break: break-all;
}

.bubble-file:hover {
  color: var(--color-text);
  background: rgba(255, 255, 255, 0.08);
}

/* Portrait overlay：氣泡跟著 chat 面板一起透出舞台輪播圖。
   使用 --chat-opacity (由 StagePage 注入) 控制氣泡 alpha；
   沒注入時退回原本實色 (landscape / 其他容器) */
@media (orientation: portrait) {
  .user .bubble-content {
    background: rgba(183, 93, 63, var(--chat-opacity, 1));
  }
  .assistant .bubble-content {
    background: rgba(30, 58, 95, var(--chat-opacity, 1));
  }
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}
</style>
