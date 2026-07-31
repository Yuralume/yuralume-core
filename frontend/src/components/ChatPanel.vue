<script setup lang="ts">
import { computed, ref, nextTick, watch, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePlayerCopy } from '@/composables/usePlayerCopy'
import { BulbOutlined, CloseOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import type { Character } from '@/types/character'
import type { ChatMessage } from '@/types/chat'
import type { ChatAssistSuggestion } from '@/types/chatAssist'
import { webDmPresenceFrame, webStagePresenceFrame } from '@/types/chat'
import type { ScheduleActivity } from '@/types/schedule'
import {
  ChatRuntimeLimitError,
  ChatStreamProtocolError,
  sendChatMessageStream,
  uploadChatAttachments,
  undoLastTurn,
} from '@/utils/api/chat'
import { isInsufficientCreditsError } from '@/utils/api/insufficientCredits'
import { isPriceChangedError } from '@/utils/api/priceChanged'
import { creditAmountText } from '@/utils/creditsFormat'
import { suggestChatAssistMessages } from '@/utils/api/chatAssist'
import { getCharacter } from '@/utils/api/characters'
import { notification } from 'ant-design-vue'
import { getCurrentActivity } from '@/utils/api/schedule'
import ChatBubble from '@/components/ChatBubble.vue'
import ChatAssistDiscoveryHint from '@/components/ChatAssistDiscoveryHint.vue'
import ChatFirstTurnGuide from '@/components/ChatFirstTurnGuide.vue'
import ActionPriceHint from '@/components/ActionPriceHint.vue'
import InsufficientCreditsNotice from '@/components/InsufficientCreditsNotice.vue'
import NsfwModeAtmosphere from '@/components/NsfwModeAtmosphere.vue'
import { UiButton } from '@/components/ui'
import { useChatAssistPreference } from '@/composables/useChatAssistPreference'
import { useAuth } from '@/composables/useAuth'
import {
  refreshCloudCreditsAfterAction,
  useCloudCredits,
} from '@/composables/useCloudCredits'
import {
  ACTION_CHAT,
  ACTION_IMAGE_CHAT_TOOL,
  useActionPricing,
} from '@/composables/useActionPricing'
import { useNsfwMode } from '@/composables/useNsfwMode'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { formatTimeRange } from '@/i18n/formatters'
import { characterDisplayRef } from '@/utils/characterDisplay'
import { splitAssistantBubbles } from '@/utils/chatSegments'
import { shouldSendChatInputOnKeydown } from '@/utils/chatInputKeys'
import { resolveTTSAvailability } from '@/utils/ttsAvailability'
import {
  isChatAssistDiscovered,
  isChatAssistHintDismissed,
  rememberChatAssistDiscovered,
  rememberChatAssistHintDismissed,
  shouldShowChatAssistHint,
} from '@/utils/chatAssistDiscovery'

const { t, locale } = useI18n()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()
const { chatAssistEnabled, loadChatAssistPreference } = useChatAssistPreference()
const { cloudMode, portalUrl } = useAuth()
const { pt } = usePlayerCopy()
const cloudCredits = useCloudCredits()
const actionPricing = useActionPricing()
// A turn that was refused for lack of credits shows the shared notice card in
// the message stream instead of a generic "chat failed" bubble.
const creditsExhausted = ref(false)
/**
 * Price of the turn we refused to send, when the refusal came from the local
 * pre-check rather than from the server's 402 (plan AP2). Null keeps the
 * notice card exactly as the server-refusal path has always rendered it.
 */
const creditsRequiredCr = ref<number | null>(null)
/**
 * "…and a picture is extra." The chat price covers everything it takes to
 * answer one message *except* a picture drawn along the way, which is charged
 * as its own action — so the composer discloses both numbers rather than
 * letting the second one turn up in the ledger unannounced.
 *
 * Fail-soft like every other price surface: no cloud mode, no published image
 * price, or tiers that disagree, and the line simply is not there.
 */
const chatImageExtraText = computed(() => {
  if (!cloudMode.value) return null
  const price = actionPricing.priceOf(ACTION_IMAGE_CHAT_TOOL)
  if (!price) return null
  return pt('credits.price.chatImageExtra', {
    amount: creditAmountText(t, price.price_cr),
  })
})
/**
 * Hosted trial chat hit its message cap. On self-host this state is
 * unreachable (there is no trial), so the existing error bubble is left
 * untouched there and this card is hosted-only — it replaces copy that used
 * to tell a paying player to "self-host or bring your own API key".
 */
const demoLimitReached = ref(false)
const {
  active: nsfwModeActive,
  loadNsfwMode,
  startNsfwModeClock,
  stopNsfwModeClock,
} = useNsfwMode()

const props = defineProps<{
  character: Character | null
  conversationId: string | null
  messages: ChatMessage[]
  loadingHistory?: boolean
  // 桌面 landscape 版面偏好 toggle：只在非 portrait 時顯示（由
  // StagePage 決定是否傳入 true）。StagePage 持有 stageLayout 狀態，
  // ChatPanel 純顯示 + emit 事件，不在此另開一份 localStorage 讀寫。
  showLayoutToggle?: boolean
  stageLayoutMode?: 'stage-centric' | 'chat-centric'
}>()

const emit = defineEmits<{
  conversationUpdate: [convId: string, msgs: ChatMessage[], char: Character]
  // Fires the moment the stream hands us a conversation id — parent
  // should stash it WITHOUT touching ``messages``. Overwriting messages
  // here races with the in-flight optimistic push and can end up
  // duplicating the user's bubble when the watcher fires back into
  // localMessages mid-send.
  conversationIdLearned: [convId: string]
  toggleStageLayout: []
}>()

const inputText = ref('')
const sending = ref(false)
const messagesContainer = ref<HTMLElement>()
const textareaRef = ref<HTMLTextAreaElement>()
const fileInputRef = ref<HTMLInputElement>()
const localMessages = ref<ChatMessage[]>([])
const streamingText = ref('')
const ttsAvailable = ref(false)
const revealingMessageIndex = ref<number | null>(null)
const currentActivity = ref<ScheduleActivity | null>(null)
const currentActivityLoading = ref(false)
const chatAssistOpen = ref(false)
const chatAssistLoading = ref(false)
const chatAssistError = ref<string | null>(null)
const chatAssistSuggestions = ref<ChatAssistSuggestion[]>([])
const chatAssistCharacterId = ref<string | null>(null)
const chatAssistDiscovered = ref(isChatAssistDiscovered(getChatAssistDiscoveryStorage()))
const chatAssistHintDismissed = ref(isChatAssistHintDismissed(getChatAssistDiscoveryStorage()))
const composingInput = ref(false)
let chatAssistRequestSeq = 0
let pendingRevealResolve: (() => void) | null = null
let pendingFirstRevealRelease: (() => void) | null = null
let nextSendingLockId = 0
let activeSendingLockId: number | null = null

function beginSendingLock(): number {
  nextSendingLockId += 1
  activeSendingLockId = nextSendingLockId
  sending.value = true
  return activeSendingLockId
}

function releaseSendingLock(lockId: number) {
  if (activeSendingLockId !== lockId) return
  activeSendingLockId = null
  sending.value = false
}

type ChatInteractionMode = 'stage' | 'dm'
// Both surfaces are the player's to pick, with no gate in between (plan
// SA, D1): switching is local state only — no request, no wait, no charge.
// DM stays the opening default because a first message is the gentler
// entry, not because same-space has to be earned.
const interactionMode = ref<ChatInteractionMode>('dm')

const panelClass = computed(() => [
  'chat-panel',
  interactionMode.value === 'dm' ? 'chat-panel--dm' : 'chat-panel--stage',
])

const revealInProgress = computed(() => revealingMessageIndex.value !== null)

const characterDisplayName = computed(() => (
  characterDisplayRef(props.character, t('common.character'))
))

const chatAssistHintVisible = computed(() => shouldShowChatAssistHint({
  enabled: chatAssistEnabled.value,
  assistOpen: chatAssistOpen.value,
  hasMessages: localMessages.value.length > 0,
  inputEmpty: inputText.value.trim() === '',
  discovered: chatAssistDiscovered.value,
  dismissed: chatAssistHintDismissed.value,
}))

const modeStatusLabel = computed(() => (
  interactionMode.value === 'dm'
    ? t('chat.mode.dmStatus')
    : t('chat.mode.stageStatus')
))

// 桌面 landscape 版面偏好 toggle 文案：依「目前狀態」描述「點下去會
// 切到哪一態」，而非描述目前狀態本身 —— 對齊按鈕慣例（動作導向文案）。
const stageLayoutToggleLabel = computed(() => (
  props.stageLayoutMode === 'chat-centric'
    ? t('stage.layout.toggleToStageCentric')
    : t('stage.layout.toggleToChatCentric')
))

const stageLayoutToggleAria = computed(() => (
  props.stageLayoutMode === 'chat-centric'
    ? t('stage.layout.toggleAriaToStageCentric')
    : t('stage.layout.toggleAriaToChatCentric')
))

const emptyMessage = computed(() => (
  interactionMode.value === 'dm'
    ? t('chat.history.emptyDm')
    : t('chat.history.empty')
))

const inputPlaceholder = computed(() => {
  if (!props.character) return t('chat.input.placeholderDefault')
  return interactionMode.value === 'dm'
    ? t('chat.input.placeholderDmWithName', { name: props.character.name })
    : t('chat.input.placeholderWithName', { name: props.character.name })
})

async function useStarterMessage(message: string) {
  inputText.value = message
  await nextTick()
  autoResizeTextarea()
  textareaRef.value?.focus()
}

async function useChatAssistSuggestion(message: string) {
  await useStarterMessage(message)
  chatAssistOpen.value = false
}

const stageTabSubtitle = computed(() => {
  if (currentActivityLoading.value && !currentActivity.value) {
    return t('chat.mode.stagePreparing', { name: characterDisplayName.value })
  }
  return t('chat.mode.stageHint')
})

// Switching surface is a local state change and nothing else (plan SA,
// D1). With no gate to consult there is no in-between "checking..." state
// to render and no refusal to explain: same-space is the player declaring
// where they are, and whether that fits the character's day is answered
// inside the reply, by the character.
function selectInteractionMode(mode: ChatInteractionMode) {
  interactionMode.value = mode
  focusInput()
}

function currentPresenceFrame(hasAttachments: boolean) {
  return interactionMode.value === 'stage'
    ? webStagePresenceFrame(hasAttachments)
    : webDmPresenceFrame(hasAttachments)
}

// Files the user has picked but not yet sent. Each entry = one image
// staged for the *next* turn; a local ``preview`` blob URL lets us
// thumbnail without waiting for the upload round-trip.
interface StagedAttachment {
  file: File
  preview: string
}
const stagedAttachments = ref<StagedAttachment[]>([])
const uploadError = ref<string | null>(null)
const MAX_ATTACHMENTS_PER_TURN = 4

// Action menu (⋯) — collapses attach + undo into one trigger so the
// input row doesn't get crowded on narrow screens.
const actionMenuOpen = ref(false)
function toggleActionMenu() {
  actionMenuOpen.value = !actionMenuOpen.value
}
function closeActionMenu() {
  actionMenuOpen.value = false
}
function handleAttachClick() {
  closeActionMenu()
  pickFiles()
}
function handleUndoClick() {
  closeActionMenu()
  handleUndoLastTurn()
}

function handleChatAssistClick() {
  closeActionMenu()
  markChatAssistDiscovered()
  chatAssistOpen.value = true
  const characterId = props.character?.id ?? null
  if (
    characterId
    && (
      chatAssistCharacterId.value !== characterId
      || chatAssistSuggestions.value.length === 0
      || chatAssistError.value
    )
  ) {
    loadChatAssistSuggestions()
  }
}

async function loadChatAssistSuggestions() {
  const characterId = props.character?.id
  if (!characterId || chatAssistLoading.value) return
  const seq = ++chatAssistRequestSeq
  chatAssistOpen.value = true
  chatAssistLoading.value = true
  chatAssistError.value = null
  try {
    const response = await suggestChatAssistMessages(characterId, 4)
    if (seq !== chatAssistRequestSeq || props.character?.id !== characterId) return
    chatAssistCharacterId.value = characterId
    chatAssistSuggestions.value = response.suggestions
  } catch (error) {
    if (seq !== chatAssistRequestSeq) return
    chatAssistError.value = error instanceof Error
      ? t('common.errorWithDetail', { message: t('chat.assist.loadFailed'), detail: error.message })
      : t('chat.assist.loadFailed')
  } finally {
    if (seq === chatAssistRequestSeq) {
      chatAssistLoading.value = false
    }
  }
}

function closeChatAssist() {
  chatAssistOpen.value = false
}

function getChatAssistDiscoveryStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function markChatAssistDiscovered() {
  rememberChatAssistDiscovered(getChatAssistDiscoveryStorage())
  chatAssistDiscovered.value = true
}

function dismissChatAssistHint() {
  rememberChatAssistHintDismissed(getChatAssistDiscoveryStorage())
  chatAssistHintDismissed.value = true
}

// Minimal click-outside directive — closes the action menu when the
// user taps elsewhere. Kept inline (vs a shared util) because this is
// the only consumer.
const vClickOutside = {
  mounted(el: HTMLElement, binding: { value: () => void }) {
    const handler = (event: Event) => {
      if (!el.contains(event.target as Node)) binding.value()
    }
    ;(el as HTMLElement & { _clickOutside?: EventListener })._clickOutside = handler
    document.addEventListener('mousedown', handler)
    document.addEventListener('touchstart', handler, { passive: true })
  },
  unmounted(el: HTMLElement) {
    const handler = (el as HTMLElement & { _clickOutside?: EventListener })._clickOutside
    if (handler) {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('touchstart', handler)
    }
  },
}

function pickFiles() {
  uploadError.value = null
  fileInputRef.value?.click()
}

function onFilesSelected(event: Event) {
  const target = event.target as HTMLInputElement
  const files = Array.from(target.files ?? [])
  target.value = ''  // let the same file be re-picked after removal
  if (files.length === 0) return
  stageFiles(files)
}

function removeStagedAttachment(index: number) {
  const item = stagedAttachments.value[index]
  if (item) URL.revokeObjectURL(item.preview)
  stagedAttachments.value.splice(index, 1)
  uploadError.value = null
}

function stageFiles(files: File[]) {
  // Shared with file-input and clipboard-paste paths. Keeps the
  // per-turn cap + error behaviour in one place.
  const slots = MAX_ATTACHMENTS_PER_TURN - stagedAttachments.value.length
  if (slots <= 0) {
    uploadError.value = t('chat.input.attachOverLimit', { n: MAX_ATTACHMENTS_PER_TURN })
    return
  }
  for (const file of files.slice(0, slots)) {
    stagedAttachments.value.push({
      file,
      preview: URL.createObjectURL(file),
    })
  }
  if (files.length > slots) {
    uploadError.value = t('chat.input.attachOverLimitTrimmed', { n: MAX_ATTACHMENTS_PER_TURN })
  } else {
    uploadError.value = null
  }
}

function onPaste(event: ClipboardEvent) {
  // Pull image/* entries out of the clipboard. ``files`` works for
  // most modern browsers; we iterate ``items`` as a fallback for
  // clipboards that only expose items (some Firefox variants).
  if (sending.value) return
  const cd = event.clipboardData
  if (!cd) return

  const images: File[] = []
  if (cd.files && cd.files.length > 0) {
    for (const f of Array.from(cd.files)) {
      if (f.type.startsWith('image/')) images.push(f)
    }
  }
  if (images.length === 0 && cd.items) {
    for (const item of Array.from(cd.items)) {
      if (item.kind !== 'file') continue
      if (!item.type.startsWith('image/')) continue
      const file = item.getAsFile()
      if (file) {
        // Clipboard images have no filename; give them one so the
        // server-side extension check accepts them.
        const ext = file.type.split('/')[1] || 'png'
        const named = new File([file], `pasted.${ext}`, { type: file.type })
        images.push(named)
      }
    }
  }

  if (images.length === 0) return
  // We have at least one image — block the default "paste as text"
  // so the textarea doesn't get flooded with a huge data URL.
  event.preventDefault()
  stageFiles(images)
}

// Undo the most recent turn. Pops the last user + assistant pair,
// rolls back memory / state / goals / arc / schedule via the
// TurnJournal snapshot on the server, then asks the parent to
// refetch so the visual state matches.
const undoing = ref(false)
async function handleUndoLastTurn() {
  if (!props.character || !props.conversationId || undoing.value || sending.value) return
  if (localMessages.value.length < 2) {
    notification.info({
      message: t('chat.actions.undoNoneTitle'),
      description: t('chat.actions.undoNoneDesc'),
      duration: 2.5,
    })
    return
  }
  if (!await confirmDialog({
    title: t('chat.actions.undoConfirmTitle'),
    content: t('chat.actions.undoConfirm', { name: characterDisplayName.value }),
    okText: t('chat.actions.undoConfirmAction'),
  })) return
  undoing.value = true
  try {
    const summary = await undoLastTurn(props.conversationId)
    // Strip the last two bubbles locally so the UI reacts instantly;
    // parent will re-sync from the server right after.
    const trimmed = localMessages.value.slice(0, -summary.reverted_messages)
    localMessages.value = trimmed
    notification.success({
      message: t('chat.actions.undoSuccessTitle'),
      duration: 2.5,
    })
    // Fetch the rolled-back character from the server so emotion /
    // affection badges reflect the restore, then push everything
    // upstream as one update. Falling back to the stale props
    // character keeps the UX usable if the character refetch fails.
    let freshChar: Character
    try {
      freshChar = await getCharacter(props.character.id)
    } catch {
      freshChar = props.character
    }
    emit('conversationUpdate', props.conversationId, trimmed, freshChar)
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error)
    notification.error({
      message: t('chat.actions.undoFailedTitle'),
      description: msg,
      duration: 4,
    })
  } finally {
    undoing.value = false
  }
}

