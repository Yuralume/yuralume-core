<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import type {
  BranchingDrama,
  DramaNode,
  DramaSession,
  DramaSessionTurn,
  Exchange,
} from '@/types/branchingDrama'
import {
  advanceSession,
  endDramaSession,
  getDramaNode,
  getSession,
  interactSession,
  regenerateSceneImage,
  startSession,
} from '@/utils/api/branchingDrama'
import { isInsufficientCreditsError } from '@/utils/api/insufficientCredits'
import { isRetryableConflict } from '@/utils/api/billingRefusal'
import { useBillingNotice } from '@/composables/useBillingNotice'
import { refreshCloudCreditsAfterAction } from '@/composables/useCloudCredits'
import ActionPriceHint from '@/components/ActionPriceHint.vue'
import CloudCreditsBadge from '@/components/CloudCreditsBadge.vue'
import DramaExitHub from '@/components/branching-drama/DramaExitHub.vue'
import InsufficientCreditsNotice from '@/components/InsufficientCreditsNotice.vue'
import { UiButton } from '@/components/ui'
import {
  ACTION_BRANCHING_DRAMA_ADVANCE,
  ACTION_BRANCHING_DRAMA_INTERACT,
  ACTION_BRANCHING_DRAMA_SCENE_REGEN,
} from '@/composables/useActionPricing'
import type { FusionToArcOperatorMode } from '@/types/fusionStory'
import { DRAMA_POSITION_TO_ARC_MODE } from '@/types/branchingDrama'
import {
  buildDramaTranscript,
  dramaTranscriptFilename,
  type DramaTranscriptBeat,
  type DramaTranscriptFormat,
} from '@/utils/dramaTranscript'
import { buildDramaProgress, type DramaProgress } from '@/utils/dramaProgress'
import { isRetryAborted, retryWithBackoff } from '@/utils/retryWithBackoff'
import { resolveSceneImageUrl } from '@/utils/sceneImage'
import { dramaToneLabel } from '@/utils/dramaTone'

/**
 * `advance` answers a transient `409` while another replica is still
 * generating the next beat's outline layer (see the route's
 * `BranchingGenerationInProgress` mapping) — the route comment is explicit
 * that the client should simply retry shortly, so that 409 must never
 * bubble up as a fatal error that boots the player out of the VN (BD5).
 *
 * The predicate is `isRetryableConflict`, not "status === 409": the very
 * same status also carries `price_changed`, which is a decision. Sitting on
 * it for a full retry window would show a minute of fake "still generating…"
 * and then throw a moved price at the player as a fault (FX2).
 */
const ADVANCE_RETRY_INITIAL_DELAY_MS = 2000
const ADVANCE_RETRY_MAX_DELAY_MS = 8000
const ADVANCE_RETRY_TOTAL_WINDOW_MS = 60000

const { t } = useI18n()

const props = defineProps<{
  drama: BranchingDrama
  characters: Character[]
  resumeSessionId?: string | null
  /**
   * Every *other* saved playthrough of this drama, as the page last read
   * them (BD14). Only the ending's branching graph consumes them, and only
   * to say what the accumulated history looks like behind the run that just
   * finished —— the live session below is always the authority on itself.
   */
  sessions?: readonly DramaSession[] | null
  /** True while the page is converting this playthrough into a draft (BD7). */
  adaptingToArc?: boolean
}>()

const emit = defineEmits<{
  (e: 'exit'): void
  (e: 'error', msg: string): void
  /**
   * BD7 — the ending's exits. The player owns the session and the
   * transcript; the *page* owns the conversion call and the draft wizard it
   * opens, so 「寫成劇本」 and 「換條路再走」 leave here as intent.
   */
  (
    e: 'adapt-requested',
    payload: { sessionId: string; mode: FusionToArcOperatorMode },
  ): void
  (e: 'replay-requested'): void
  /**
   * 「場景圖集」 (BD9). The collection belongs to the whole drama, not to this
   * playthrough, and it is painted on the drama's own page — so this leaves
   * the VN exactly like 「回列表」 does, and the page opens the panel.
   */
  (e: 'gallery-requested'): void
}>()

