<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
  type ComponentPublicInstance,
} from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { listCharacters } from '@/utils/api/characters'
import { clampSeedPrompt, parseCastQuery } from '@/utils/fusionSeed'
import { takeStudioSeed } from '@/utils/studioSeedTransfer'
import { notification } from 'ant-design-vue'
import {
  adaptDramaSessionToArc,
  createBranchingDrama,
  deleteBranchingDrama,
  getBranchingDrama,
  getDramaGallery,
  listBranchingDramas,
  listSessions,
  regenerateSceneImage,
} from '@/utils/api/branchingDrama'
import { isInsufficientCreditsError } from '@/utils/api/insufficientCredits'
import { useBillingNotice } from '@/composables/useBillingNotice'
import { refreshCloudCreditsAfterAction } from '@/composables/useCloudCredits'
import CloudCreditsBadge from '@/components/CloudCreditsBadge.vue'
import type { Character } from '@/types/character'
import type { TemplateDraftPayload } from '@/types/arcTemplateIntake'
import type { FusionToArcOperatorMode } from '@/types/fusionStory'
import type {
  BranchingDrama,
  BranchingDramaSummary,
  BranchingDramaStatus,
  DramaOperatorPosition,
  DramaSceneGallery,
  DramaSession,
  DramaVisualStyle,
} from '@/types/branchingDrama'
import {
  DEFAULT_DRAMA_OPERATOR_POSITION,
  DRAMA_OPERATOR_NOTE_MAX_CHARS,
  DRAMA_OPERATOR_POSITIONS,
  DRAMA_VISUAL_STYLES,
  MAX_DRAMA_TOTAL_SEGMENTS,
  MIN_DRAMA_TOTAL_SEGMENTS,
} from '@/types/branchingDrama'
import {
  chooseVisualStyle,
  defaultVisualStyleForCast,
  initialVisualStyleSelection,
  syncVisualStyleWithCast,
} from '@/utils/dramaVisualStyle'
import { UiButton, UiProgressRing, UiSelect, UiTextarea } from '@/components/ui'
import CharacterMultiSelect from '@/components/fusion-story/CharacterMultiSelect.vue'
import BranchingDramaStatusBadge from '@/components/branching-drama/BranchingDramaStatusBadge.vue'
import BranchingDramaPlayer from '@/components/branching-drama/BranchingDramaPlayer.vue'
import DramaSceneGalleryPanel from '@/components/branching-drama/DramaSceneGallery.vue'
import ArcTemplateIntakeWizard from '@/components/ArcTemplateIntakeWizard.vue'
import StudioCreatorPanel from '@/components/studio/StudioCreatorPanel.vue'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { formatDateTime } from '@/i18n/formatters'
import { resolveSceneImageUrl } from '@/utils/sceneImage'
import { createGalleryFetchGuard } from '@/utils/dramaGallery'
import {
  buildDramaProgress,
  dramaCompletionRingRatio,
  formatDramaCompletionPercent,
  type DramaProgress,
} from '@/utils/dramaProgress'
import { isInsufficientCreditsFailure } from '@/utils/studioFailure'
import InsufficientCreditsNotice from '@/components/InsufficientCreditsNotice.vue'
import ActionPriceHint from '@/components/ActionPriceHint.vue'
import {
  ACTION_BRANCHING_DRAMA_ADVANCE,
  ACTION_BRANCHING_DRAMA_CREATE,
  ACTION_BRANCHING_DRAMA_SCENE_REGEN,
} from '@/composables/useActionPricing'

const { t } = useI18n()
const { locale } = useLocale()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()
const route = useRoute()
const router = useRouter()

const characters = ref<Character[]>([])
const dramas = ref<BranchingDramaSummary[]>([])
const selectedDrama = ref<BranchingDrama | null>(null)
const selectedCharacterIds = ref<string[]>([])
const promptText = ref('')
const totalSegments = ref(6)
const operatorPosition = ref<DramaOperatorPosition>(
  DEFAULT_DRAMA_OPERATOR_POSITION,
)
const operatorNote = ref('')
/**
 * BD10 art direction. Held as `{ style, touched }` rather than a bare ref
 * so the cast can pre-select a look without ever overruling a player who
 * picked one — see `@/utils/dramaVisualStyle`.
 */
const visualStyleSelection = ref(initialVisualStyleSelection())
const visualStyle = computed<DramaVisualStyle>({
  get: () => visualStyleSelection.value.style,
  set: (value) => { visualStyleSelection.value = chooseVisualStyle(value) },
})
const errorMessage = ref('')
const creating = ref(false)
/**
 * The create press was refused, not broken (FX2). Both refusals leave the
 * form exactly as the player filled it in — nothing ran and nothing was
 * charged — so they render as their own notice next to the button rather
 * than in the red `bd-page__alert` box, which reads as "the Studio is down".
 */
const createBilling = useBillingNotice()
const {
  outOfCredits: createOutOfCredits,
  priceChanged: createPriceChanged,
} = createBilling
const sidebarOpen = ref(false)
const playing = ref(false)
const resumeSessionId = ref<string | null>(null)
const sessions = ref<DramaSession[]>([])
/**
 * Bumped on every 「開始／換條路再走」 so the VN player remounts. Starting a
 * fresh session by clearing `resumeSessionId` alone would not restart a
 * player that is already on screen — the prop lands a tick after the press
 * and the component would re-enter the session it just finished (BD7).
 */
const playSeq = ref(0)
/** BD7 — the ending's 「把這條路寫成劇本」 and the wizard it hands off to. */
const adaptingToArc = ref(false)
const adaptedDraft = ref<TemplateDraftPayload | null>(null)
let pollHandle: number | null = null

const inStudio = computed(() => route.matched.some(record => record.name === 'studio'))
const backTarget = computed(() => inStudio.value ? { name: 'studio-authoring' } : '/')

const isBusy = computed(() => {
  if (!selectedDrama.value) return false
  const s = selectedDrama.value.status
  return s !== 'ready' && s !== 'failed'
})

const isReady = computed(
  () => selectedDrama.value?.status === 'ready',
)

/**
 * Generation is 202 + poll, so an exhausted balance can only surface as a
 * code on the failed drama. It is the one failure the player can fix, so it
 * gets the shared top-up card in place of the raw error line; unknown codes
 * and plain crashes keep the existing generic detail.
 */
const outOfCredits = computed(
  () => isInsufficientCreditsFailure(selectedDrama.value),
)

// `generating_outlines` only ever produces `initial_node_target` nodes
// (root + prefetch layers) before the drama is handed back to the
// player — the full-tree `expected_node_count` is realized lazily much
// later, while playing, so it is the wrong denominator here and pins the
// bar at ~1% for the entire outline phase (BD5).
const progressPercent = computed(() => {
  if (!selectedDrama.value || !isBusy.value) return 0
  const d = selectedDrama.value
  if (d.initial_node_target <= 0) return 0
  return Math.min(
    Math.round((d.generated_node_count / d.initial_node_target) * 100),
    99,
  )
})

const progressLabel = computed(() => {
  if (!selectedDrama.value) return ''
  const d = selectedDrama.value
  if (d.status === 'generating_outlines') {
    return t('branchingDrama.page.progressOutlines', {
      generated: d.generated_node_count,
      expected: d.initial_node_target,
    })
  }
  if (d.status === 'generating_images') {
    return t('branchingDrama.status.generatingImages')
  }
  return ''
})