// Refresh the current-activity badge every 60s so it stays in sync as
// the character moves between scheduled blocks.
let activityTimer: ReturnType<typeof setInterval> | null = null

async function refreshCurrentActivity() {
  if (!props.character) {
    currentActivity.value = null
    return
  }
  currentActivityLoading.value = true
  try {
    const snapshot = await getCurrentActivity(props.character.id)
    currentActivity.value = snapshot.current
  } catch {
    currentActivity.value = null
  } finally {
    currentActivityLoading.value = false
  }
}

function formatActivityTime(activity: ScheduleActivity): string {
  return formatTimeRange(
    activity.start_at,
    activity.end_at,
    locale.value,
    timeZone.value,
  )
}

watch(() => props.messages, (msgs) => {
  localMessages.value = [...msgs]
  scrollToBottom()
}, { immediate: true })

watch(() => props.character?.id ?? null, (characterId) => {
  focusInput()
  chatAssistRequestSeq += 1
  chatAssistOpen.value = false
  chatAssistLoading.value = false
  chatAssistError.value = null
  chatAssistSuggestions.value = []
  chatAssistCharacterId.value = characterId
  if (activityTimer) {
    clearInterval(activityTimer)
    activityTimer = null
  }
  if (characterId) {
    interactionMode.value = 'dm'
    refreshCurrentActivity()
    activityTimer = setInterval(() => {
      refreshCurrentActivity()
    }, 60_000)
  } else {
    currentActivity.value = null
  }
}, { immediate: true })