const session = ref<DramaSession | null>(null)
const currentNode = ref<DramaNode | null>(null)
const narrationText = ref('')
const playerInput = ref('')
const loading = ref(false)
const reachedEnd = ref(false)
const showEndOverlay = ref(false)
const displayedTurns = ref<DramaSessionTurn[]>([])
const advanceHint = ref<string | null>(null)
const pendingExchanges = ref<Exchange[]>([])
const atFinalBeat = ref(false)
const retryingAdvance = ref(false)
/**
 * A billing refusal on start / talk / advance (FX2).
 *
 * Neither of these is a fault and neither charged anything, so neither may
 * take the playthrough down with it: `emit('error')` boots the player back
 * to the list, loses the line they had just typed, and shows the gateway's
 * English sentence. They are answered in place instead — the shared top-up
 * card, or "prices moved, press it again" — exactly like the redraw (BD6)
 * already does one header above.
 */
const billing = useBillingNotice()
const {
  outOfCredits: billingOutOfCredits,
  priceChanged: billingPriceChanged,
} = billing
/** True when it was the *opening* that was refused: there is no session to
 *  press on with, so the notice carries its own retry. */
const startRefused = ref(false)
/**
 * Cancels an advance retry loop the player has walked away from. The window
 * is up to a minute long, so leaving mid-wait is ordinary; without this the
 * timer and its eventual rejection outlive the screen that asked (FX2).
 */
let advanceAbort: AbortController | null = null
/**
 * The redraw (BD6) deliberately does NOT emit `error`: the page's handler
 * for that boots the player out of the VN, which is a wildly
 * disproportionate answer to "that picture didn't come out". It fails in
 * place instead — a dismissible line, or the shared top-up card when the
 * wallet is what refused.
 */
const regenerating = ref(false)
const regenError = ref('')
const regenOutOfCredits = ref(false)
/**
 * BD7 export. Composed here rather than fetched: the whole playthrough is
 * already on screen, so asking the server to render it again would be a
 * round trip for text the browser is holding. Node titles are remembered
 * as they arrive and back-filled fail-soft on press, so a resumed session
 * still gets its headings and a missing node costs one heading, not the
 * file.
 */
const exportingFormat = ref<DramaTranscriptFormat | null>(null)
const exportError = ref('')
const nodeTitles = new Map<string, string>()

/** Which conversion mode the exit hub starts on — how this was played. */
const adaptDefaultMode = computed<FusionToArcOperatorMode>(
  () => DRAMA_POSITION_TO_ARC_MODE[props.drama.operator_position] ?? 'write_in',
)

/**
 * 分歧圖的資料來源 (BD14)，給結局畫面用。
 *
 * The page's `sessions` are as of the last `listSessions` —— which ran
 * *before* this playthrough existed, so on its own it would draw an ending
 * screen with the run that just ended missing from it. The live session is
 * therefore merged in, replacing any stale copy of itself by id (a resumed
 * save slot is in both lists, and counting it twice would double its nodes
 * in the de-duplication that follows).
 */
const dramaProgress = computed<DramaProgress | null>(() => {
  const live = session.value
  const history = (props.sessions ?? []).filter((s) => s.id !== live?.id)
  const merged = live ? [...history, live] : history
  if (merged.length === 0) return null
  return buildDramaProgress(merged, props.drama.total_segments)
})

const charMap = computed(() => {
  const m = new Map<string, Character>()
  for (const c of props.characters) m.set(c.id, c)
  return m
})

const currentImageUrl = computed(() => {
  if (!currentNode.value?.image_path) return null
  return resolveSceneImageUrl(currentNode.value.image_path)
})

const appearingCharNames = computed(() => {
  if (!currentNode.value) return ''
  return currentNode.value.appearing_character_ids
    .map((id) => charMap.value.get(id)?.name ?? '???')
    .join(t('common.listSeparator'))
})

const advanceButtonText = computed(() => {
  if (atFinalBeat.value) {
    return advanceHint.value
      ? t('branchingDrama.player.endWithHint', { hint: advanceHint.value })
      : t('branchingDrama.player.endStory')
  }
  if (advanceHint.value) return t('branchingDrama.player.advanceWithHint', { hint: advanceHint.value })
  return t('branchingDrama.player.advance')
})

const scrollRef = ref<HTMLElement | null>(null)