const titleScreenImageUrl = computed(() => {
  if (!selectedDrama.value?.first_scene_image_path) return null
  return resolveSceneImageUrl(selectedDrama.value.first_scene_image_path)
})

/**
 * Redrawing the title image (BD6). Offered as soon as the tree is playable
 * and the root node exists — including when the picture is missing, which
 * is the state a renderer outage at creation leaves behind and the one no
 * automatic retry ever repairs.
 */
const regeneratingTitle = ref(false)
const titleRegenError = ref('')
const titleRegenOutOfCredits = ref(false)

const canRegenerateTitle = computed(
  () => isReady.value && !!selectedDrama.value?.first_scene_node_id,
)

async function handleRegenerateTitleImage() {
  const drama = selectedDrama.value
  const nodeId = drama?.first_scene_node_id
  if (!drama || !nodeId || regeneratingTitle.value) return
  regeneratingTitle.value = true
  titleRegenError.value = ''
  titleRegenOutOfCredits.value = false
  try {
    const node = await regenerateSceneImage(drama.id, nodeId)
    // The backend hands every redraw a fresh object key, so writing the
    // returned path straight back is what makes the browser fetch the new
    // picture instead of re-showing the cached one.
    selectedDrama.value = { ...drama, first_scene_image_path: node.image_path }
    refreshCloudCreditsAfterAction()
  } catch (err: unknown) {
    if (isInsufficientCreditsError(err)) {
      titleRegenOutOfCredits.value = true
    } else {
      titleRegenError.value =
        err instanceof Error
          ? err.message
          : t('branchingDrama.page.errors.regenFailed')
    }
  } finally {
    regeneratingTitle.value = false
  }
}

/**
 * 劇場圖集 (BD9) — the pictures this drama has painted, as a collection.
 *
 * Offered only once a playthrough exists: before the first press every tile
 * would be a silhouette, which reads as a broken grid rather than as
 * something to fill in. `sessions` is loaded alongside the drama itself and
 * already gates the save-slot list, so it is the same signal.
 *
 * Fetched on open rather than with the drama: it is a side trip most visits
 * do not take, and its denominator moves as lazy generation paints deeper
 * layers, so a copy captured at selection time would be stale by the time
 * anyone looked at it.
 */
const galleryOpen = ref(false)
const gallery = ref<DramaSceneGallery | null>(null)
const galleryLoading = ref(false)
const galleryError = ref('')
/**
 * One gallery request at a time owns these refs. Without it, a read issued
 * against a drama the player has since left still lands and paints that
 * drama's tiles under the current title (FX5-1).
 */
const galleryFetch = createGalleryFetchGuard()

const canShowGallery = computed(
  () => isReady.value && sessions.value.length > 0,
)

/**
 * 完成度 (D8.1) — how much of the drama's tree these playthroughs have
 * covered.
 *
 * Derived, not fetched: `sessions` and `total_segments` are already on this
 * page, so there is no request to guard and nothing for `resetGallery` to
 * clear. Selecting another drama replaces both refs and clearing the
 * selection empties them, so this recomputes or goes `null` on its own — a
 * stale read cannot survive a switch the way a gallery response can.
 */
const dramaProgress = computed<DramaProgress | null>(() => {
  const drama = selectedDrama.value
  if (!drama) return null
  return buildDramaProgress(sessions.value, drama.total_segments)
})

/**
 * D8.4 — the ring on the gallery entry button. Reads off the same
 * `dramaProgress.completion` the panel header prints (never a separate
 * fetch), so the ring and the number next to it can never disagree about
 * which drama they describe. `0` — an empty ring — whenever there is no
 * honest percentage yet, same rule `DramaSceneGallery`'s header line uses.
 *
 * **Not** `completion.percent / 100` (FX3): `percent` is rounded to one
 * decimal for reading, and on a deep tree that rounding is the whole ring —
 * a 12-act tree's first ten costume-change loops all round to `0.0`, so the
 * ring never left the track no matter how many times the player replayed.
 * `dramaCompletionRingRatio` divides the raw counts and floors a nonzero
 * result to a visible sliver instead.
 */
const galleryEntryRatio = computed(() => {
  const completion = dramaProgress.value?.completion
  if (!completion) return 0
  return dramaCompletionRingRatio(completion)
})

/**
 * The percentage printed under the label, or `null` when there is no honest
 * one — in which case the button shows its name alone rather than a
 * fabricated `0%`.
 *
 * Deliberately *not* a picture count. The gallery's own count is
 * `walked ∩ nodes that have an image_path` (`build_scene_gallery`), a strict
 * intersection: with `KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH=0`, on a self-host
 * with no scene image port wired, or simply while the fire-and-forget
 * prefetch is still running, walked nodes far outnumber painted ones. That
 * number only exists once `openGallery` has answered and this button has to
 * render before then — so it states the completion the ring is already
 * drawing and claims nothing about pictures.
 *
 * Same `toFixed(1)` as `DramaSceneGallery`'s header line, off the same
 * `dramaProgress.completion`, so entry and panel can never disagree.
 */
const galleryEntryPercent = computed<string | null>(() => {
  const completion = dramaProgress.value?.completion
  if (!completion || completion.total <= 0) return null
  return formatDramaCompletionPercent(completion)
})

const galleryEntryAriaLabel = computed(() => {
  const percent = galleryEntryPercent.value
  if (percent === null) {
    return t('branchingDrama.gallery.open')
  }
  return t('branchingDrama.gallery.entryAria', { percent })
})

/**
 * 「這一輪」 on the drama's own page (BD14): the playthrough touched most
 * recently, which after `handleExitPlayer` is exactly the one just walked.
 *
 * Read off `updated_at` rather than trusting list order —— the repository
 * does sort newest-first today, but a branching graph that highlights the
 * wrong path is a silent lie, and this is two lines. Parsed rather than
 * compared as strings so a row whose timestamp is formatted differently
 * cannot win by sorting high; an unparseable one simply never wins.
 */
const latestSessionId = computed<string | null>(() => {
  let bestId: string | null = null
  let bestAt = Number.NEGATIVE_INFINITY
  for (const sess of sessions.value) {
    const at = Date.parse(sess.updated_at)
    if (!Number.isFinite(at) || at <= bestAt) continue
    bestAt = at
    bestId = sess.id
  }
  return bestId
})

/**
 * The panel itself, so the ending's 「場景圖集」 can put it in front of the
 * player. Coming from a button press inside the VN, landing on a page whose
 * gallery sits below the fold reads as "nothing happened".
 */
const galleryPanel = ref<ComponentPublicInstance | null>(null)

function resetGallery() {
  // Orphan any read still in the air before clearing: it belongs to the
  // drama being left, and its answer must not refill what this clears.
  galleryFetch.invalidate()
  galleryOpen.value = false
  gallery.value = null
  galleryLoading.value = false
  galleryError.value = ''
}