watch(chatAssistEnabled, (enabled) => {
  if (!enabled) {
    chatAssistOpen.value = false
  }
})

onUnmounted(() => {
  if (activityTimer) clearInterval(activityTimer)
  if (pendingRevealResolve) {
    pendingRevealResolve()
    pendingRevealResolve = null
  }
  pendingFirstRevealRelease = null
})

function waitForMessageReveal(index: number, onFirstReveal: () => void): Promise<void> {
  revealingMessageIndex.value = index
  pendingFirstRevealRelease = onFirstReveal
  return new Promise(resolve => {
    pendingRevealResolve = resolve
  })
}

function handleBubbleRevealComplete(index: number) {
  if (revealingMessageIndex.value !== index) return
  revealingMessageIndex.value = null
  if (pendingFirstRevealRelease) {
    pendingFirstRevealRelease()
    pendingFirstRevealRelease = null
  }
  if (pendingRevealResolve) {
    pendingRevealResolve()
    pendingRevealResolve = null
  }
}

function handleBubbleRevealProgress(index: number) {
  if (revealingMessageIndex.value !== index) return
  if (pendingFirstRevealRelease) {
    pendingFirstRevealRelease()
    pendingFirstRevealRelease = null
  }
  void scrollToBottom()
}

/**
 * The wallet as the pre-check sees it. Reads the shared badge snapshot — no
 * extra request on the send path — and stays deliberately timid: an unknown
 * or stale balance never refuses a turn (see `shortfallFor`).
 */