function scrollToBottom() {
  nextTick(() => {
    if (scrollRef.value) {
      scrollRef.value.scrollTop = scrollRef.value.scrollHeight
    }
  })
}

function clearBillingNotice() {
  billing.clear()
  startRefused.value = false
}

function cancelAdvanceRetry() {
  advanceAbort?.abort()
  advanceAbort = null
}

async function begin() {
  loading.value = true
  reachedEnd.value = false
  showEndOverlay.value = false
  displayedTurns.value = []
  advanceHint.value = null
  pendingExchanges.value = []
  atFinalBeat.value = false
  clearBillingNotice()
  try {
    let sess: DramaSession
    if (props.resumeSessionId) {
      sess = await getSession(props.drama.id, props.resumeSessionId)
    } else {
      // Opening a new playthrough writes (and bills) the first beat, same as
      // every later advance — resuming an existing session charges nothing.
      sess = await startSession(props.drama.id)
      refreshCloudCreditsAfterAction()
    }
    session.value = sess
    reachedEnd.value = sess.status === 'ended'
    showEndOverlay.value = false
    if (sess.turns.length > 0) {
      displayedTurns.value = [...sess.turns]
      const lastTurn = sess.turns[sess.turns.length - 1]
      narrationText.value = lastTurn.narration
      pendingExchanges.value = [...(lastTurn.exchanges ?? [])]
      const node = await getDramaNode(props.drama.id, lastTurn.node_id)
      currentNode.value = node
      rememberNodeTitle(node)
      atFinalBeat.value = node.depth >= props.drama.total_segments - 1
    }
    scrollToBottom()
  } catch (err: unknown) {
    // Starting a session writes the opening beat and is billed like one
    // advance (FX1), so the wallet and a moved price are both normal answers
    // here — and neither is a reason to throw the player back to the list.
    if (await billing.absorb(err)) {
      startRefused.value = true
      return
    }
    emit('error', err instanceof Error ? err.message : t('branchingDrama.player.errors.startFailed'))
  } finally {
    loading.value = false
  }
}

async function handleInteract() {
  if (!session.value || !playerInput.value.trim() || loading.value) return
  const input = playerInput.value.trim()
  loading.value = true
  clearBillingNotice()
  try {
    const result = await interactSession(
      props.drama.id,
      session.value.id,
      input,
    )
    // Cleared only once the line has actually landed: a refusal that charged
    // nothing must not also cost the player the sentence they wrote.
    playerInput.value = ''
    session.value = result.session
    advanceHint.value = result.advance_hint
    pendingExchanges.value.push({ player_input: input, response: result.response })
    displayedTurns.value = [...result.session.turns]
    scrollToBottom()
    refreshCloudCreditsAfterAction()
  } catch (err: unknown) {
    if (await billing.absorb(err)) return
    emit('error', err instanceof Error ? err.message : t('branchingDrama.player.errors.interactFailed'))
  } finally {
    loading.value = false
  }
}

async function handleAdvance() {
  if (!session.value || loading.value) return
  const dramaId = props.drama.id
  const sessionId = session.value.id
  loading.value = true
  retryingAdvance.value = false
  clearBillingNotice()
  cancelAdvanceRetry()
  const controller = new AbortController()
  advanceAbort = controller
  try {
    if (atFinalBeat.value) {
      const sess = await endDramaSession(dramaId, sessionId)
      session.value = sess
      reachedEnd.value = true
      showEndOverlay.value = false
      displayedTurns.value = [...sess.turns]
      cancelAdvanceRetry()
      scrollToBottom()
      refreshCloudCreditsAfterAction()
      return
    }
    const result = await retryWithBackoff(
      () => advanceSession(dramaId, sessionId),
      {
        isRetryable: isRetryableConflict,
        initialDelayMs: ADVANCE_RETRY_INITIAL_DELAY_MS,
        maxDelayMs: ADVANCE_RETRY_MAX_DELAY_MS,
        totalWindowMs: ADVANCE_RETRY_TOTAL_WINDOW_MS,
        onRetry: () => { retryingAdvance.value = true },
        signal: controller.signal,
      },
    )
    session.value = result.session
    currentNode.value = result.current_node
    rememberNodeTitle(result.current_node)
    atFinalBeat.value = result.is_ending
    showEndOverlay.value = false
    displayedTurns.value = [...result.session.turns]
    narrationText.value =
      result.session.turns[result.session.turns.length - 1]?.narration ?? ''
    advanceHint.value = null
    pendingExchanges.value = []
    scrollToBottom()
    refreshCloudCreditsAfterAction()
  } catch (err: unknown) {
    // The player left mid-wait: nobody is owed this answer, and the screen
    // that would show it is on its way out.
    if (isRetryAborted(err)) return
    if (await billing.absorb(err)) return
    emit('error', err instanceof Error ? err.message : t('branchingDrama.player.errors.advanceFailed'))
  } finally {
    if (advanceAbort === controller) advanceAbort = null
    loading.value = false
    retryingAdvance.value = false
  }
}