async function openGallery() {
  const drama = selectedDrama.value
  if (!drama) return
  const token = galleryFetch.begin()
  galleryOpen.value = true
  galleryLoading.value = true
  galleryError.value = ''
  try {
    const next = await getDramaGallery(drama.id)
    if (!galleryFetch.isCurrent(token)) return
    gallery.value = next
  } catch (err: unknown) {
    // Keep whatever was already collected on screen: a failed refresh is
    // not a reason to blank a grid the player is looking at.
    if (!galleryFetch.isCurrent(token)) return
    galleryError.value =
      err instanceof Error
        ? err.message
        : t('branchingDrama.gallery.errors.loadFailed')
  } finally {
    // Only the current request may clear the spinner: a superseded read
    // finishing late must not cancel the loading state of the one that
    // replaced it.
    if (galleryFetch.isCurrent(token)) galleryLoading.value = false
  }
}

/**
 * Why the segment count on screen cannot be sent as-is.
 *
 * The `<input type="number">` `min`/`max` attributes only constrain the
 * spinner — a typed 40 sails through `v-model.number` and comes back as a
 * Pydantic 422 whose English body the player was never meant to read. So the
 * bound is stated here too, in their language, before anything is sent.
 */
const segmentRangeError = computed(() => {
  const value = totalSegments.value
  if (Number.isInteger(value)
    && value >= MIN_DRAMA_TOTAL_SEGMENTS
    && value <= MAX_DRAMA_TOTAL_SEGMENTS) {
    return ''
  }
  return t('branchingDrama.page.segmentRangeError', {
    min: MIN_DRAMA_TOTAL_SEGMENTS,
    max: MAX_DRAMA_TOTAL_SEGMENTS,
  })
})

const canCreate = computed(() => {
  if (creating.value) return false
  if (selectedCharacterIds.value.length < 2) return false
  if (segmentRangeError.value) return false
  return promptText.value.trim().length > 0
})

const operatorPositionOptions = computed(() =>
  DRAMA_OPERATOR_POSITIONS.map((value) => ({
    value,
    label: t(`branchingDrama.page.operatorPosition.options.${value}.label`),
  })),
)

/** The one-line explanation under the picker, for the current choice. */
const operatorPositionHint = computed(() =>
  t(
    `branchingDrama.page.operatorPosition.options.${operatorPosition.value}.hint`,
  ),
)

const visualStyleOptions = computed(() =>
  DRAMA_VISUAL_STYLES.map((value) => ({
    value,
    label: t(`branchingDrama.page.visualStyle.options.${value}.label`),
  })),
)

/**
 * The line under the picker. While the player has not touched it, it says
 * where the suggestion came from — otherwise a pre-selected 寫實 looks like
 * a setting they forgot they changed.
 */
const visualStyleHint = computed(() => {
  const chosen = t(
    `branchingDrama.page.visualStyle.options.${visualStyle.value}.hint`,
  )
  if (visualStyleSelection.value.touched) return chosen
  return `${chosen}${t('branchingDrama.page.visualStyle.followsCast')}`
})

/**
 * Suggest the first cast member's look until the player says otherwise.
 *
 * Watching the *suggestion* rather than the cast means the handler runs
 * only when the answer would actually move — reordering a cast whose first
 * member did not change is not a reason to touch the picker.
 */
watch(
  () => defaultVisualStyleForCast(
    selectedCharacterIds.value, characters.value,
  ),
  () => {
    visualStyleSelection.value = syncVisualStyleWithCast(
      visualStyleSelection.value,
      selectedCharacterIds.value,
      characters.value,
    )
  },
)

/**
 * The look the drama was actually made in (BD10). Empty for a drama
 * created before the slot existed — those still style themselves off their
 * first character, so naming a look here would be a guess printed as a
 * fact, and the row is hidden instead.
 */
const selectedVisualStyleLabel = computed(() => {
  const style = selectedDrama.value?.visual_style
  if (!style) return ''
  return t(`branchingDrama.page.visualStyle.options.${style}.label`)
})

/**
 * The detail view reads the stored value, which is always one of the
 * three — a pre-BD2 drama comes back as `central` from the backend
 * mapper, so there is no "unset" case to render.
 */
const selectedPositionLabel = computed(() => {
  const position = selectedDrama.value?.operator_position
  if (!position) return ''
  return t(`branchingDrama.page.operatorPosition.options.${position}.label`)
})

const segmentWarning = computed(() => {
  // An out-of-range value is answered by `segmentRangeError`; running the
  // 3^N fan-out on it would print an astronomical node count next to it.
  if (segmentRangeError.value) return ''
  if (totalSegments.value < 9) return ''
  const count = (3 ** totalSegments.value - 1) / 2
  return t('branchingDrama.page.segmentWarning', {
    segments: totalSegments.value,
    count: Math.round(count),
  })
})

async function refreshLists() {
  try {
    const [charList, dramaList] = await Promise.all([
      listCharacters(),
      listBranchingDramas(),
    ])
    characters.value = charList
    dramas.value = dramaList
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('common.errors.loadFailed', { reason: t('common.errors.unknown') })
  }
}

async function refreshSelected(silent = false) {
  if (!selectedDrama.value) return
  try {
    const next = await getBranchingDrama(selectedDrama.value.id)
    selectedDrama.value = next
    const idx = dramas.value.findIndex((d) => d.id === next.id)
    if (idx >= 0) {
      dramas.value[idx] = {
        id: next.id,
        character_ids: next.character_ids,
        title: next.title,
        total_segments: next.total_segments,
        status: next.status,
        error_message: next.error_message,
        error_code: next.error_code ?? null,
        created_at: next.created_at,
        updated_at: next.updated_at,
      }
    }
  } catch (err: unknown) {
    if (!silent) {
      errorMessage.value =
        err instanceof Error ? err.message : t('branchingDrama.page.errors.updateFailed')
    }
  }
}

function startPolling() {
  stopPolling()
  pollHandle = window.setInterval(() => {
    if (selectedDrama.value && isBusy.value) {
      void refreshSelected(true)
    }
  }, 2000)
}

function stopPolling() {
  if (pollHandle != null) {
    window.clearInterval(pollHandle)
    pollHandle = null
  }
}

async function selectDrama(summary: BranchingDramaSummary) {
  errorMessage.value = ''
  createBilling.clear()
  sidebarOpen.value = false
  playing.value = false
  resumeSessionId.value = null
  titleRegenError.value = ''
  titleRegenOutOfCredits.value = false
  resetGallery()
  try {
    const [drama, sessList] = await Promise.all([
      getBranchingDrama(summary.id),
      summary.status === 'ready'
        ? listSessions(summary.id)
        : Promise.resolve([]),
    ])
    selectedDrama.value = drama
    sessions.value = sessList
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('branchingDrama.page.errors.readFailed')
  }
}

function clearSelection() {
  selectedDrama.value = null
  createBilling.clear()
  sidebarOpen.value = false
  playing.value = false
  resumeSessionId.value = null
  sessions.value = []
  titleRegenError.value = ''
  titleRegenOutOfCredits.value = false
  resetGallery()
}