function currentBalanceView() {
  return {
    total: cloudCredits.total.value,
    known: cloudCredits.hasBalance.value,
    stale: cloudCredits.stale.value,
  }
}

async function handleSend() {
  if (!props.character || sending.value) return
  const hasText = inputText.value.trim().length > 0
  const hasImages = stagedAttachments.value.length > 0
  if (!hasText && !hasImages) return

  // AP2 pre-check: with a fixed, published price we can tell the player the
  // turn will not go through *before* their sentence disappears into a send
  // that ends in a 402. Runs before anything is cleared, so the text they
  // wrote is still in the box when they come back from topping up.
  const shortfall = actionPricing.shortfallFor(ACTION_CHAT, currentBalanceView())
  if (shortfall !== null) {
    creditsRequiredCr.value = shortfall
    creditsExhausted.value = true
    await scrollToBottom()
    return
  }

  const userText = inputText.value.trim() || t('chat.input.attachWithImage')
  const toUpload = stagedAttachments.value.slice()
  inputText.value = ''
  uploadError.value = null
  creditsExhausted.value = false
  creditsRequiredCr.value = null
  demoLimitReached.value = false
  const sendingLockId = beginSendingLock()

  // Upload first so the assistant turn has real URLs to reference.
  let uploadedUrls: string[] = []
  if (toUpload.length > 0) {
    try {
      uploadedUrls = await uploadChatAttachments(toUpload.map(s => s.file))
    } catch (err) {
      uploadError.value = err instanceof Error ? err.message : t('chat.errors.uploadFailed')
      releaseSendingLock(sendingLockId)
      return
    }
  }

  // Clear the staged preview once the bytes are on the server; the
  // chat bubble below uses the persisted URL from this point on.
  for (const item of toUpload) URL.revokeObjectURL(item.preview)
  stagedAttachments.value = []

  // Immediate optimistic bubble so the user sees their turn land.
  localMessages.value.push({
    role: 'user',
    content: userText,
    attachments: uploadedUrls.map(url => ({
      kind: 'image',
      url,
      mime_type: 'image/*',
      caption: null,
    })),
  })
  streamingText.value = ''
  await scrollToBottom()

  // Captured from the stream's first SSE event. If the request later
  // fails, we still have the id so the parent can reload from the DB
  // (where the backend has already persisted the user message).
  let liveConversationId: string | null = props.conversationId
  const isDmSend = interactionMode.value === 'dm'
  try {
    const reply = await sendChatMessageStream(
      {
        character_id: props.character.id,
        conversation_id: props.conversationId,
        message: userText,
        attachment_urls: uploadedUrls,
        presence_frame: currentPresenceFrame(uploadedUrls.length > 0),
      },
      (token: string) => {
        if (!isDmSend) {
          streamingText.value += token
        }
        scrollToBottom()
      },
      (convId: string) => {
        liveConversationId = convId
        // Stash the id in the parent WITHOUT touching ``messages``.
        // A full conversationUpdate here would reassign props.messages,
        // which re-runs the messages watcher and overwrites the
        // in-flight optimistic user bubble mid-stream — producing a
        // visible duplicate once we append the assistant reply.
        emit('conversationIdLearned', convId)
      },
    )

    // 串流結束後把 streaming bubble 換成正式訊息；忙碌延遲的追加訊息
    // 可能只有 user message，沒有 immediate assistant reply。
    streamingText.value = ''
    if (reply.assistant_message) {
      const shouldReveal = isDmSend
        && splitAssistantBubbles(reply.assistant_message.content).length > 1
      const revealPromise = shouldReveal
        ? waitForMessageReveal(
          localMessages.value.length,
          () => releaseSendingLock(sendingLockId),
        )
        : null
      localMessages.value.push(reply.assistant_message)
      await scrollToBottom()
      if (revealPromise) {
        await revealPromise
      }
    }

    const updatedChar: Character = {
      ...props.character!,
      state: reply.state,
    }
    emit('conversationUpdate', reply.conversation_id, [...localMessages.value], updatedChar)
    // The post-turn processor may have nudged the character forward in
    // their schedule; refresh the cheap activity badge.
    refreshCurrentActivity()
    // The turn just spent credits — settle the badge on the post-charge
    // number rather than leaving the player to guess.
    refreshCloudCreditsAfterAction()
    if (!cloudMode.value) {
      loadNsfwMode()
    }
  } catch (err) {
    streamingText.value = ''
    revealingMessageIndex.value = null
    if (pendingRevealResolve) {
      pendingRevealResolve()
      pendingRevealResolve = null
    }
    pendingFirstRevealRelease = null
    if (isInsufficientCreditsError(err)) {
      // Not a failure to explain away: nothing ran and nothing was charged.
      // The notice card carries that promise plus the top-up CTA, so a
      // generic error bubble here would only muddy it.
      creditsExhausted.value = true
    } else if (isPriceChangedError(err)) {
      // The quoted Lume price moved mid-session; nothing was charged. Pull
      // the refreshed list so the composer hint shows the number the next
      // send will actually bind to.
      actionPricing.refresh()
      localMessages.value.push({
        role: 'assistant',
        content: t('chat.priceChanged'),
      })
    } else if (cloudMode.value && isDemoMessageCapError(err)) {
      demoLimitReached.value = true
    } else {
      localMessages.value.push({
        role: 'assistant',
        content: chatErrorContent(err),
      })
    }
    // Surface whatever conversation id we did learn so the parent
    // rehydrates against the backend copy (which has the user message
    // persisted from send_message_stream pre-LLM save).
    if (liveConversationId && props.character) {
      emit('conversationUpdate', liveConversationId, [...localMessages.value], props.character)
    }
  } finally {
    releaseSendingLock(sendingLockId)
    await scrollToBottom()
    focusInput()
  }
}