/** Leaving cancels whatever this screen was still waiting for. */
function handleExit() {
  cancelAdvanceRetry()
  emit('exit')
}

/** The opening was refused, not lost: press again at the price now in force. */
function retryStart() {
  if (loading.value) return
  void begin()
}

function handleReplay() {
  cancelAdvanceRetry()
  emit('replay-requested')
}

/** The gallery is on the drama's page, so this is a way out of the VN too. */
function handleGallery() {
  cancelAdvanceRetry()
  emit('gallery-requested')
}

function rememberNodeTitle(node: DramaNode | null) {
  if (node) nodeTitles.set(node.id, node.title)
}

/**
 * Titles for the beats on screen, back-filled for a resumed session.
 *
 * Fail-soft per node: a beat whose outline row is gone still exports its
 * narration under a numbered heading, because the transcript is the part
 * that exists nowhere else.
 */
async function collectTranscriptBeats(): Promise<DramaTranscriptBeat[]> {
  const turns = displayedTurns.value
  const missing = [...new Set(
    turns.map((turn) => turn.node_id).filter((id) => !nodeTitles.has(id)),
  )]
  await Promise.all(missing.map(async (nodeId) => {
    try {
      rememberNodeTitle(await getDramaNode(props.drama.id, nodeId))
    } catch {
      /* one heading, not the file */
    }
  }))
  return turns.map((turn) => ({
    title: nodeTitles.get(turn.node_id) ?? '',
    tone: dramaToneLabel(turn.chosen_tone, t),
    narration: turn.narration,
    playerInput: turn.player_input,
    exchanges: (turn.exchanges ?? []).map((ex) => ({
      playerInput: ex.player_input,
      response: ex.response,
    })),
  }))
}