async function handleCreate() {
  if (!canCreate.value) return
  errorMessage.value = ''
  createBilling.clear()
  creating.value = true
  try {
    const created = await createBranchingDrama({
      character_ids: selectedCharacterIds.value,
      prompt: promptText.value.trim(),
      total_segments: totalSegments.value,
      operator_position: operatorPosition.value,
      operator_note: operatorNote.value.trim() || null,
      visual_style: visualStyle.value,
    })
    selectedDrama.value = created
    selectedCharacterIds.value = []
    promptText.value = ''
    totalSegments.value = 6
    operatorPosition.value = DEFAULT_DRAMA_OPERATOR_POSITION
    operatorNote.value = ''
    // Back to following the cast: the next drama is a new question, and
    // the previous answer is not the player's opinion about it.
    visualStyleSelection.value = initialVisualStyleSelection()
    await refreshLists()
    refreshCloudCreditsAfterAction()
  } catch (err: unknown) {
    // The charge is raised before the 202, so two of the answers here are
    // decisions rather than faults. Rendering either as 「建立失敗」 would
    // tell a player whose balance is simply empty that the Studio is broken.
    if (await createBilling.absorb(err)) return
    errorMessage.value =
      err instanceof Error ? err.message : t('branchingDrama.page.errors.createFailed')
  } finally {
    creating.value = false
  }
}

async function handleDelete(summary: BranchingDramaSummary) {
  if (!await confirmDialog({
    content: t('branchingDrama.page.confirmDelete', { title: summary.title }),
    okText: t('common.actions.delete'),
    danger: true,
  })) {
    return
  }
  try {
    await deleteBranchingDrama(summary.id)
    if (selectedDrama.value?.id === summary.id) {
      selectedDrama.value = null
      playing.value = false
    }
    await refreshLists()
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('branchingDrama.page.errors.deleteFailed')
  }
}

function startNewGame() {
  resumeSessionId.value = null
  playSeq.value += 1
  playing.value = true
}

function resumeGame(sessionId: string) {
  resumeSessionId.value = sessionId
  playSeq.value += 1
  playing.value = true
}

/**
 * Convert the line this player walked into an unsaved arc draft (BD7).
 *
 * Failure is reported as a notification rather than through `error`: that
 * handler boots the player back to the list, which is a wildly
 * disproportionate answer to 「改編失敗」 at the moment they finished a
 * playthrough. The ending stays on screen and the button is pressable again.
 */
async function handleAdaptToArc(payload: {
  sessionId: string
  mode: FusionToArcOperatorMode
}) {
  if (!selectedDrama.value || adaptingToArc.value) return
  adaptingToArc.value = true
  try {
    adaptedDraft.value = await adaptDramaSessionToArc(
      selectedDrama.value.id,
      payload.sessionId,
      { operator_mode: payload.mode },
    )
  } catch (err: unknown) {
    notification.error({
      message: err instanceof Error
        ? err.message
        : t('branchingDrama.exitHub.errors.adaptFailed'),
      duration: 4,
    })
  } finally {
    adaptingToArc.value = false
  }
}

function handleAdaptedTemplateSaved() {
  notification.success({
    message: t('branchingDrama.exitHub.adaptSaved'),
    duration: 4,
  })
  adaptedDraft.value = null
}

async function handleExitPlayer() {
  playing.value = false
  resumeSessionId.value = null
  if (selectedDrama.value) {
    try {
      sessions.value = await listSessions(selectedDrama.value.id)
    } catch { /* ignore */ }
    // A playthrough is exactly what changes the gallery — the beats just
    // walked became collected, and the prefetch painted more behind them.
    // Coming back to the counts from before the run would be plain wrong.
    if (galleryOpen.value) await openGallery()
  }
}

/**
 * 「場景圖集」 from the ending overlay (BD9 入口，BD7 出口).
 *
 * The collection belongs to the drama rather than to the playthrough that
 * just ended, and it is painted on the drama's page — so this leaves the VN
 * first, through the same exit that already refreshes the session list and a
 * gallery left open behind it. Only a *closed* gallery still needs the read;
 * asking for one right after `handleExitPlayer` refreshed it would be the
 * same request twice.
 */
async function handleGalleryFromEnding() {
  await handleExitPlayer()
  const opening = galleryOpen.value ? null : openGallery()
  // `openGallery` flips the flag before it awaits anything, so the panel is
  // mounted by the next tick and can be scrolled to while its tiles load.
  await nextTick()
  const el = galleryPanel.value?.$el as HTMLElement | undefined
  el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  await opening
}

function formatSessionDate(iso: string): string {
  return formatDateTime(iso, locale.value, timeZone.value)
}

function statusOf(status: BranchingDramaStatus): BranchingDramaStatus {
  return status
}

function charNamesFor(ids: string[]): string {
  return ids
    .map((id) => characters.value.find((c) => c.id === id)?.name ?? '?')
    .join(t('common.listSeparator'))
}

/**
 * Shared entry seam (from a fusion story's "換個玩法" exit): grab a
 * pending seed handoff before ANY API call fires. In-app entrances use
 * the in-memory stash (the seed quotes user prose, which must never
 * ride the URL into Referer / history / access logs); a
 * `?seedPrompt=&cast=` query is kept as a fallback for canned deep
 * links — its keys are stripped here, synchronously, ahead of the first
 * same-origin request.
 */
function captureSeedHandoff(): { seedPrompt: string; cast: string[] } | null {
  const stashed = takeStudioSeed()
  if (stashed) {
    return { seedPrompt: stashed.seedPrompt, cast: stashed.cast ?? [] }
  }
  const rawPrompt = route.query.seedPrompt
  const rawCast = route.query.cast
  const seedPrompt = typeof rawPrompt === 'string' ? rawPrompt : ''
  const rawCastStr = typeof rawCast === 'string' ? rawCast : ''
  if (!seedPrompt && !rawCastStr) return null
  const next: Record<string, string> = {}
  for (const [key, value] of Object.entries(route.query)) {
    if (key === 'seedPrompt' || key === 'cast') continue
    if (typeof value === 'string') next[key] = value
  }
  void router.replace({ query: next })
  return {
    seedPrompt,
    cast: rawCastStr ? rawCastStr.split(',').map((s) => s.trim()) : [],
  }
}

/** Apply a captured handoff once the owned character list is loaded. */
function applySeedHandoff(handoff: { seedPrompt: string; cast: string[] }) {
  if (handoff.seedPrompt) {
    promptText.value = clampSeedPrompt(handoff.seedPrompt)
  }
  const ownedIds = characters.value.map((c) => c.id)
  const cast = parseCastQuery(handoff.cast.join(','), ownedIds)
  if (cast.length) selectedCharacterIds.value = cast
}