/** The hosted trial's own message ceiling, not a runtime/billing failure. */
function isDemoMessageCapError(err: unknown): boolean {
  return err instanceof ChatRuntimeLimitError
    && err.code === 'max_messages_per_session'
}

function chatErrorContent(err: unknown): string {
  if (err instanceof ChatRuntimeLimitError
    && err.code === 'max_messages_per_session') {
    return t('chat.errors.demoMaxMessages')
  }
  if (err instanceof ChatRuntimeLimitError
    && err.code === 'subscription_frozen') {
    return t('chat.errors.subscriptionFrozen')
  }
  if (err instanceof ChatStreamProtocolError
    && err.code === 'stream_ended_without_final_response') {
    return t('chat.errors.streamError', {
      reason: t('chat.errors.streamEndedWithoutFinalResponse'),
    })
  }
  return t('chat.errors.streamError', {
    reason: err instanceof Error ? err.message : t('common.errors.unknown'),
  })
}

function handleKeydown(e: KeyboardEvent) {
  if (!shouldSendChatInputOnKeydown(e, composingInput.value)) return
  e.preventDefault()
  handleSend()
}

function handleCompositionStart() {
  composingInput.value = true
}

function handleCompositionEnd() {
  composingInput.value = false
}

async function scrollToBottom() {
  await nextTick()
  if (messagesContainer.value) {
    messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
  }
}

// Touch devices: never auto-focus the textarea. Popping the on-screen
// keyboard without the user asking for it shrinks visualViewport and
// — on portrait, where chat is an absolute overlay that can be
// translateY(100%)-collapsed — makes iOS Safari scroll the document
// to "bring the focused input into view", pushing the absolutely
// positioned header buttons (sidebar toggle, drama / fusion / gram
// launchers) off the top of the screen.
function isCoarsePointer(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(pointer: coarse)').matches
}

async function focusInput() {
  if (isCoarsePointer()) return
  await nextTick()
  textareaRef.value?.focus()
}

function autoResizeTextarea() {
  // Grow with content up to the CSS max-height cap. Resetting to
  // "auto" first lets scrollHeight report the true content height
  // (otherwise it stays pinned to the previous larger value).
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}

// Re-run on every inputText change so shrinking after send / paste
// edits also settles to the right height.
watch(inputText, () => {
  nextTick(autoResizeTextarea)
})

// --- Virtual keyboard handling ---------------------------------------
// On mobile, the on-screen keyboard overlays the layout viewport but
// does NOT shrink 100dvh. Without this the chat input ends up hidden
// below the keyboard. VisualViewport gives us the real visible area;
// we mirror it into ``--app-height`` (consumed by StagePage) so the
// whole panel re-flows above the keyboard and the input stays in
// reach. We only override when the keyboard is actually occluding
// something — otherwise we let dvh drive.
function updateAppHeight() {
  const vv = window.visualViewport
  if (!vv) return
  const occluded = window.innerHeight - vv.height - vv.offsetTop
  if (occluded > 80) {
    // Keyboard (or similar overlay) is eating at least ~80px. Lock
    // app height to the visible slice so bottom-anchored UI rides up.
    document.documentElement.style.setProperty('--app-height', `${vv.height}px`)
  } else {
    // Back to the CSS default (100dvh / 100vh).
    document.documentElement.style.removeProperty('--app-height')
  }
}

onMounted(() => {
  void resolveTTSAvailability().then((available) => {
    ttsAvailable.value = available
  })
  loadChatAssistPreference()
  if (!cloudMode.value) {
    startNsfwModeClock()
    loadNsfwMode()
  }
  autoResizeTextarea()
  const vv = window.visualViewport
  if (vv) {
    vv.addEventListener('resize', updateAppHeight)
    vv.addEventListener('scroll', updateAppHeight)
    updateAppHeight()
  }
})

onUnmounted(() => {
  stopNsfwModeClock()
  const vv = window.visualViewport
  if (vv) {
    vv.removeEventListener('resize', updateAppHeight)
    vv.removeEventListener('scroll', updateAppHeight)
  }
  document.documentElement.style.removeProperty('--app-height')
})
</script>