async function handleExportTranscript(format: DramaTranscriptFormat) {
  if (exportingFormat.value) return
  exportingFormat.value = format
  try {
    const text = buildDramaTranscript({
      title: props.drama.title,
      beats: await collectTranscriptBeats(),
      format,
      strings: {
        you: t('branchingDrama.player.you'),
        beatFallback: (n: number) =>
          t('branchingDrama.exitHub.beatFallback', { n }),
      },
    })
    const blob = new Blob([text], {
      type: format === 'md'
        ? 'text/markdown;charset=utf-8'
        : 'text/plain;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = dramaTranscriptFilename(props.drama.title, format)
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (err: unknown) {
    // Same reasoning as the redraw: a failed export must not boot the
    // player out of the ending they just reached.
    exportError.value =
      err instanceof Error
        ? err.message
        : t('branchingDrama.exitHub.errors.exportFailed')
  } finally {
    exportingFormat.value = null
  }
}

function handleAdaptRequested(mode: FusionToArcOperatorMode) {
  if (!session.value || props.adaptingToArc) return
  emit('adapt-requested', { sessionId: session.value.id, mode })
}

async function handleRegenerateScene() {
  const node = currentNode.value
  if (!node || regenerating.value) return
  regenerating.value = true
  regenError.value = ''
  regenOutOfCredits.value = false
  try {
    // The response carries the node with its new `image_path`; the backend
    // gives every redraw a fresh object key, so simply swapping the node in
    // is enough to make the browser fetch the new picture.
    currentNode.value = await regenerateSceneImage(props.drama.id, node.id)
    refreshCloudCreditsAfterAction()
  } catch (err: unknown) {
    if (isInsufficientCreditsError(err)) {
      regenOutOfCredits.value = true
    } else {
      regenError.value =
        err instanceof Error
          ? err.message
          : t('branchingDrama.player.errors.regenFailed')
    }
  } finally {
    regenerating.value = false
  }
}

function dismissRegenError() {
  regenError.value = ''
  regenOutOfCredits.value = false
}

function handleKeydown(ev: KeyboardEvent) {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault()
    handleInteract()
  }
}

watch(
  () => props.drama.id,
  () => {
    cancelAdvanceRetry()
    clearBillingNotice()
    session.value = null
    currentNode.value = null
    narrationText.value = ''
    displayedTurns.value = []
    reachedEnd.value = false
    showEndOverlay.value = false
    advanceHint.value = null
    pendingExchanges.value = []
    atFinalBeat.value = false
    retryingAdvance.value = false
    regenerating.value = false
    regenError.value = ''
    regenOutOfCredits.value = false
    exportingFormat.value = null
    exportError.value = ''
    nodeTitles.clear()
  },
)

onBeforeUnmount(cancelAdvanceRetry)

begin()
</script>

<template>
  <div class="vn-player">
    <!-- background scene image -->
    <div
      class="vn-player__bg"
      :style="{
        backgroundImage: currentImageUrl
          ? `url(${currentImageUrl})`
          : undefined,
      }"
    >
      <div v-if="!currentImageUrl" class="vn-player__bg-fallback" />
    </div>

    <!-- top bar -->
    <header class="vn-player__header">
      <button class="vn-player__exit" @click="handleExit">
        &larr; {{ t('branchingDrama.player.backToList') }}
      </button>
      <span class="vn-player__title">{{ drama.title }}</span>
      <span v-if="currentNode" class="vn-player__depth">
        {{ currentNode.depth + 1 }} / {{ drama.total_segments }}
      </span>
      <!-- 螢火餘額：扣點動作都在這個播放器裡發生，玩家最需要在這裡看得到
           餘額。self-host／未知餘額時徽章自身不輸出任何節點；`inline` 讓展開
           的明細卡浮在工具列下方，不把這一排撐高擠壓對話區。 -->
      <CloudCreditsBadge variant="inline" class="vn-player__credits" />
      <!-- 重繪這一幕：暫時失敗補得回來，不滿意也重得了。價格自成一格，
           跟推進、對戲各自標價；查不到價格時 chip 不輸出任何節點。 -->
      <div v-if="currentNode" class="vn-player__regen-group">
        <UiButton
          variant="ghost"
          size="sm"
          :loading="regenerating"
          :disabled="regenerating || loading"
          :title="t('branchingDrama.player.regenSceneTooltip')"
          @click="handleRegenerateScene"
        >
          {{ regenerating
            ? t('branchingDrama.player.regeneratingScene')
            : t('branchingDrama.player.regenScene') }}
        </UiButton>
        <ActionPriceHint
          :action-key="ACTION_BRANCHING_DRAMA_SCENE_REGEN"
          tooltip-key="credits.price.dramaSceneRegenTooltip"
          variant="chip"
        />
      </div>
    </header>

    <!-- A failed redraw never ends the playthrough: it says so in place. -->
    <div v-if="regenOutOfCredits" class="vn-player__regen-notice">
      <InsufficientCreditsNotice />
      <button class="vn-player__regen-dismiss" @click="dismissRegenError">
        {{ t('common.actions.close') }}
      </button>
    </div>
    <div v-else-if="regenError" class="vn-player__regen-alert" role="status">
      <span>{{ regenError }}</span>
      <button class="vn-player__regen-dismiss" @click="dismissRegenError">
        {{ t('common.actions.close') }}
      </button>
    </div>

    <!-- 這一次沒有跑、也沒有扣點：所以就地說明，不把人踢出劇場。開場被擋下時
         沒有 session 可以按，通知自己帶一顆「再試一次」。 -->
    <div v-if="billingOutOfCredits" class="vn-player__regen-notice">
      <InsufficientCreditsNotice />
      <div class="vn-player__notice-actions">
        <button
          v-if="startRefused"
          class="vn-player__regen-dismiss"
          :disabled="loading"
          @click="retryStart"
        >
          {{ t('common.actions.retry') }}
        </button>
        <button class="vn-player__regen-dismiss" @click="clearBillingNotice">
          {{ t('common.actions.close') }}
        </button>
      </div>
    </div>
    <div
      v-else-if="billingPriceChanged"
      class="vn-player__regen-alert"
      role="status"
    >
      <span>{{ t('credits.price.changed') }}</span>
      <button
        v-if="startRefused"
        class="vn-player__regen-dismiss"
        :disabled="loading"
        @click="retryStart"
      >
        {{ t('common.actions.retry') }}
      </button>
      <button class="vn-player__regen-dismiss" @click="clearBillingNotice">
        {{ t('common.actions.close') }}
      </button>
    </div>

    <!-- dialogue scroll area -->
    <div ref="scrollRef" class="vn-player__dialogue-scroll">
      <template v-for="(turn, i) in displayedTurns" :key="i">
        <!-- narration (opening scene for this beat) -->
        <div class="vn-player__bubble vn-player__bubble--narration">
          <div class="vn-player__bubble-label">
            {{ appearingCharNames || t('branchingDrama.player.scene') }}
            <span v-if="turn.chosen_tone" class="vn-player__tone-tag" :data-tone="turn.chosen_tone">
              {{ dramaToneLabel(turn.chosen_tone, t) }}
            </span>
          </div>
          <div class="vn-player__bubble-text">{{ turn.narration }}</div>
        </div>
        <!-- exchanges within this beat -->
        <template v-for="(ex, j) in turn.exchanges" :key="`${i}-ex-${j}`">
          <div class="vn-player__bubble vn-player__bubble--player">
            <div class="vn-player__bubble-label">{{ t('branchingDrama.player.you') }}</div>
            <div class="vn-player__bubble-text">{{ ex.player_input }}</div>
          </div>
          <div class="vn-player__bubble vn-player__bubble--narration vn-player__bubble--exchange">
            <div class="vn-player__bubble-label">
              {{ appearingCharNames || t('branchingDrama.player.scene') }}
            </div>
            <div class="vn-player__bubble-text">{{ ex.response }}</div>
          </div>
        </template>
      </template>

      <div v-if="loading" class="vn-player__loading">
        {{ retryingAdvance ? t('branchingDrama.player.nextSegmentGenerating') : t('branchingDrama.player.thinking') }}
      </div>

      <!-- end prompt (shown after final narration, before overlay) -->
      <div v-if="reachedEnd && !loading && !showEndOverlay" class="vn-player__end-prompt">
        <button class="vn-player__btn vn-player__btn--end" @click="showEndOverlay = true">
          ~ Fin ~
        </button>
      </div>
    </div>

    <!-- ending overlay: the exits out of a finished path (BD7 + BD9 gallery) -->
    <div v-if="showEndOverlay" class="vn-player__ending">
      <DramaExitHub
        :drama-title="drama.title"
        :progress="dramaProgress"
        :current-session-id="session?.id ?? null"
        :default-mode="adaptDefaultMode"
        :adapting-to-arc="props.adaptingToArc"
        :exporting-format="exportingFormat"
        :error-message="exportError || null"
        @replay="handleReplay"
        @adapt="handleAdaptRequested"
        @export="handleExportTranscript"
        @gallery="handleGallery"
        @dismiss-error="exportError = ''"
        @exit="handleExit"
      />
    </div>

    <!-- input area (hidden on ending) -->
    <footer v-if="session && !reachedEnd" class="vn-player__input-bar">
      <div class="vn-player__input-row">
        <textarea
          v-model="playerInput"
          class="field-textarea"
          :disabled="loading"
          rows="2"
          :placeholder="t('branchingDrama.player.inputPlaceholder')"
          @keydown="handleKeydown"
        />
        <button
          class="vn-player__send"
          :disabled="loading || !playerInput.trim()"
          @click="handleInteract"
        >
          {{ loading ? '…' : t('common.actions.submit') }}
        </button>
      </div>
      <!-- 明碼標價：說一句和推進一段是兩個不同的價格，各自標在自己的鈕旁邊。
           查不到價格（自架、按用量計費的方案）時兩個節點都不輸出。 -->
      <div class="vn-player__price-row">
        <ActionPriceHint
          :action-key="ACTION_BRANCHING_DRAMA_INTERACT"
          tooltip-key="credits.price.dramaInteractTooltip"
          variant="chip"
        />
      </div>
      <button
        class="vn-player__advance"
        :class="{
          'vn-player__advance--hinted': !!advanceHint,
          'vn-player__advance--final': atFinalBeat,
        }"
        :disabled="loading"
        @click="handleAdvance"
      >
        {{ advanceButtonText }}
      </button>
      <div class="vn-player__price-row">
        <ActionPriceHint
          :action-key="ACTION_BRANCHING_DRAMA_ADVANCE"
          tooltip-key="credits.price.dramaAdvanceTooltip"
          variant="chip"
        />
      </div>
    </footer>

    <!-- initial loading -->
    <div v-if="!session && loading" class="vn-player__init-loading">
      {{ t('branchingDrama.player.starting') }}
    </div>
  </div>