onMounted(async () => {
  // Capture (and strip) any seed handoff before the first API call so
  // seed text never rides the Referer header (see captureSeedHandoff).
  const handoff = captureSeedHandoff()
  await refreshLists()
  if (handoff) applySeedHandoff(handoff)
  startPolling()
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="bd-page" :class="{ 'is-playing': playing, 'is-embedded': inStudio }">
    <header v-if="!playing" class="bd-page__topbar">
      <div class="bd-page__brand">
        <RouterLink :to="backTarget" class="bd-page__back" :aria-label="t('branchingDrama.page.back')">
          &larr;
          <span class="bd-page__back-label">{{ t('branchingDrama.page.back') }}</span>
        </RouterLink>
        <h1>{{ t('branchingDrama.page.title') }}</h1>
        <!-- 螢火餘額：self-host／未知餘額時徽章自身不輸出任何節點，
             這裡不必另外包 v-if。`inline` 讓展開的明細卡浮在標題列下方，
             不把整排標題撐高。 -->
        <CloudCreditsBadge variant="inline" class="bd-page__credits" />
        <button
          class="bd-page__menu-btn"
          :aria-expanded="sidebarOpen"
          :aria-label="t('branchingDrama.page.toggleHistory')"
          @click="sidebarOpen = !sidebarOpen"
        >
          {{ sidebarOpen ? '✕' : '☰' }}
          <span class="bd-page__menu-count">{{ dramas.length }}</span>
        </button>
      </div>
      <div class="bd-page__hint">
        {{ t('branchingDrama.page.hint') }}
      </div>
    </header>

    <div v-if="errorMessage && !playing" class="bd-page__alert">
      {{ errorMessage }}
    </div>

    <!-- VN player mode -->
    <BranchingDramaPlayer
      v-if="playing && selectedDrama"
      :key="`${selectedDrama.id}:${resumeSessionId ?? 'new'}:${playSeq}`"
      :drama="selectedDrama"
      :characters="characters"
      :resume-session-id="resumeSessionId"
      :sessions="sessions"
      :adapting-to-arc="adaptingToArc"
      @exit="handleExitPlayer"
      @error="(m) => { errorMessage = m; handleExitPlayer() }"
      @adapt-requested="handleAdaptToArc"
      @replay-requested="startNewGame"
      @gallery-requested="handleGalleryFromEnding"
    />

    <!-- normal list + creator layout -->
    <div v-if="!playing" class="bd-page__layout">
      <div
        v-if="sidebarOpen"
        class="bd-page__scrim"
        @click="sidebarOpen = false"
      />
      <aside class="bd-page__sidebar" :class="{ 'is-open': sidebarOpen }">
        <div class="bd-page__sidebar-head">
          <h2>{{ t('branchingDrama.page.history') }}</h2>
          <UiButton size="sm" @click="clearSelection">
            {{ t('branchingDrama.page.newDrama') }}
          </UiButton>
        </div>
        <ul class="bd-page__drama-list">
          <li
            v-for="drama in dramas"
            :key="drama.id"
            class="bd-page__drama"
            :class="{ 'is-selected': selectedDrama?.id === drama.id }"
          >
            <button class="bd-page__drama-btn" @click="selectDrama(drama)">
              <div class="bd-page__drama-title">{{ drama.title }}</div>
              <div class="bd-page__drama-meta">
                <BranchingDramaStatusBadge :status="statusOf(drama.status)" />
                <span>{{ t('branchingDrama.page.segmentCountCompact', { count: drama.total_segments }) }}</span>
              </div>
              <div class="bd-page__drama-chars">
                {{ charNamesFor(drama.character_ids) }}
              </div>
            </button>
            <button
              class="bd-page__drama-del"
              :title="t('common.actions.delete')"
              @click="handleDelete(drama)"
            >
              &times;
            </button>
          </li>
          <li v-if="!dramas.length" class="bd-page__empty">
            {{ t('branchingDrama.page.emptyDramas') }}
          </li>
        </ul>
      </aside>

      <main class="bd-page__main">
        <!-- detail view for a selected drama -->
        <section
          v-if="selectedDrama"
          class="bd-page__detail"
          :class="{ 'is-ready': isReady }"
        >
          <div
            class="bd-page__title-screen"
            :class="{ 'has-scene-image': titleScreenImageUrl }"
            :style="titleScreenImageUrl ? { '--bd-title-image': `url(${titleScreenImageUrl})` } : undefined"
          >
            <div class="bd-page__title-copy">
              <p class="spark-label">{{ t('branchingDrama.page.titleScreenEyebrow') }}</p>
              <h2 class="display-title display-title--gradient">{{ selectedDrama.title }}</h2>
              <div class="bd-page__detail-meta">
                <BranchingDramaStatusBadge :status="statusOf(selectedDrama.status)" />
                <span>{{ t('branchingDrama.page.segmentCount', { count: selectedDrama.total_segments }) }}</span>
                <span>{{ t('branchingDrama.page.nodeCount', { count: selectedDrama.expected_node_count }) }}</span>
              </div>
              <!-- 重繪標題圖：沒生出來的補得回來，不喜歡的換得掉。價格自成一格。 -->
              <div v-if="canRegenerateTitle" class="bd-page__title-regen">
                <UiButton
                  variant="ghost"
                  size="sm"
                  :loading="regeneratingTitle"
                  :disabled="regeneratingTitle"
                  :title="t('branchingDrama.page.regenSceneTooltip')"
                  @click="handleRegenerateTitleImage"
                >
                  {{ regeneratingTitle
                    ? t('branchingDrama.page.regeneratingScene')
                    : t('branchingDrama.page.regenScene') }}
                </UiButton>
                <ActionPriceHint
                  :action-key="ACTION_BRANCHING_DRAMA_SCENE_REGEN"
                  tooltip-key="credits.price.dramaSceneRegenTooltip"
                  variant="chip"
                />
              </div>
            </div>
          </div>
          <InsufficientCreditsNotice
            v-if="titleRegenOutOfCredits"
            class="bd-page__credits-notice"
          />
          <p v-else-if="titleRegenError" class="bd-page__error-detail">
            {{ titleRegenError }}
          </p>
          <p v-if="selectedDrama.warning" class="bd-page__warning">
            {{ selectedDrama.warning }}
          </p>
          <InsufficientCreditsNotice
            v-if="outOfCredits"
            class="bd-page__credits-notice"
          />
          <p
            v-else-if="selectedDrama.error_message"
            class="bd-page__error-detail"
          >
            {{ selectedDrama.error_message }}
          </p>
          <div class="bd-page__detail-prompt">
            <label>{{ t('branchingDrama.page.promptLabel') }}</label>
            <div>{{ selectedDrama.prompt }}</div>
          </div>
          <div class="bd-page__detail-chars">
            <label>{{ t('branchingDrama.page.castLabel') }}</label>
            <div>{{ charNamesFor(selectedDrama.character_ids) }}</div>
          </div>
          <div class="bd-page__detail-chars">
            <label>{{ t('branchingDrama.page.operatorPosition.label') }}</label>
            <div>{{ selectedPositionLabel }}</div>
          </div>
          <div v-if="selectedDrama.operator_note" class="bd-page__detail-chars">
            <label>{{ t('branchingDrama.page.operatorNote.label') }}</label>
            <div>{{ selectedDrama.operator_note }}</div>
          </div>
          <!-- 沒有 visual_style 的是 BD10 之前建的劇場：它們照第一位角色
               的風格畫，這裡寫任何一個名字都是猜的，所以整列不顯示。 -->
          <div v-if="selectedVisualStyleLabel" class="bd-page__detail-chars">
            <label>{{ t('branchingDrama.page.visualStyle.label') }}</label>
            <div>{{ selectedVisualStyleLabel }}</div>
          </div>
          <div v-if="isReady" class="bd-page__detail-actions">
            <UiButton class="bd-page__new-game" variant="hero" size="lg" @click="startNewGame">
              {{ t('branchingDrama.page.newGame') }}
            </UiButton>
            <!-- 開始一場會寫出開場那一段，照推進計價（FX1）——所以這裡標的是
                 推進價，不是建劇場價。查不到價格時不輸出任何節點。 -->
            <ActionPriceHint
              :action-key="ACTION_BRANCHING_DRAMA_ADVANCE"
              tooltip-key="credits.price.dramaStartTooltip"
              variant="chip"
            />
            <!-- 圖集入口 (D8.4)：玩過至少一場才出現。一場都沒玩時整面都是
                 鎖定格，那不是收藏，只是一片問號。免費，讀已經畫好的東西。
                 環形進度與下方的百分比同出一個 dramaProgress，所以永遠同步；
                 這裡不寫張數——圖集裡實際有幾張要等 openGallery 回來才知道，
                 而這顆鈕必須在那之前就畫出來。它是次要入口，不跟左邊的
                 「新遊戲」hero 鈕搶視覺重量，所以走自訂 scoped 按鈕而非
                 UiButton 的任何 variant。 -->
            <button
              v-if="canShowGallery && !galleryOpen"
              type="button"
              class="bd-page__gallery-entry"
              :aria-label="galleryEntryAriaLabel"
              @click="openGallery"
            >
              <UiProgressRing :ratio="galleryEntryRatio" :size="30" :thickness="3" />
              <span class="bd-page__gallery-entry-text">
                <span class="bd-page__gallery-entry-label">
                  {{ t('branchingDrama.gallery.open') }}
                </span>
                <span
                  v-if="galleryEntryPercent !== null"
                  class="bd-page__gallery-entry-percent"
                >
                  {{ t('branchingDrama.gallery.entryPercent', { percent: galleryEntryPercent }) }}
                </span>
              </span>
            </button>
          </div>

          <DramaSceneGalleryPanel
            v-if="galleryOpen"
            ref="galleryPanel"
            class="bd-page__gallery"
            :gallery="gallery"
            :progress="dramaProgress"
            :current-session-id="latestSessionId"
            :loading="galleryLoading"
            :error-message="galleryError || null"
            @close="galleryOpen = false"
          />

          <!-- session history -->
          <div v-if="isReady && sessions.length > 0" class="bd-page__sessions">
            <label>{{ t('branchingDrama.page.sessionsLabel') }}</label>
            <ul class="bd-page__session-list">
              <li
                v-for="(sess, index) in sessions"
                :key="sess.id"
                class="bd-page__session-item"
              >
                <button class="bd-page__session-btn" @click="resumeGame(sess.id)">
                  <span class="bd-page__session-slot">
                    {{ t('branchingDrama.page.sessionSlot', { index: sessions.length - index }) }}
                  </span>
                  <span class="bd-page__session-status" :data-status="sess.status">
                    {{ sess.status === 'playing' ? t('branchingDrama.page.sessionPlaying') : t('branchingDrama.page.sessionEnded') }}
                  </span>
                  <span class="bd-page__session-progress">
                    {{ t('branchingDrama.page.sessionProgress', { current: sess.turns.length, total: selectedDrama!.total_segments }) }}
                  </span>
                  <span class="bd-page__session-date">
                    {{ formatSessionDate(sess.updated_at) }}
                  </span>
                </button>
              </li>
            </ul>
          </div>

          <div v-if="isBusy" class="bd-page__progress">
            <div class="bd-page__progress-label">{{ progressLabel }}</div>
            <div class="bd-page__progress-bar">
              <div
                class="bd-page__progress-fill"
                :style="{ width: `${progressPercent}%` }"
              />
            </div>
            <div class="bd-page__progress-pct">{{ progressPercent }}%</div>
            <div class="bd-page__progress-note">
              {{ t('branchingDrama.page.progressNote') }}
            </div>
          </div>
        </section>

        <!-- creator form -->
        <StudioCreatorPanel
          v-else
          class="bd-page__creator"
          :eyebrow="t('studio.creatorPanel.eyebrow')"
          :title="t('branchingDrama.page.createTitle')"
          :notice="t('branchingDrama.page.notice')"
        >
          <div class="bd-page__field">
            <label class="field-label">{{ t('branchingDrama.page.castLabel') }}</label>
            <CharacterMultiSelect
              v-model="selectedCharacterIds"
              :characters="characters"
              :min="2"
              :max="5"
            />
          </div>
          <div class="bd-page__field">
            <label class="field-label">{{ t('branchingDrama.page.promptLabel') }}</label>
            <textarea
              v-model="promptText"
              class="field-textarea"
              rows="4"
              :placeholder="t('branchingDrama.page.promptPlaceholder')"
            />
          </div>
          <div class="bd-page__field">
            <UiSelect
              v-model="operatorPosition"
              :label="t('branchingDrama.page.operatorPosition.label')"
              :options="operatorPositionOptions"
              :hint="operatorPositionHint"
            />
          </div>
          <div class="bd-page__field">
            <UiSelect
              v-model="visualStyle"
              :label="t('branchingDrama.page.visualStyle.label')"
              :options="visualStyleOptions"
              :hint="visualStyleHint"
            />
          </div>
          <div class="bd-page__field">
            <UiTextarea
              v-model="operatorNote"
              :label="t('branchingDrama.page.operatorNote.label')"
              :placeholder="t('branchingDrama.page.operatorNote.placeholder')"
              :hint="t('branchingDrama.page.operatorNote.hint')"
              :rows="2"
              :maxlength="DRAMA_OPERATOR_NOTE_MAX_CHARS"
            />
          </div>
          <div class="bd-page__field bd-page__field--inline">
            <label class="field-label">{{ t('branchingDrama.page.totalSegmentsLabel') }}</label>
            <input
              v-model.number="totalSegments"
              type="number"
              class="field-input"
              :min="MIN_DRAMA_TOTAL_SEGMENTS"
              :max="MAX_DRAMA_TOTAL_SEGMENTS"
            />
          </div>
          <p v-if="segmentRangeError" class="bd-page__error-detail">
            {{ segmentRangeError }}
          </p>
          <p v-if="segmentWarning" class="bd-page__warning">
            {{ segmentWarning }}
          </p>
          <div class="bd-page__actions">
            <UiButton
              variant="hero"
              :disabled="!canCreate"
              :loading="creating"
              @click="handleCreate"
            >
              {{ creating ? t('branchingDrama.page.creating') : t('branchingDrama.page.createAction') }}
            </UiButton>
            <!-- 一口價：分歧樹與前幾張場景圖都在這個數字裡，按之前就看得到。
                 開場那一段照推進計價，另外標在開始鈕旁邊。
                 查不到價格（自架、按用量計費的方案）時不輸出任何節點。 -->
            <ActionPriceHint
              class="bd-page__price"
              :action-key="ACTION_BRANCHING_DRAMA_CREATE"
              tooltip-key="credits.price.dramaCreateTooltip"
              variant="chip"
            />
          </div>
          <!-- 沒扣到點就是沒跑：表單原封不動留著，只多一張卡／一行說明。 -->
          <InsufficientCreditsNotice
            v-if="createOutOfCredits"
            class="bd-page__credits-notice"
          />
          <p v-else-if="createPriceChanged" class="bd-page__price-changed" role="status">
            {{ t('credits.price.changed') }}
          </p>
        </StudioCreatorPanel>
      </main>
    </div>
    <!-- BD7 — the arc draft this playthrough became, handed to the same
         review wizard the fusion adaptation opens. Outside the `playing`
         branch on purpose: it is an overlay above the VN, not a step in it. -->
    <ArcTemplateIntakeWizard
      v-if="adaptedDraft"
      :initial-draft="adaptedDraft"
      @saved="handleAdaptedTemplateSaved"
      @close="adaptedDraft = null"
    />
  </div>
</template>

<style scoped>
.bd-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  padding-top: calc(16px + var(--safe-area-top, 0px));
  padding-bottom: calc(16px + var(--safe-area-bottom, 0px));
  padding-left: calc(16px + var(--safe-area-left, 0px));
  padding-right: calc(16px + var(--safe-area-right, 0px));
  height: 100dvh;
  box-sizing: border-box;
  color: var(--color-text);
  background: var(--color-bg);
}

.bd-page.is-embedded:not(.is-playing) {
  height: auto;
  min-height: 0;
  padding: 0;
  background: transparent;
}

.bd-page.is-playing {
  padding: 0;
  gap: 0;
}

.bd-page__topbar {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--color-border);
}
.bd-page__brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.bd-page__brand h1 {
  margin: 0;
  font-size: 20px;
  flex: 1;
  min-width: 0;
}
.bd-page__back {
  color: rgba(255, 255, 255, 0.6);
  text-decoration: none;
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.bd-page__back:hover {
  color: rgba(255, 255, 255, 0.9);
}
/* Padding, pill width and the expanded card's positioning are the badge's own
   `inline` variant (see CloudCreditsBadge.vue) — reaching in with :deep() was
   how the card ended up growing this header row. What is left here is this
   row's business: don't let the flex line squeeze the pill. */
.bd-page__credits {
  flex-shrink: 0;
}
.bd-page__menu-btn {
  display: none;
  align-items: center;
  gap: 6px;
  background: rgba(255, 255, 255, 0.08);
  color: inherit;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 6px;
  padding: 6px 10px;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
}
.bd-page__menu-count {
  font-size: 11px;
  background: rgba(var(--color-primary-rgb), 0.3);
  color: var(--color-primary-light);
  border-radius: 999px;
  padding: 1px 6px;
  min-width: 18px;
  text-align: center;
}
.bd-page__hint {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
}
.bd-page__alert {
  background: rgba(245, 34, 45, 0.15);
  border: 1px solid rgba(245, 34, 45, 0.5);
  color: var(--color-danger);
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
}

.bd-page__layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 16px;
  flex: 1;
  min-height: 0;
  position: relative;
}
.bd-page__scrim {
  display: none;
}