<template>
  <div :class="panelClass">
    <NsfwModeAtmosphere v-if="nsfwModeActive" />

    <div v-if="!character" class="chat-empty">
      <span>{{ t('chat.header.noCharacter') }}</span>
    </div>

    <template v-else>
      <div class="chat-header">
        <div class="header-left">
          <span class="header-name">{{ character.name }}</span>
          <span
            v-if="currentActivity"
            class="header-activity"
            :title="currentActivity.description"
          >
            {{ formatActivityTime(currentActivity) }} · {{ currentActivity.description }}
          </span>
        </div>
        <div class="header-right">
          <span class="header-status">{{ modeStatusLabel }}</span>
          <span
            v-if="currentActivityLoading && !currentActivity"
            class="header-status"
          >{{ t('chat.header.preparingLife') }}</span>
          <span v-if="loadingHistory" class="header-status">{{ t('chat.header.loadingHistory') }}</span>
          <span v-else-if="conversationId" class="header-status">{{ t('chat.header.conversationOngoing') }}</span>
          <span v-else class="header-status">{{ t('chat.header.newConversation') }}</span>
          <UiButton
            v-if="showLayoutToggle"
            variant="ghost"
            size="sm"
            class="stage-layout-toggle"
            :aria-label="stageLayoutToggleAria"
            @click="emit('toggleStageLayout')"
          >
            {{ stageLayoutToggleLabel }}
          </UiButton>
        </div>
      </div>

      <div class="chat-mode-bar" role="tablist" :aria-label="t('chat.mode.ariaLabel')">
        <button
          type="button"
          role="tab"
          class="ui-btn ui-btn--segment mode-tab"
          :class="{ 'is-active': interactionMode === 'stage' }"
          :aria-selected="interactionMode === 'stage'"
          @click="selectInteractionMode('stage')"
        >
          <span class="mode-tab-icon" aria-hidden="true">⌂</span>
          <span class="mode-tab-text">
            <span class="mode-tab-title">{{ t('chat.mode.stage') }}</span>
            <span class="mode-tab-subtitle">{{ stageTabSubtitle }}</span>
          </span>
        </button>
        <button
          type="button"
          role="tab"
          class="ui-btn ui-btn--segment mode-tab"
          :class="{ 'is-active': interactionMode === 'dm' }"
          :aria-selected="interactionMode === 'dm'"
          @click="selectInteractionMode('dm')"
        >
          <span class="mode-tab-icon" aria-hidden="true">▣</span>
          <span class="mode-tab-text">
            <span class="mode-tab-title">{{ t('chat.mode.dm') }}</span>
            <span class="mode-tab-subtitle">{{ t('chat.mode.dmHint') }}</span>
          </span>
        </button>
      </div>

      <div ref="messagesContainer" class="messages-container">
        <ChatFirstTurnGuide
          v-if="localMessages.length === 0 && !sending && !loadingHistory"
          :character-name="character.name"
          :mode="interactionMode"
          :context="emptyMessage"
          @select-starter="useStarterMessage"
        />

        <ChatBubble
          v-for="(msg, i) in localMessages"
          :key="i"
          :message="msg"
          :character-id="character?.id ?? null"
          :tts-available="ttsAvailable"
          :animate-reveal="revealingMessageIndex === i"
          :text-message-mode="interactionMode === 'dm'"
          @reveal-complete="handleBubbleRevealComplete(i)"
          @reveal-progress="handleBubbleRevealProgress(i)"
          @insufficient-credits="creditsExhausted = true"
        />
        <!-- 串流中的 bubble -->
        <ChatBubble
          v-if="streamingText"
          :message="{ role: 'assistant', content: streamingText }"
          :character-id="character?.id ?? null"
          :tts-available="ttsAvailable"
          @insufficient-credits="creditsExhausted = true"
        />
        <!-- 首 token 到達前的 typing indicator -->
        <div v-else-if="sending && !revealInProgress" class="typing-indicator">
          <span class="dot" /><span class="dot" /><span class="dot" />
          <span
            v-if="character && character.allowed_tools && character.allowed_tools.length > 0"
            class="tool-wait-hint"
          >{{ t('chat.history.streamingHint') }}</span>
        </div>

        <!-- 螢火不足：取代泛用錯誤氣泡，直接給承諾文案與加值入口。
             獨立 v-if（不接上面的 v-else-if 鏈）以免動到 typing
             indicator 既有的顯示條件。 -->
        <InsufficientCreditsNotice
          v-if="creditsExhausted"
          class="chat-credits-notice"
          :required-cr="creditsRequiredCr"
        />

        <!-- 試玩上限：雲端專用卡片，把玩家帶回帳號中心註冊正式方案。 -->
        <div v-if="demoLimitReached" class="chat-demo-limit" role="status">
          <p class="chat-demo-limit__body">{{ pt('chat.errors.demoMaxMessages') }}</p>
          <a
            v-if="portalUrl"
            class="chat-demo-limit__cta"
            :href="portalUrl"
          >{{ t('chat.errors.demoMaxMessagesCta') }}</a>
        </div>
      </div>

      <div class="chat-input-area">
        <ChatAssistDiscoveryHint
          :visible="chatAssistHintVisible"
          :character-name="characterDisplayName"
          @open="handleChatAssistClick"
          @dismiss="dismissChatAssistHint"
        />

        <div
          v-if="chatAssistEnabled && chatAssistOpen"
          class="chat-assist-panel"
          role="region"
          :aria-label="t('chat.assist.title')"
        >
          <div class="chat-assist-panel__header">
            <span class="chat-assist-panel__title">{{ t('chat.assist.title') }}</span>
            <div class="chat-assist-panel__actions">
              <button
                type="button"
                class="chat-assist-icon-btn"
                :disabled="chatAssistLoading || sending"
                :title="t('chat.assist.refresh')"
                :aria-label="t('chat.assist.refresh')"
                @click="loadChatAssistSuggestions"
              >
                <ReloadOutlined />
              </button>
              <button
                type="button"
                class="chat-assist-icon-btn"
                :title="t('common.actions.close')"
                :aria-label="t('common.actions.close')"
                @click="closeChatAssist"
              >
                <CloseOutlined />
              </button>
            </div>
          </div>

          <div v-if="chatAssistLoading" class="chat-assist-state">
            {{ t('chat.assist.loading') }}
          </div>
          <div v-else-if="chatAssistError" class="chat-assist-state chat-assist-state--error">
            {{ chatAssistError }}
          </div>
          <div v-else-if="chatAssistSuggestions.length > 0" class="chat-assist-suggestions">
            <button
              v-for="suggestion in chatAssistSuggestions"
              :key="suggestion.text"
              type="button"
              class="chat-assist-suggestion"
              :title="suggestion.reason || suggestion.text"
              @click="useChatAssistSuggestion(suggestion.text)"
            >
              {{ suggestion.text }}
            </button>
          </div>
          <div v-else class="chat-assist-state">
            {{ t('chat.assist.empty') }}
          </div>
        </div>

        <div
          v-if="stagedAttachments.length > 0 || uploadError"
          class="staged-attachments"
        >
          <div
            v-for="(item, idx) in stagedAttachments"
            :key="idx"
            class="staged-thumb"
          >
            <img :src="item.preview" :alt="`pending ${idx + 1}`" />
            <button
              type="button"
              class="staged-remove"
              :disabled="sending"
              :title="t('chat.input.removeThis')"
              @click="removeStagedAttachment(idx)"
            >×</button>
          </div>
          <span v-if="uploadError" class="upload-error">{{ uploadError }}</span>
        </div>
        <div class="input-row">
          <div class="input-actions" v-click-outside="closeActionMenu">
            <button
              type="button"
              class="action-trigger"
              :class="{ 'action-trigger--open': actionMenuOpen }"
              :disabled="sending"
              :aria-expanded="actionMenuOpen"
              aria-haspopup="menu"
              :title="t('chat.input.moreActions')"
              @click="toggleActionMenu"
            >⋯</button>
            <div v-if="actionMenuOpen" class="action-menu" role="menu">
              <button
                v-if="chatAssistEnabled"
                type="button"
                role="menuitem"
                class="action-item"
                :disabled="sending || chatAssistLoading"
                @click="handleChatAssistClick"
              >
                <BulbOutlined class="action-icon" />
                <span class="action-label">
                  {{ chatAssistLoading ? t('chat.assist.loadingShort') : t('chat.assist.action') }}
                </span>
              </button>
              <button
                type="button"
                role="menuitem"
                class="action-item"
                :disabled="sending || stagedAttachments.length >= MAX_ATTACHMENTS_PER_TURN"
                @click="handleAttachClick"
              >
                <span class="action-icon">📎</span>
                <span class="action-label">{{ t('chat.input.attachImage') }}</span>
                <span
                  v-if="stagedAttachments.length >= MAX_ATTACHMENTS_PER_TURN"
                  class="action-hint"
                >{{ t('chat.input.attachLimit', { n: MAX_ATTACHMENTS_PER_TURN }) }}</span>
              </button>
              <button
                v-if="conversationId && localMessages.length >= 2"
                type="button"
                role="menuitem"
                class="action-item"
                :disabled="undoing || sending"
                @click="handleUndoClick"
              >
                <span class="action-icon">↶</span>
                <span class="action-label">
                  {{ undoing ? t('chat.actions.undoing') : t('chat.actions.undo') }}
                </span>
              </button>
            </div>
          </div>
          <input
            ref="fileInputRef"
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            multiple
            style="display: none"
            @change="onFilesSelected"
          />
          <textarea
            ref="textareaRef"
            v-model="inputText"
            class="chat-textarea"
            :placeholder="inputPlaceholder"
            @input="autoResizeTextarea"
            rows="1"
            :disabled="sending"
            @compositionstart="handleCompositionStart"
            @compositionend="handleCompositionEnd"
            @keydown="handleKeydown"
            @paste="onPaste"
          />
          <button
            class="send-btn"
            :disabled="(!inputText.trim() && stagedAttachments.length === 0) || sending"
            @click="handleSend"
          >
            {{ sending ? t('chat.input.sending') : t('chat.input.send') }}
          </button>
        </div>
        <!-- 明碼標價：送出前就看得到這則對話的價格。查不到價格（自架站、按用量
             計費的方案、價目表讀不到）時完全不輸出節點。 -->
        <div class="chat-price-row">
          <!-- 一則訊息的價格不含圖：他在對話中畫圖是另一筆，所以在同一行一併
               揭露，不讓玩家事後才發現多扣。查不到圖片價格就整段不出現。 -->
          <span v-if="chatImageExtraText" class="chat-price-extra">
            {{ chatImageExtraText }}
          </span>
          <ActionPriceHint
            :action-key="ACTION_CHAT"
            tooltip-key="credits.price.chatTooltip"
          />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.chat-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.chat-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
  font-size: 14px;
  padding: 24px;
  text-align: center;
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--color-border);
  background: rgba(0, 0, 0, 0.15);
  flex-shrink: 0;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.header-name {
  font-weight: 600;
  font-size: 14px;
  min-width: 0;
  flex-shrink: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.header-activity {
  font-size: 11px;
  padding: 2px 8px;
  background: rgba(100, 150, 220, 0.18);
  border-radius: 10px;
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
  flex: 0 1 auto;
  max-width: 240px;
}

.header-status {
  font-size: 11px;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.chat-mode-bar {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.025);
  flex-shrink: 0;
}


.mode-tab {
  gap: 8px;
  min-width: 0;
  min-height: 48px;
  padding: 8px 10px;
  text-align: left;
}

.mode-tab-icon {
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.06);
  font-size: 15px;
  line-height: 1;
}