</template>

<style scoped>
.vn-player {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  border-radius: 8px;
  background: var(--color-bg);
}

.vn-player__bg {
  position: absolute;
  inset: 0;
  background-size: cover;
  background-position: center;
  z-index: 0;
}
.vn-player__bg::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(
    to bottom,
    rgba(0, 0, 0, 0.3) 0%,
    rgba(0, 0, 0, 0.6) 50%,
    rgba(0, 0, 0, 0.85) 100%
  );
}
.vn-player__bg-fallback {
  width: 100%;
  height: 100%;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 40%, #0f3460 100%);
}

.vn-player__header {
  position: relative;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(6px);
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.vn-player__exit {
  background: none;
  border: none;
  color: rgba(255, 255, 255, 0.6);
  cursor: pointer;
  font-size: 13px;
  padding: 4px 8px;
  border-radius: 4px;
}
.vn-player__exit:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.08);
}
.vn-player__title {
  flex: 1;
  font-weight: 600;
  font-size: 15px;
  color: rgba(255, 255, 255, 0.9);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vn-player__depth {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
  white-space: nowrap;
}

.vn-player__regen-group {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

/* Padding, pill width and the expanded card's positioning are the badge's own
   `inline` variant (see CloudCreditsBadge.vue) — reaching in with :deep() was
   how the card ended up growing this header. What is left here is this row's
   business: don't let the flex line squeeze the pill. */
.vn-player__credits {
  flex-shrink: 0;
}

.vn-player__regen-alert,
.vn-player__regen-notice {
  position: relative;
  z-index: 3;
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 8px 16px 0;
  padding: 10px 14px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.65);
  border: 1px solid rgba(255, 255, 255, 0.14);
  color: rgba(255, 255, 255, 0.85);
  font-size: 13px;
  backdrop-filter: blur(6px);
}
.vn-player__regen-notice {
  flex-direction: column;
  align-items: stretch;
}
.vn-player__regen-alert span {
  flex: 1;
}
.vn-player__regen-dismiss {
  background: none;
  border: none;
  color: rgba(255, 255, 255, 0.6);
  cursor: pointer;
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 4px;
  align-self: flex-end;
}
.vn-player__regen-dismiss:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.08);
}
.vn-player__regen-dismiss:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.vn-player__notice-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.vn-player__dialogue-scroll {
  position: relative;
  z-index: 2;
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.vn-player__bubble {
  max-width: 85%;
  padding: 10px 14px;
  border-radius: 12px;
  animation: fadeSlide 0.3s ease;
}
@keyframes fadeSlide {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.vn-player__bubble--narration {
  align-self: flex-start;
  background: rgba(255, 255, 255, 0.08);
  backdrop-filter: blur(4px);
  border: 1px solid rgba(255, 255, 255, 0.1);
}
.vn-player__bubble--player {
  align-self: flex-end;
  background: rgba(var(--color-primary-rgb), 0.18);
  border: 1px solid rgba(var(--color-primary-rgb), 0.3);
}
.vn-player__bubble-label {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.55);
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.vn-player__bubble-text {
  font-size: 14px;
  line-height: 1.7;
  color: rgba(255, 255, 255, 0.92);
  white-space: pre-wrap;
}

.vn-player__tone-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.vn-player__tone-tag[data-tone='dark'] {
  background: rgba(190, 24, 93, 0.25);
  color: #f472b6;
}
.vn-player__tone-tag[data-tone='sunny'] {
  background: rgba(234, 179, 8, 0.25);
  color: #fde047;
}
.vn-player__tone-tag[data-tone='neutral'] {
  background: rgba(148, 163, 184, 0.25);
  color: #cbd5e1;
}

.vn-player__loading {
  text-align: center;
  color: rgba(255, 255, 255, 0.5);
  font-size: 13px;
  padding: 12px;
  animation: pulse 1.5s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}

.vn-player__end-prompt {
  display: flex;
  justify-content: center;
  padding: 16px 0 8px;
}
.vn-player__btn--end {
  background: rgba(var(--color-primary-rgb), 0.2);
  border: 1px solid rgba(var(--color-primary-rgb), 0.5);
  color: var(--color-primary-light);
  padding: 12px 32px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 16px;
  font-weight: 300;
  letter-spacing: 0.15em;
  transition: background 0.2s;
}
.vn-player__btn--end:hover {
  background: rgba(var(--color-primary-rgb), 0.35);
}

.vn-player__ending {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.8);
  backdrop-filter: blur(8px);
  animation: fadeIn 0.6s ease;
}
@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
.vn-player__btn {
  background: rgba(var(--color-primary-rgb), 0.25);
  border: 1px solid rgba(var(--color-primary-rgb), 0.55);
  color: var(--color-primary-light);
  padding: 10px 20px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
}
.vn-player__btn:hover {
  background: rgba(var(--color-primary-rgb), 0.35);
}
.vn-player__input-bar {
  position: relative;
  z-index: 2;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(6px);
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}
.vn-player__input-row {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}
.vn-player__input-row .field-textarea {
  flex: 1;
  resize: none;
  line-height: 1.5;
}
/* Empty when the price cannot be stated honestly — the hint renders nothing
   at all there, and an empty flex row with no gap collapses to zero height. */
.vn-player__price-row {
  display: flex;
  justify-content: flex-end;
}
.vn-player__send {
  background: rgba(var(--color-primary-rgb), 0.3);
  border: 1px solid rgba(var(--color-primary-rgb), 0.55);
  color: var(--color-primary-light);
  padding: 8px 16px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  white-space: nowrap;
  align-self: stretch;
}
.vn-player__send:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.vn-player__send:hover:not(:disabled) {
  background: rgba(var(--color-primary-rgb), 0.4);
}
.vn-player__advance {
  background: rgba(var(--color-primary-rgb), 0.12);
  border: 1px solid rgba(var(--color-primary-rgb), 0.3);
  color: rgba(var(--color-primary-rgb), 0.7);
  padding: 8px 16px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.25s ease;
}
.vn-player__advance:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.vn-player__advance:hover:not(:disabled) {
  background: rgba(var(--color-primary-rgb), 0.2);
  border-color: rgba(var(--color-primary-rgb), 0.5);
}
.vn-player__advance--hinted {
  background: rgba(var(--color-primary-rgb), 0.2);
  border-color: rgba(var(--color-primary-rgb), 0.55);
  color: var(--color-primary-light);
  animation: hintPulse 2s ease-in-out infinite;
}
@keyframes hintPulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(var(--color-primary-rgb), 0); }
  50% { box-shadow: 0 0 12px 2px rgba(var(--color-primary-rgb), 0.2); }
}
.vn-player__advance--final {
  background: rgba(234, 179, 8, 0.15);
  border-color: rgba(234, 179, 8, 0.4);
  color: #fde047;
}
.vn-player__advance--final:hover:not(:disabled) {
  background: rgba(234, 179, 8, 0.25);
  border-color: rgba(234, 179, 8, 0.6);
}
.vn-player__bubble--exchange {
  border-left: 2px solid rgba(var(--color-primary-rgb), 0.3);
}

.vn-player__init-loading {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: center;
  color: rgba(255, 255, 255, 0.6);
  font-size: 16px;
  animation: pulse 1.5s infinite;
}

@media (max-width: 768px) {
  .vn-player__bubble {
    max-width: 95%;
  }
  .vn-player__dialogue-scroll {
    padding: 10px;
    gap: 8px;
  }
  .vn-player__input-bar {
    padding: 8px 10px;
  }
}
</style>