.bd-page__sidebar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
  background: rgba(255, 255, 255, 0.03);
  padding: 12px;
  border-radius: 8px;
  min-height: 0;
}
.bd-page__sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.bd-page__sidebar-head h2 {
  font-size: 14px;
  margin: 0;
  color: rgba(255, 255, 255, 0.75);
}

.bd-page__drama-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.bd-page__drama {
  display: flex;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  overflow: hidden;
}
.bd-page__drama.is-selected {
  border-color: var(--color-primary);
  background: rgba(var(--color-primary-rgb), 0.08);
}
.bd-page__drama-btn {
  flex: 1;
  text-align: left;
  background: transparent;
  border: 0;
  color: inherit;
  padding: 8px 10px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.bd-page__drama-title {
  font-weight: 600;
  font-size: 14px;
}
.bd-page__drama-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.5);
}
.bd-page__drama-chars {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bd-page__drama-del {
  background: transparent;
  border: 0;
  color: rgba(255, 255, 255, 0.4);
  cursor: pointer;
  padding: 0 8px;
  font-size: 16px;
}
.bd-page__drama-del:hover {
  color: var(--color-danger);
}
.bd-page__empty {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.45);
}

.bd-page__main {
  overflow-y: auto;
}

/* detail view */
.bd-page__detail {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 0;
  border: 1px solid rgba(var(--color-primary-rgb), 0.2);
  background: rgba(18, 12, 42, 0.42);
  border-radius: 8px;
  overflow: hidden;
}
.bd-page__title-screen {
  position: relative;
  min-height: 260px;
  padding: var(--space-5);
  display: flex;
  align-items: flex-end;
  background:
    linear-gradient(180deg, transparent 0%, rgba(10, 6, 24, 0.78) 72%, rgba(10, 6, 24, 0.96) 100%),
    radial-gradient(580px 260px at 28% 20%, rgba(var(--color-primary-rgb), 0.28), transparent 72%),
    radial-gradient(520px 220px at 80% 10%, rgba(var(--color-secondary-rgb), 0.18), transparent 72%),
    radial-gradient(circle, rgba(255, 255, 255, 0.22) 0 1px, transparent 1px),
    var(--color-bg-secondary);
  background-size: auto, auto, auto, 44px 44px, auto;
}
.bd-page__title-screen.has-scene-image {
  min-height: 320px;
  background:
    linear-gradient(180deg, rgba(8, 5, 20, 0.08) 0%, rgba(10, 6, 24, 0.36) 48%, rgba(10, 6, 24, 0.92) 100%),
    radial-gradient(520px 240px at 22% 14%, rgba(var(--color-primary-rgb), 0.28), transparent 70%),
    var(--bd-title-image);
  background-position: center, center, center;
  background-size: cover, cover, cover;
}
.bd-page__title-screen.has-scene-image::before {
  content: "";
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, rgba(10, 6, 24, 0.58) 0%, transparent 48%, rgba(10, 6, 24, 0.26) 100%),
    radial-gradient(460px 180px at 24% 80%, rgba(var(--color-spark-rgb), 0.16), transparent 74%);
  pointer-events: none;
}
.bd-page__title-screen.has-scene-image .bd-page__title-copy {
  position: relative;
  z-index: 1;
  max-width: 760px;
  text-shadow: 0 2px 22px rgba(0, 0, 0, 0.62);
}
.bd-page__title-copy {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
}
.bd-page__title-copy h2,
.bd-page__title-copy p {
  margin: 0;
}
.bd-page__title-copy h2 {
  font-size: 42px;
}
.bd-page__title-regen {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.bd-page__detail-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 13px;
  color: rgba(255, 255, 255, 0.6);
}
.bd-page__detail > p,
.bd-page__detail-prompt,
.bd-page__detail-chars,
.bd-page__detail-actions,
.bd-page__sessions,
.bd-page__progress {
  margin-inline: var(--space-5);
}
.bd-page__detail-prompt,
.bd-page__detail-chars {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.bd-page__detail-prompt label,
.bd-page__detail-chars label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}
.bd-page__detail-prompt div,
.bd-page__detail-chars div {
  font-size: 14px;
  line-height: 1.6;
}
/*
  `wrap`: 「新遊戲」＋價格 chip＋圖集入口 stop fitting on one line somewhere
  under 360px (375px still fits). Without this the row overflows its column
  instead; with it the gallery entry simply drops to its own line.
*/
.bd-page__detail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  padding-top: 4px;
}
.bd-page__new-game {
  animation: bd-hero-glow 2.8s ease-in-out infinite;
}
/*
  圖集入口 (D8.4) — a real <button> (keyboard reachable, focus-visible,
  hover state) kept deliberately quiet next to the hero CTA: no fill, no
  glow, just a border and the progress ring doing the talking. It must
  read as "secondary utility", not as a second primary action competing
  with 「新遊戲」.
*/
.bd-page__gallery-entry {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: 6px var(--space-3);
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: 999px;
  color: var(--color-text);
  font-family: inherit;
  cursor: pointer;
  transition: background-color 0.15s, border-color 0.15s;
}
.bd-page__gallery-entry:hover {
  background: rgba(255, 255, 255, 0.07);
  border-color: rgba(255, 255, 255, 0.28);
}
.bd-page__gallery-entry:focus-visible {
  outline: 2px solid var(--color-primary-light);
  outline-offset: 1px;
}
.bd-page__gallery-entry-text {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  line-height: 1.25;
}
.bd-page__gallery-entry-label {
  font-size: var(--font-sm);
  font-weight: 500;
}
/*
  `nowrap`: the percentage is the button's second line, and letting it break
  makes the whole action row taller on 320–360px screens. It is short in all
  three catalogues, so pinning it to one line costs nothing.
*/
.bd-page__gallery-entry-percent {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.bd-page__progress {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: var(--space-5);
}
.bd-page__progress-label {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.7);
  animation: pulse 1.5s infinite;
}
.bd-page__progress-bar {
  height: 6px;
  background: rgba(255, 255, 255, 0.08);
  border-radius: 3px;
  overflow: hidden;
}
.bd-page__progress-fill {
  position: relative;
  height: 100%;
  background: linear-gradient(90deg, var(--color-primary-dark), var(--color-primary-light));
  border-radius: 3px;
  transition: width 0.6s ease;
  min-width: 2px;
  overflow: hidden;
}
.bd-page__progress-fill::after {
  content: "";
  position: absolute;
  inset: 0;
  transform: translateX(-100%);
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.46), transparent);
  animation: bd-progress-shimmer 1.3s ease-in-out infinite;
}
.bd-page__progress-pct {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
  text-align: right;
}
.bd-page__progress-note {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.4);
}
@keyframes pulse {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}
@keyframes bd-hero-glow {
  0%, 100% {
    filter: brightness(1);
  }
  50% {
    filter: brightness(1.12);
  }
}
@keyframes bd-progress-shimmer {
  to {
    transform: translateX(100%);
  }
}
.bd-page__warning {
  font-size: 12px;
  color: #faad14;
  background: rgba(250, 173, 20, 0.1);
  border: 1px solid rgba(250, 173, 20, 0.3);
  padding: 6px 10px;
  border-radius: 4px;
  margin: 0;
}
.bd-page__error-detail {
  font-size: 12px;
  color: var(--color-danger);
  margin: 0;
}
.bd-page__credits-notice {
  margin: 0;
}
/* A moved price is an answer, not a fault — deliberately not danger-red. */
.bd-page__price-changed {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* creator form */
.bd-page__creator {
  align-self: start;
}
.bd-page__field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.bd-page__field--inline {
  flex-direction: row;
  align-items: center;
  gap: 10px;
}
.bd-page__field label {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.7);
}
.bd-page__field .field-textarea {
  width: 100%;
}
.bd-page__field .field-input {
  width: 80px;
  text-align: center;
}
.bd-page__actions {
  display: flex;
  gap: 8px;
}
.bd-page__price {
  align-self: center;
}
.bd-page__gallery {
  margin-top: 4px;
}
.bd-page__sessions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bd-page__sessions > label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}
.bd-page__session-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--space-2);
}
.bd-page__session-item {
  border: 1px solid rgba(var(--color-primary-rgb), 0.18);
  border-radius: 8px;
  background:
    linear-gradient(145deg, rgba(var(--color-primary-rgb), 0.1), rgba(255, 255, 255, 0.025)),
    rgba(18, 12, 42, 0.46);
  overflow: hidden;
  transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}