.mode-tab-text {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.mode-tab-title {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.2;
  color: inherit;
}

.mode-tab-subtitle {
  font-size: 11px;
  line-height: 1.25;
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-panel--dm {
  background: linear-gradient(180deg, rgba(9, 14, 26, 0.35), rgba(16, 10, 38, 0.65));
}

.chat-panel--dm .messages-container,
.chat-panel--dm .chat-input-area {
  width: min(100%, 460px);
  align-self: center;
}

.chat-panel--dm .messages-container {
  border-left: 1px solid rgba(255, 255, 255, 0.06);
  border-right: 1px solid rgba(255, 255, 255, 0.06);
  background: rgba(0, 0, 0, 0.16);
}
.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex-shrink: 0;
}

@media (max-width: 720px) {
  /* 手機維持並排：副標已隱藏，兩顆變成緊湊單行 segmented，不直疊吃掉對話空間。 */
  .chat-mode-bar {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px;
    padding: 6px 10px;
  }

  .mode-tab {
    min-height: 34px;
    padding: 5px 8px;
    gap: 6px;
    justify-content: center;
    text-align: center;
  }

  .mode-tab-icon {
    width: 22px;
    height: 22px;
    font-size: 13px;
  }

  .mode-tab-text {
    flex-direction: row;
  }

  .mode-tab-subtitle {
    display: none;
  }

  .chat-header {
    padding: 8px 10px;
    gap: 8px;
  }

  /* 把整列讓給角色名 + 行程，行程吃掉剩餘寬度只在真的過長時才省略。 */
  .header-left {
    flex: 1;
    gap: 6px;
  }

  .header-name {
    flex-shrink: 1;
    max-width: 55%;
  }

  .header-activity {
    flex: 1 1 auto;
    max-width: none;
  }

  /* 模式狀態與對話狀態跟下方 mode bar 重複，手機收起省空間。 */
  .header-right {
    display: flex;
    gap: 6px;
  }

  .header-right .header-status {
    display: none;
  }

}

.input-actions {
  position: relative;
  display: flex;
  align-items: stretch;
}

.action-trigger {
  width: 44px;
  min-width: 44px;
  min-height: 44px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  color: var(--color-text-secondary);
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}

.action-trigger:hover:not(:disabled),
.action-trigger--open {
  background: rgba(255, 255, 255, 0.12);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.action-trigger:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.action-menu {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  min-width: 180px;
  background: var(--color-bg-secondary, #1f2024);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
  padding: 4px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  z-index: 20;
}

.action-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: transparent;
  border: none;
  border-radius: 6px;
  color: var(--color-text-primary);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition: background 0.12s;
}

.action-item:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
}

.action-item:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.action-icon {
  font-size: 16px;
  width: 20px;
  text-align: center;
}

.action-label {
  flex: 1;
}

.action-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.messages-container {
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
  /* Keep rubber-banding from chaining into the page / VisualViewport,
     which on iOS can otherwise lift the input area above the keyboard
     unexpectedly mid-scroll. */
  overscroll-behavior: contain;
}

.chat-credits-notice {
  align-self: flex-start;
  max-width: 90%;
}

.chat-demo-limit {
  align-self: flex-start;
  max-width: 90%;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.04);
}