.bd-page__session-item:hover {
  transform: translateY(-1px);
  border-color: rgba(var(--color-spark-rgb), 0.42);
  box-shadow: 0 0 20px rgba(var(--color-primary-rgb), 0.18);
}
.bd-page__session-btn {
  width: 100%;
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: start;
  gap: 10px;
  padding: var(--space-3);
  background: transparent;
  border: 0;
  color: inherit;
  cursor: pointer;
  font-size: 13px;
  text-align: left;
}
.bd-page__session-btn:hover {
  background: rgba(255, 255, 255, 0.05);
}
.bd-page__session-slot {
  grid-row: span 2;
  min-width: 56px;
  color: var(--color-spark);
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 700;
  line-height: 1.1;
}
.bd-page__session-status {
  display: inline-block;
  width: max-content;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
}
.bd-page__session-status[data-status='playing'] {
  background: rgba(var(--color-primary-rgb), 0.18);
  color: var(--color-primary-light);
}
.bd-page__session-status[data-status='ended'] {
  background: rgba(148, 163, 184, 0.18);
  color: #94a3b8;
}
.bd-page__session-progress {
  color: rgba(255, 255, 255, 0.7);
}
.bd-page__session-date {
  grid-column: 2;
  color: rgba(255, 255, 255, 0.4);
  font-size: 12px;
}

@media (max-width: 768px) {
  .bd-page {
    gap: 8px;
    padding: 10px;
    padding-top: calc(10px + var(--safe-area-top, 0px));
    padding-bottom: calc(10px + var(--safe-area-bottom, 0px));
    padding-left: calc(10px + var(--safe-area-left, 0px));
    padding-right: calc(10px + var(--safe-area-right, 0px));
  }
  .bd-page.is-embedded:not(.is-playing) {
    padding: 0;
  }
  .bd-page__brand h1 {
    font-size: 17px;
  }
  .bd-page__back-label {
    display: none;
  }
  .bd-page__menu-btn {
    display: inline-flex;
  }
  .bd-page__hint {
    font-size: 11px;
    line-height: 1.5;
  }
  .bd-page__layout {
    grid-template-columns: 1fr;
  }
  .bd-page__sidebar {
    position: fixed;
    top: 0;
    left: 0;
    width: min(82vw, 320px);
    height: 100vh;
    height: 100dvh;
    z-index: 45;
    transform: translateX(-110%);
    transition: transform 180ms ease;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6);
    background: var(--color-bg-secondary);
    padding-top: calc(12px + var(--safe-area-top, 0px));
    padding-bottom: calc(12px + var(--safe-area-bottom, 0px));
    padding-left: calc(12px + var(--safe-area-left, 0px));
  }
  .bd-page__sidebar.is-open {
    transform: translateX(0);
  }
  .bd-page__scrim {
    display: block;
    position: fixed;
    top: 0;
    left: 0;
    width: 100vw;
    height: 100vh;
    height: 100dvh;
    background: rgba(0, 0, 0, 0.88);
    backdrop-filter: blur(2px);
    z-index: 35;
  }
  .bd-page__title-screen {
    min-height: 220px;
    padding: var(--space-4);
  }
  .bd-page__title-copy h2 {
    font-size: 30px;
  }
  .bd-page__detail > p,
  .bd-page__detail-prompt,
  .bd-page__detail-chars,
  .bd-page__detail-actions,
  .bd-page__sessions,
  .bd-page__progress {
    margin-inline: var(--space-4);
  }
  .bd-page__actions {
    flex-direction: column;
  }
}

@media (prefers-reduced-motion: reduce) {
  .bd-page__new-game,
  .bd-page__progress-label,
  .bd-page__progress-fill::after {
    animation: none;
  }

  .bd-page__session-item,
  .bd-page__session-item:hover {
    transform: none;
    transition: none;
  }
}
</style>