.chat-demo-limit__body {
  margin: 0;
  font-size: var(--font-xs);
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.chat-demo-limit__cta {
  align-self: flex-start;
  padding: 6px 12px;
  border: 1px solid var(--color-primary);
  border-radius: 6px;
  color: var(--color-primary-light);
  font-size: var(--font-xs);
  text-decoration: none;
}

.chat-demo-limit__cta:hover {
  background: rgba(183, 93, 63, 0.18);
}

.typing-indicator {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  background: var(--color-assistant-bubble);
  border-radius: 12px;
  width: fit-content;
  max-width: 90%;
}

.tool-wait-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  margin-left: 4px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  animation: bounce 1.4s infinite ease-in-out;
}

.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }

@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}

.chat-input-area {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 14px;
  /* 底部加上 iOS home indicator 的避讓高度 */
  padding-bottom: calc(10px + var(--safe-area-bottom));
  border-top: 1px solid var(--color-border);
  background: rgba(0, 0, 0, 0.2);
  flex-shrink: 0;
}

.chat-assist-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 9px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.045);
}

.chat-assist-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.chat-assist-panel__title {
  color: var(--color-text);
  font-size: 12px;
  font-weight: 700;
  line-height: 1.3;
}

.chat-assist-panel__actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}

.chat-assist-icon-btn {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}

.chat-assist-icon-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: rgba(106, 169, 240, 0.55);
  background: rgba(106, 169, 240, 0.12);
}

.chat-assist-icon-btn:disabled {
  opacity: 0.45;
  cursor: wait;
}

.chat-assist-suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chat-assist-suggestion {
  max-width: 100%;
  padding: 7px 9px;
  border: 1px solid rgba(106, 169, 240, 0.34);
  border-radius: 8px;
  background: rgba(106, 169, 240, 0.1);
  color: var(--color-text);
  font: inherit;
  font-size: 12px;
  line-height: 1.4;
  text-align: left;
  white-space: normal;
  overflow-wrap: anywhere;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}

.chat-assist-suggestion:hover {
  border-color: rgba(106, 169, 240, 0.72);
  background: rgba(106, 169, 240, 0.18);
}

.chat-assist-state {
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.45;
}

.chat-assist-state--error {
  color: #ff9a8a;
}

.input-row {
  display: flex;
  gap: 8px;
  align-items: stretch;
}

/* Sits under the composer, right-aligned against the send button. Both parts
   render nothing when there is no price to quote, so the row costs nothing. */
.chat-price-row {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 4px 8px;
  margin-top: -4px;
}

.chat-price-extra {
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.4;
}

.staged-attachments {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-height: 52px;
}

.staged-thumb {
  position: relative;
  width: 52px;
  height: 52px;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
}

.staged-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.staged-remove {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.6);
  color: #fff;
  border: none;
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  padding: 0;
}

.staged-remove:hover {
  background: rgba(231, 76, 60, 0.9);
}

.upload-error {
  font-size: 11px;
  color: #ff8a75;
  padding: 0 6px;
}

.chat-textarea {
  flex: 1;
  padding: 10px 12px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  color: var(--color-text);
  /*
    iOS 在輸入框 font-size < 16px 時會自動放大縮放頁面，體驗極差。
    16px 是 Mobile Safari 不觸發 auto-zoom 的最低值。
  */
  font-size: 16px;
  line-height: 1.4;
  font-family: inherit;
  resize: none;
  outline: none;
  transition: border-color 0.2s, height 0.05s;
  /* Auto-expand: JS sets height to scrollHeight on input. min-height
     gives us a sane single-row floor; max-height caps growth so a
     long draft can't swallow the whole screen. On mobile the dynamic
     cap is a fraction of the visible viewport so the messages area
     always keeps some breathing room above the keyboard. */
  min-height: 44px;
  max-height: min(200px, 35dvh);
  overflow-y: auto;
  /* Explicit — rule out any ancestor clamping cursor / selection. */
  -webkit-user-select: text;
  user-select: text;
  touch-action: manipulation;
}

.chat-textarea:focus {
  border-color: var(--color-primary);
}

.chat-textarea:disabled {
  opacity: 0.5;
}

.send-btn {
  padding: 10px 20px;
  background: var(--color-primary);
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 15px;
  font-weight: 600;
  transition: background 0.2s;
  align-self: stretch;
  min-width: 88px;
  min-height: 44px;
  white-space: nowrap;
}

.send-btn:hover:not(:disabled) {
  background: var(--color-primary-dark);
}

.send-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
