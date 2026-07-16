<script setup lang="ts">
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import type { Character } from '@/types/character'
import type { ChatMessage } from '@/types/chat'
import { listCharacters, getCharacter, deleteCharacter } from '@/utils/api/characters'
import { getLatestConversation, markConversationRead } from '@/utils/api/chat'
import {
  connectProactiveEvents,
  type EventStreamHandle,
  type ProactiveMessageEvent,
} from '@/utils/proactiveEvents'
import {
  connectFeedEvents,
  type FeedCommentReplyEvent,
  type FeedEventStreamHandle,
  type FeedPostEvent,
} from '@/utils/feedEvents'
import { getGlobalFeedUnread, markFeedReactionsSeen } from '@/utils/api/feed'
import {
  characterIdsWithUnreadFeedReplies,
  formatLumeGramBadge,
  totalLumeGramUnread,
} from '@/utils/lumeGramUnread'
import PlayerSidebar from '@/components/PlayerSidebar.vue'
import ChatPanel from '@/components/ChatPanel.vue'
import ImageStage from '@/components/ImageStage.vue'
import KokoroGramOverlay from '@/components/KokoroGramOverlay.vue'
import MemoirOverlay from '@/components/MemoirOverlay.vue'
import {
  isStudioCoachmarkDismissed,
  rememberStudioCoachmarkDismissed,
} from '@/utils/arcDiscovery'
import { showBackgroundPageNotification } from '@/utils/pushNotifications'
import { getActiveStudioJobs } from '@/utils/api/studioJobs'
import {
  getExplicitStageLayout,
  resolveStageLayout,
  setExplicitStageLayout,
  type StageLayoutMode,
} from '@/utils/stageLayout'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const characters = ref<Character[]>([])
const selectedCharacter = ref<Character | null>(null)
const conversationId = ref<string | null>(null)
const messages = ref<ChatMessage[]>([])
const historyLoading = ref(false)

function getStageLocalStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

const studioCoachmarkDismissed = ref(isStudioCoachmarkDismissed(getStageLocalStorage()))
const showStudioCoachmark = computed(() => !studioCoachmarkDismissed.value)

function dismissStudioCoachmark() {
  rememberStudioCoachmarkDismissed(getStageLocalStorage())
  studioCoachmarkDismissed.value = true
}

function handleStudioLauncherClick() {
  dismissStudioCoachmark()
}

// Creator Studio「創作進行中」全域輕量指示（C0 生成體驗）：
// 低頻輪詢 job ledger 的 running 數，>0 時 launcher 亮點。
const studioActiveJobs = ref(0)
let studioJobsPollHandle: number | null = null

async function refreshStudioActiveJobs() {
  try {
    studioActiveJobs.value = (await getActiveStudioJobs()).running
  } catch {
    studioActiveJobs.value = 0
  }
}

onMounted(() => {
  void refreshStudioActiveJobs()
  studioJobsPollHandle = window.setInterval(
    () => void refreshStudioActiveJobs(),
    20000,
  )
})

onBeforeUnmount(() => {
  if (studioJobsPollHandle != null) {
    window.clearInterval(studioJobsPollHandle)
    studioJobsPollHandle = null
  }
})

// 桌面預設開啟、窄螢幕（<900px）預設收合
const sidebarOpen = ref(
  typeof window !== 'undefined' ? window.matchMedia('(min-width: 900px)').matches : true,
)

// LumeGram overlay 開關。Overlay 內部自己管 filter（全局 vs 點到
// 角色），launcher 只負責開啟。
const kokoroGramOpen = ref(false)

// 回憶錄 overlay 開關。從 PlayerSidebar 的 memoir 按鈕觸發；character
// 從 sidebar event 帶上來，避免 overlay 自己再查一次 character API。
const memoirOpen = ref(false)
const memoirCharacter = ref<Character | null>(null)
function openMemoirOverlay(char: Character) {
  memoirCharacter.value = char
  memoirOpen.value = true
}

// 全局 LumeGram 新貼文未讀 —— 「上次打開動態牆之後新增了幾篇貼文」
// 的計數。watermark 由 KokoroGramOverlay 在打開瞬間寫入 localStorage
// 並 dispatch ``kokoro:feed-watermark-bumped`` event；server 端只負責
// 回應 ``count_since`` 對齊初始值。Launcher 實際紅點會再合併每角色
// ``unread_feed_reply_count``，讓角色留言回覆也能提示。
const FEED_WATERMARK_KEY = 'kokoro.feedLastViewedAt'
const globalFeedUnread = ref(0)
const lumeGramUnread = computed(() => totalLumeGramUnread(
  globalFeedUnread.value,
  characters.value,
))

function readWatermark(): string | null {
  if (typeof window === 'undefined') return null
  try {
    return localStorage.getItem(FEED_WATERMARK_KEY)
  } catch {
    return null
  }
}

async function refreshGlobalFeedUnread() {
  try {
    const res = await getGlobalFeedUnread(readWatermark())
    globalFeedUnread.value = res.count
  } catch {
    // 失敗就靜默 — 紅點不算 critical，下次 SSE 推或下次 onMount 再算
  }
}

function handleWatermarkBumped() {
  // GlobalFeedPage 進頁時 dispatch — 此時 watermark 已寫入，紅點歸零
  globalFeedUnread.value = 0
  void markUnreadFeedRepliesSeen()
}

function handleFeedPostEvent(_evt: FeedPostEvent) {
  // 新貼文就 +1；若使用者剛好在 /feed 頁面，GlobalFeedPage 會在頁面
  // reload 後再 bump watermark → 觸發 handleWatermarkBumped 歸零，所
  // 以這裡不需要判斷當前路由。
  globalFeedUnread.value += 1
}

const kokoroGramTitle = computed(() => {
  if (lumeGramUnread.value > 0) {
    return t('stage.launchers.kokoroGramTitleUnread', { count: lumeGramUnread.value })
  }
  return t('stage.launchers.kokoroGramTitleDefault')
})

// 偵測是否為 portrait 取向：portrait 下走 overlay 佈局（stage 滿版 + chat 滿版浮層）
const isPortrait = ref(false)
let portraitMql: MediaQueryList | null = null

function handlePortraitChange(ev: MediaQueryListEvent | MediaQueryList) {
  isPortrait.value = 'matches' in ev ? ev.matches : false
}

// ---- Portrait overlay：stage 全螢幕背景 + chat 半透明浮層 ----
// 只影響 portrait 模式。Landscape 的雙欄佈局照舊。
// chat 顯示狀態不 persist（reload 預設是開的，避免使用者找不到訊息）；
// 透明度會 persist（使用者偏好不希望每次重設）。
const OPACITY_KEY = 'kokoro.chatOpacity'
const chatVisible = ref(true)
const chatOverlaySettingsOpen = ref(false)
// 預設 0.78 — 滿版浮層下這個透明度能讓舞台背景隱約透出，聊天字又清楚
const chatOpacity = ref(
  typeof window !== 'undefined'
    ? clampOpacity(parseFloat(localStorage.getItem(OPACITY_KEY) ?? '') || 0.78)
    : 0.78,
)

function clampOpacity(v: number): number {
  if (!Number.isFinite(v)) return 0.78
  return Math.min(1, Math.max(0.35, v))
}

watch(chatOpacity, (v) => {
  if (typeof window !== 'undefined') {
    localStorage.setItem(OPACITY_KEY, String(clampOpacity(v)))
  }
})

// 模糊半徑依透明度調整：低透明度幾乎不模糊，讓舞台輪播圖清楚透出來；
// 高透明度保留 glassmorphism。0.35→0px，1.0→14px，中間線性內插。
const chatBlurPx = computed(() => {
  const t = (clampOpacity(chatOpacity.value) - 0.35) / (1 - 0.35)
  return Math.round(Math.max(0, Math.min(1, t)) * 14)
})

function collapseChatOverlay() {
  chatOverlaySettingsOpen.value = false
  chatVisible.value = false
}

// ---- 桌面 landscape 版面偏好：stage-centric（預設）/ chat-centric ----
// 只影響 >=960px landscape 分支；portrait overlay 不受影響。
// 角色無圖時自動預設 chat-centric，但使用者一旦顯式切換過，該偏好永久
// 蓋過自動規則（見 utils/stageLayout.ts 的 resolveStageLayout）。
const explicitStageLayout = ref<StageLayoutMode | null>(
  getExplicitStageLayout(getStageLocalStorage(), selectedCharacter.value?.id),
)

const hasStageImages = computed(
  () => (selectedCharacter.value?.image_urls?.length ?? 0) > 0,
)

const stageLayoutMode = computed<StageLayoutMode>(() =>
  resolveStageLayout({
    explicit: explicitStageLayout.value,
    hasImages: hasStageImages.value,
  }),
)

// 切角色時重新讀取「這個角色」的顯式偏好（per-character key）
watch(
  () => selectedCharacter.value?.id,
  (id) => {
    explicitStageLayout.value = getExplicitStageLayout(getStageLocalStorage(), id)
  },
)

function setStageLayout(mode: StageLayoutMode) {
  explicitStageLayout.value = mode
  setExplicitStageLayout(getStageLocalStorage(), selectedCharacter.value?.id, mode)
}

function toggleStageLayout() {
  setStageLayout(stageLayoutMode.value === 'stage-centric' ? 'chat-centric' : 'stage-centric')
}

// SSE handle for in-app push (proactive messages). Opened once in
// onMounted and closed on teardown; native EventSource reconnects on
// its own so we don't retry manually.
let eventStream: EventStreamHandle | null = null

// Separate SSE handle for LumeGram comment-reply notifications.
// Lives at the page level (not inside the overlay) so the badge
// updates even while the overlay is closed — that's the main UX win.
let feedEventStream: FeedEventStreamHandle | null = null

function applyUnreadCount(characterId: string, count: number) {
  characters.value = characters.value.map(c =>
    c.id === characterId ? { ...c, unread_proactive_count: count } : c,
  )
  if (selectedCharacter.value?.id === characterId) {
    selectedCharacter.value = {
      ...selectedCharacter.value,
      unread_proactive_count: count,
    }
  }
}

function applyFeedReplyUnread(characterId: string, count: number) {
  characters.value = characters.value.map(c =>
    c.id === characterId
      ? { ...c, unread_feed_reply_count: count }
      : c,
  )
  if (selectedCharacter.value?.id === characterId) {
    selectedCharacter.value = {
      ...selectedCharacter.value,
      unread_feed_reply_count: count,
    }
  }
}

function handleFeedCommentReply(event: FeedCommentReplyEvent) {
  // Server's ``unread_count`` is the post-increment value; we just
  // mirror it locally so the badge matches the next character GET.
  applyFeedReplyUnread(event.character_id, event.unread_count)
}

async function markUnreadFeedRepliesSeen() {
  const ids = characterIdsWithUnreadFeedReplies(characters.value)
  if (ids.length === 0) return

  const results = await Promise.allSettled(
    ids.map(async (id) => {
      await markFeedReactionsSeen(id)
      return id
    }),
  )
  for (const result of results) {
    if (result.status === 'fulfilled') {
      applyFeedReplyUnread(result.value, 0)
    }
  }
}

async function handleProactiveEvent(event: ProactiveMessageEvent) {
  // Always reflect the new count on the sidebar first — this is what
  // the user scans; missing an event here is the failure mode that
  // feels "broken" even if the message shows up once they open chat.
  applyUnreadCount(event.character_id, event.unread_count)

  const target = characters.value.find(c => c.id === event.character_id)
  const name = target?.name ?? t('common.fallback.character')

  if (selectedCharacter.value?.id === event.character_id) {
    // Chat is already open on this character — pull the fresh thread
    // instead of mutating the local array (server is source of truth
    // for ordering + attachments) and clear the just-bumped counter.
    try {
      await loadHistoryFor(event.character_id)
      await markConversationRead(event.character_id)
      applyUnreadCount(event.character_id, 0)
    } catch {
      // If reload fails the next send will recover; no need to bother
      // the user with a toast for an internal refresh glitch.
    }
    return
  }

  const shownByOs = await showBackgroundPageNotification({
    title: t('stage.notifications.messageFrom', { name }),
    body: event.message.length > 120
      ? event.message.slice(0, 120) + '...'
      : event.message,
    icon: target?.image_urls?.[0] ?? null,
    url: `/?character=${encodeURIComponent(event.character_id)}`,
    type: 'proactive',
    characterId: event.character_id,
  })
  if (shownByOs) return

  // Different character (or nothing selected) — toast as a call to
  // action. Clicking the notification switches to the character so
  // the mark-read flow clears the badge naturally.
  notification.info({
    message: t('stage.notifications.messageFrom', { name }),
    description: event.message.length > 120
      ? event.message.slice(0, 120) + '…'
      : event.message,
    duration: 6,
    onClick: () => {
      if (target) void handleSelectCharacter(target)
      notification.destroy()
    },
  })
}

async function applyNotificationDeepLink(url: string) {
  const parsed = new URL(url, window.location.origin)
  const characterId = parsed.searchParams.get('character')
  const surface = parsed.searchParams.get('surface')
  if (characterId) {
    const existing = characters.value.find(c => c.id === characterId)
    if (existing) {
      await handleSelectCharacter(existing)
    } else {
      try {
        const character = await getCharacter(characterId)
        await handleSelectCharacter(character)
      } catch {
        // Deep link may point at a deleted or unauthorized character.
      }
    }
  }
  if (surface === 'feed') {
    kokoroGramOpen.value = true
  }
}

function handleServiceWorkerMessage(event: MessageEvent) {
  const data = event.data as { type?: string, url?: string } | null
  if (data?.type !== 'yuralume:notification-click' || !data.url) return
  void applyNotificationDeepLink(data.url)
}

onMounted(async () => {
  if (typeof window !== 'undefined') {
    portraitMql = window.matchMedia('(orientation: portrait)')
    isPortrait.value = portraitMql.matches
    portraitMql.addEventListener('change', handlePortraitChange)
  }

  const [charList] = await Promise.all([
    listCharacters(),
  ])
  characters.value = charList

  // Initial selection priority: ?character=<id> query (e.g. coming
  // from the World page's roster "對話" button) → localStorage last
  // pick → none. The query wins so the World→Chat handoff lands the
  // operator on the requested character even if they had a different
  // one selected previously.
  const queryCharId =
    typeof route.query.character === 'string' && route.query.character
      ? route.query.character
      : null
  const querySurface =
    typeof route.query.surface === 'string' ? route.query.surface : null
  const savedId =
    queryCharId ?? localStorage.getItem('kokoro.selectedCharacterId')
  if (savedId) {
    try {
      const c = await getCharacter(savedId)
      selectedCharacter.value = c
      await loadHistoryFor(c.id)
      if (c.unread_proactive_count > 0) {
        try {
          await markConversationRead(c.id)
          applyUnreadCount(c.id, 0)
        } catch { /* non-fatal */ }
      }
    } catch {
      localStorage.removeItem('kokoro.selectedCharacterId')
    }
  }
  if (querySurface === 'feed') {
    kokoroGramOpen.value = true
  }
  // Strip the ``?character=`` param so reloads don't pin the operator
  // to one character forever — the localStorage entry takes over from
  // here and the URL stays clean.
  if (queryCharId || querySurface) {
    void router.replace({ path: '/', query: {} })
  }

  eventStream = connectProactiveEvents((event) => {
    void handleProactiveEvent(event)
  })
  // Feed SSE 包兩個頻道：
  //   - ``feed_post``：launcher 紅點 +1（全局未讀計數）
  //   - ``feed_comment_reply``：角色回覆使用者留言，per-character 計
  //     數，個別角色 overlay 顯示
  feedEventStream = connectFeedEvents(handleFeedPostEvent, {
    onCommentReply: handleFeedCommentReply,
  })

  // 啟動時對齊一次紅點 — 拿 localStorage watermark 跟 server 比，
  // 算「上次看完後新增了幾篇」。沒 watermark（第一次造訪）回 0。
  void refreshGlobalFeedUnread()

  // GlobalFeedPage 進頁時會 dispatch 這個 event，紅點立刻歸零
  if (typeof window !== 'undefined') {
    window.addEventListener(
      'kokoro:feed-watermark-bumped', handleWatermarkBumped,
    )
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.addEventListener(
        'message',
        handleServiceWorkerMessage,
      )
    }
  }
})

onBeforeUnmount(() => {
  portraitMql?.removeEventListener('change', handlePortraitChange)
  eventStream?.close()
  eventStream = null
  feedEventStream?.close()
  feedEventStream = null
  if (typeof window !== 'undefined') {
    window.removeEventListener(
      'kokoro:feed-watermark-bumped', handleWatermarkBumped,
    )
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.removeEventListener(
        'message',
        handleServiceWorkerMessage,
      )
    }
  }
})

async function loadHistoryFor(characterId: string) {
  historyLoading.value = true
  try {
    const snapshot = await getLatestConversation(characterId)
    if (snapshot) {
      conversationId.value = snapshot.id
      messages.value = snapshot.messages
    } else {
      conversationId.value = null
      messages.value = []
    }
  } catch {
    conversationId.value = null
    messages.value = []
  } finally {
    historyLoading.value = false
  }
}

async function handleSelectCharacter(char: Character) {
  selectedCharacter.value = char
  localStorage.setItem('kokoro.selectedCharacterId', char.id)
  await loadHistoryFor(char.id)
  // Opening the chat == reading any pending proactive messages, so
  // zero the badge on both server and local state.
  if (char.unread_proactive_count > 0) {
    try {
      await markConversationRead(char.id)
      applyUnreadCount(char.id, 0)
    } catch { /* non-fatal */ }
  }
  // 手機直式下選完角色自動收起 sidebar
  if (typeof window !== 'undefined' && !window.matchMedia('(min-width: 900px)').matches) {
    sidebarOpen.value = false
  }
}

function handleCharacterUpdated(char: Character) {
  selectedCharacter.value = char
  characters.value = characters.value.map(c => c.id === char.id ? char : c)
}

function handleCharacterCreated(char: Character) {
  characters.value.push(char)
  handleSelectCharacter(char)
}

async function handleCharacterDataReset(char: Character) {
  // Clearing memories/conversations may leave the web UI pointing at a
  // stale conversation id. Reload the character's latest snapshot so the
  // chat panel reflects the fresh state.
  if (selectedCharacter.value?.id !== char.id) return
  await loadHistoryFor(char.id)
}

async function handleDeleteCharacter(char: Character) {
  try {
    await deleteCharacter(char.id)
  } catch (err) {
    notification.error({
      message: t('common.errors.deleteFailed', {
        reason: err instanceof Error ? err.message : t('common.errors.unknown'),
      }),
      duration: 4,
    })
    return
  }

  characters.value = characters.value.filter(c => c.id !== char.id)

  if (selectedCharacter.value?.id === char.id) {
    selectedCharacter.value = null
    conversationId.value = null
    messages.value = []
    localStorage.removeItem('kokoro.selectedCharacterId')
  }
}

function handleConversationUpdate(convId: string, msgs: ChatMessage[], char: Character) {
  conversationId.value = convId
  messages.value = msgs
  selectedCharacter.value = char
  characters.value = characters.value.map(c => c.id === char.id ? char : c)
}
</script>

<template>
  <div
    class="stage-layout"
    :class="{
      'sidebar-collapsed': !sidebarOpen,
      'chat-hidden': isPortrait && !chatVisible,
      'portrait-overlay': isPortrait,
      'chat-centric': !isPortrait && stageLayoutMode === 'chat-centric',
    }"
    :style="{ '--chat-opacity': chatOpacity, '--chat-blur': `${chatBlurPx}px` }"
  >
    <!-- 小螢幕遮罩 -->
    <div
      v-if="sidebarOpen"
      class="sidebar-backdrop"
      aria-hidden="true"
      @click="sidebarOpen = false"
    />

    <aside class="sidebar">
      <PlayerSidebar
        :characters="characters"
        :selected-character="selectedCharacter"
        @select-character="handleSelectCharacter"
        @character-updated="handleCharacterUpdated"
        @character-created="handleCharacterCreated"
        @delete-character="handleDeleteCharacter"
        @character-data-reset="handleCharacterDataReset"
        @open-memoir="openMemoirOverlay"
      />
    </aside>

    <main class="main-area">
      <div class="stage-section">
        <ImageStage :character="selectedCharacter" />
        <nav class="stage-launcher-row" :aria-label="t('stage.launchers.ariaLabel')">
          <div class="stage-launcher-wrap">
            <router-link
              to="/studio"
              class="stage-launcher"
              :title="t('stage.launchers.studioTitle')"
              :aria-label="t('stage.launchers.studioAria')"
              @click="handleStudioLauncherClick"
            >
              <svg viewBox="0 0 24 24" width="22" height="22" fill="none"
                stroke="currentColor" stroke-width="2"
                stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 20h9" />
                <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" />
                <path d="m15 5 3 3" />
              </svg>
              <span class="stage-launcher-label">{{ t('stage.launchers.studio') }}</span>
              <span
                v-if="studioActiveJobs > 0"
                class="stage-launcher-busy-dot"
                :title="t('stage.launchers.studioBusy', { count: studioActiveJobs })"
                aria-hidden="true"
              />
            </router-link>
            <div
              v-if="showStudioCoachmark"
              class="stage-launcher-coachmark"
              role="note"
            >
              <span class="stage-launcher-coachmark__title">
                {{ t('stage.launchers.studioCoachmark.title') }}
              </span>
              <span class="stage-launcher-coachmark__body">
                {{ t('stage.launchers.studioCoachmark.body') }}
              </span>
              <button
                type="button"
                class="stage-launcher-coachmark__close"
                :aria-label="t('stage.launchers.studioCoachmark.dismiss')"
                @click="dismissStudioCoachmark"
              >
                ×
              </button>
            </div>
          </div>
          <button
            class="stage-launcher stage-launcher--feed"
            type="button"
            :title="kokoroGramTitle"
            :aria-label="t('stage.launchers.kokoroGramOpen')"
            @click="kokoroGramOpen = true"
          >
            <img
              src="/LumeGramLogo.png"
              alt=""
              class="stage-launcher-logo"
              aria-hidden="true"
            />
            <span class="stage-launcher-label">{{ t('stage.launchers.kokoroGram') }}</span>
            <span
              v-if="lumeGramUnread > 0"
              class="stage-launcher-badge"
              :aria-label="t('stage.launchers.kokoroGramUnreadAria', { count: lumeGramUnread })"
            >{{ formatLumeGramBadge(lumeGramUnread) }}</span>
          </button>
        </nav>
      </div>

      <div class="chat-section">
        <!-- Portrait overlay 專用控制列：透明度 + 收合。橫式用不到（chat 在右側固定寬度） -->
        <div v-if="isPortrait" class="chat-overlay-bar">
          <button
            class="overlay-settings-btn"
            type="button"
            :aria-label="t('stage.overlay.toggleDisplaySettings')"
            :aria-expanded="chatOverlaySettingsOpen"
            @click="chatOverlaySettingsOpen = !chatOverlaySettingsOpen"
          >
            <span aria-hidden="true">◐</span>
            <span>{{ t('stage.overlay.displaySettings') }}</span>
          </button>
          <button
            class="overlay-close-btn"
            :aria-label="t('stage.overlay.collapseChat')"
            @click="collapseChatOverlay"
          >{{ t('stage.overlay.collapseLabel') }}</button>
        </div>
        <div v-if="isPortrait && chatOverlaySettingsOpen" class="chat-overlay-settings">
          <label class="opacity-control" :title="t('stage.overlay.chatOpacity')">
            <span>{{ t('stage.overlay.chatOpacity') }}</span>
            <input
              v-model.number="chatOpacity"
              type="range"
              min="0.35"
              max="1"
              step="0.05"
              class="field-range opacity-slider"
              :aria-label="t('stage.overlay.chatOpacity')"
            />
          </label>
        </div>

        <ChatPanel
          :character="selectedCharacter"
          :conversation-id="conversationId"
          :messages="messages"
          :loading-history="historyLoading"
          :show-layout-toggle="!isPortrait"
          :stage-layout-mode="stageLayoutMode"
          @conversation-update="handleConversationUpdate"
          @conversation-id-learned="conversationId = $event"
          @toggle-stage-layout="toggleStageLayout"
        />
      </div>

      <!-- Chat 收合時的浮動重開按鈕（只在 portrait + hidden 顯示） -->
      <button
        v-if="isPortrait && !chatVisible"
        class="chat-fab"
        :aria-label="t('stage.overlay.expandChat')"
        @click="chatVisible = true"
      >💬</button>
    </main>

    <button
      class="sidebar-toggle"
      :aria-label="sidebarOpen ? t('stage.sidebarToggle.collapse') : t('stage.sidebarToggle.expand')"
      @click="sidebarOpen = !sidebarOpen"
    >
      {{ sidebarOpen ? '◀︎' : '▶︎' }}
    </button>

    <KokoroGramOverlay
      :open="kokoroGramOpen"
      @close="kokoroGramOpen = false"
    />

    <MemoirOverlay
      :open="memoirOpen"
      :character="memoirCharacter"
      @close="memoirOpen = false"
    />
  </div>
</template>

<style scoped>
.stage-layout {
  display: flex;
  height: var(--app-height);
  width: 100vw;
  position: relative;
  padding-left: var(--safe-area-left);
  padding-right: var(--safe-area-right);
}

/* ---------- Sidebar ---------- */
.sidebar {
  width: var(--sidebar-width);
  min-width: var(--sidebar-width);
  height: 100%;
  background: var(--color-bg-secondary);
  border-right: 1px solid var(--color-border);
  overflow-y: auto;
  padding-top: var(--safe-area-top);
  padding-bottom: var(--safe-area-bottom);
  transition: margin-left 0.3s ease, transform 0.3s ease;
}

.sidebar-collapsed .sidebar {
  margin-left: calc(-1 * var(--sidebar-width));
}

.sidebar-backdrop {
  display: none;
}

/* ---------- Main area ---------- */
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  min-height: 0;
}

.stage-section {
  flex: 1;
  min-height: 200px;
  position: relative;
  background: var(--color-bg);
  padding-top: var(--safe-area-top);
}

.stage-launcher-row {
  position: absolute;
  top: calc(12px + var(--safe-area-top));
  left: calc(56px + var(--safe-area-left));
  right: calc(12px + var(--safe-area-right));
  z-index: 10;
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: flex-end;
  gap: 8px;
  pointer-events: none;
}

.stage-launcher-wrap {
  position: relative;
  display: inline-flex;
  pointer-events: auto;
}

.stage-launcher {
  position: relative;
  min-width: 0;
  min-height: 42px;
  padding: 0 13px 0 10px;
  border-radius: 21px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(23, 37, 56, 0.78);
  backdrop-filter: blur(12px) saturate(1.2);
  -webkit-backdrop-filter: blur(12px) saturate(1.2);
  color: #fff;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  text-decoration: none;
  font: inherit;
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.32);
  cursor: pointer;
  pointer-events: auto;
  transition: transform 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
}

.stage-launcher:hover {
  transform: translateY(-1px);
  background: rgba(35, 59, 76, 0.88);
  box-shadow: 0 5px 14px rgba(0, 0, 0, 0.42);
}

.stage-launcher:active {
  transform: translateY(0) scale(0.98);
}

.stage-launcher-label {
  display: inline;
  white-space: nowrap;
}

.stage-launcher-busy-dot {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--color-spark, #ffc861);
  box-shadow: 0 0 0 2px rgba(23, 37, 56, 0.9);
  animation: stage-launcher-busy-pulse 1.6s ease-in-out infinite;
}

@keyframes stage-launcher-busy-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.55; transform: scale(0.85); }
}

.stage-launcher-coachmark {
  position: absolute;
  top: calc(100% + 10px);
  right: 0;
  z-index: 12;
  width: min(260px, calc(100vw - 32px));
  padding: 11px 36px 11px 12px;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 8px;
  background: rgba(13, 23, 34, 0.94);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.34);
  color: #fff;
  display: grid;
  gap: 4px;
  pointer-events: auto;
}

.stage-launcher-coachmark::before {
  content: '';
  position: absolute;
  top: -5px;
  right: 24px;
  width: 10px;
  height: 10px;
  border-left: 1px solid rgba(255, 255, 255, 0.18);
  border-top: 1px solid rgba(255, 255, 255, 0.18);
  background: rgba(13, 23, 34, 0.94);
  transform: rotate(45deg);
}

.stage-launcher-coachmark__title {
  font-size: 12px;
  font-weight: 750;
  line-height: 1.35;
}

.stage-launcher-coachmark__body {
  color: rgba(255, 255, 255, 0.78);
  font-size: 11px;
  line-height: 1.45;
}

.stage-launcher-coachmark__close {
  position: absolute;
  top: 7px;
  right: 7px;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: rgba(255, 255, 255, 0.72);
  font: inherit;
  font-size: 18px;
  line-height: 24px;
  text-align: center;
  cursor: pointer;
}

.stage-launcher-coachmark__close:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}

.stage-launcher--feed {
  border: none;
  padding-left: 8px;
}

.stage-launcher-logo {
  display: block;
  width: 26px;
  height: 26px;
  object-fit: contain;
  pointer-events: none;
}

.stage-launcher-badge {
  position: absolute;
  top: -6px;
  right: -6px;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: #ff3b30;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  line-height: 18px;
  text-align: center;
  box-shadow: 0 0 0 2px var(--color-bg, #1a1a2e);
  pointer-events: none;
  letter-spacing: 0;
}

@media (max-width: 480px) {
  .stage-launcher-row {
    gap: 6px;
  }

  .stage-launcher {
    min-height: 38px;
    padding: 0 10px 0 8px;
    font-size: 12px;
  }

  .stage-launcher-logo {
    width: 23px;
    height: 23px;
  }

  .stage-launcher-coachmark {
    right: auto;
    left: 50%;
    transform: translateX(-50%);
    width: min(232px, calc(100vw - 24px));
  }

  .stage-launcher-coachmark::before {
    right: auto;
    left: calc(50% - 5px);
  }
}

.chat-section {
  height: 320px;
  min-height: 200px;
  border-top: 1px solid var(--color-border);
  background: var(--color-bg-secondary);
  display: flex;
  flex-direction: column;
  min-height: 0;
}

/* ---------- 桌面橫式（≥960px landscape） ---------- */
@media (min-width: 960px) and (orientation: landscape) {
  .main-area {
    flex-direction: row;
  }

  .stage-section {
    flex: 1;
    min-height: 0;
    transition: flex-basis 0.2s ease;
  }

  .chat-section {
    width: 420px;
    min-width: 340px;
    max-width: 45vw;
    height: 100%;
    border-top: none;
    border-left: 1px solid var(--color-border);
  }

  /* chat-centric：反轉兩欄的 flex 角色（chat 吃滿版、stage 變固定窄
     欄），不做 DOM 順序交換 —— stage 保持在左、chat 保持在右，
     launcher row（stage-launcher-row）維持在左側，滑鼠移動習慣不被打
     亂。窄欄寬度沿用現況 .chat-section 同一組數值，確保 launcher 兩顆
     按鈕與 coachmark 在此欄寬下不會比現況更窄。 */
  .chat-centric .stage-section {
    flex: none;
    width: 420px;
    min-width: 340px;
    max-width: 45vw;
    border-left: 1px solid var(--color-border);
  }

  .chat-centric .chat-section {
    flex: 1;
    width: auto;
    min-width: 0;
    max-width: none;
    border-left: none;
  }
}

/* ---------- 手機／平板直式：overlay 佈局 ----------
   舞台滿版當背景，chat 也是滿版半透明浮層疊在上面；
   用 FAB 切換顯示／隱藏，用 slider 調 chat 透明度把舞台透出來。
   不保留 splitter — 要看圖就按收合，要聊天就展開 chat。
*/
@media (orientation: portrait) {
  .main-area {
    position: relative;
  }

  /* 舞台佔滿整個 main-area */
  .stage-section {
    position: absolute;
    inset: 0;
    z-index: 1;
    flex: none;
    min-height: 0;
    width: 100%;
  }

  /* Chat 浮層：滿版覆蓋，用背景 alpha 透出舞台 */
  .chat-section {
    position: absolute;
    inset: 0;
    /* 覆寫 base style 的 height: 320px / min-height: 200px；
       absolute 元素若 top+bottom+height 同時指定，spec 會讓 height 贏、
       bottom 被忽略，結果就只長到 320px。強制改回 auto 讓 inset 生效。 */
    height: auto;
    min-height: 0;
    max-height: none;
    z-index: 3;
    /* --color-bg-secondary 是 #16213e → rgb(22, 33, 62)。
       這裡用 CSS var 控透明度，比 color-mix() 相容性好。 */
    background: rgba(22, 33, 62, var(--chat-opacity, 0.78));
    backdrop-filter: blur(var(--chat-blur, 14px)) saturate(1.2);
    -webkit-backdrop-filter: blur(var(--chat-blur, 14px)) saturate(1.2);
    border-top: none;
    box-shadow: none;
    display: flex;
    flex-direction: column;
    transition: transform 0.3s ease, opacity 0.3s ease;
  }

  /* Chat 收合 → 整塊往下滑出畫面；舞台吃滿整個 viewport */
  .chat-hidden .chat-section {
    transform: translateY(100%);
    opacity: 0;
    pointer-events: none;
  }

  /* Overlay 控制列：首屏只保留顯示設定開關與收合按鈕。 */
  .chat-overlay-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 12px;
    padding-top: calc(6px + var(--safe-area-top));
    background: rgba(0, 0, 0, 0.25);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    flex-shrink: 0;
  }

  .overlay-settings-btn {
    display: flex;
    align-items: center;
    gap: 7px;
    flex: 1;
    min-height: 28px;
    padding: 4px 10px;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.06);
    font-size: 11px;
    color: var(--color-text);
    cursor: pointer;
    transition: background 0.15s;
  }

  .overlay-settings-btn:hover,
  .overlay-settings-btn[aria-expanded="true"] {
    background: rgba(255, 255, 255, 0.13);
  }

  .overlay-close-btn {
    background: rgba(255, 255, 255, 0.08);
    color: var(--color-text);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 6px;
    font-size: 11px;
    padding: 4px 10px;
    min-height: 28px;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.15s;
  }

  .overlay-close-btn:hover {
    background: rgba(255, 255, 255, 0.15);
  }

  .chat-overlay-settings {
    padding: 8px 12px;
    background: rgba(0, 0, 0, 0.2);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    flex-shrink: 0;
  }

  .opacity-control {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 11px;
    color: var(--color-text-secondary);
  }

  .opacity-control span {
    white-space: nowrap;
  }

  .opacity-slider {
    flex: 1;
    min-width: 0;
  }

  /* 重開 chat 的 floating action button */
  .chat-fab {
    position: absolute;
    right: calc(16px + var(--safe-area-right));
    bottom: calc(16px + var(--safe-area-bottom));
    z-index: 5;
    width: 52px;
    height: 52px;
    border-radius: 50%;
    background: var(--color-primary);
    color: #fff;
    border: none;
    font-size: 22px;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.5);
    cursor: pointer;
    transition: transform 0.15s, background 0.15s;
  }

  .chat-fab:hover {
    background: var(--color-primary-light);
    transform: scale(1.05);
  }

  .chat-fab:active {
    transform: scale(0.96);
  }
}

/* Overlay bar / FAB 預設隱藏（landscape 不顯示） */
.chat-overlay-bar,
.chat-fab {
  display: none;
}
@media (orientation: portrait) {
  .chat-overlay-bar {
    display: flex;
  }
  .chat-fab {
    display: flex;
    align-items: center;
    justify-content: center;
  }
}

/* ---------- 窄螢幕：sidebar overlay ---------- */
@media (max-width: 899px) {
  .sidebar {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    z-index: 200;
    box-shadow: 4px 0 20px rgba(0, 0, 0, 0.5);
    transform: translateX(0);
  }

  .sidebar-collapsed .sidebar {
    margin-left: 0;
    transform: translateX(-100%);
  }

  .sidebar-backdrop {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 150;
    background: rgba(0, 0, 0, 0.5);
    animation: fadeIn 0.25s ease;
  }
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

/* ---------- Sidebar toggle ---------- */
.sidebar-toggle {
  position: absolute;
  top: calc(12px + var(--safe-area-top));
  left: var(--sidebar-width);
  z-index: 250;
  background: var(--color-surface);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 0 6px 6px 0;
  padding: 10px 8px;
  font-size: 14px;
  transition: left 0.3s ease;
  min-width: 32px;
  min-height: 44px;
}

.sidebar-collapsed .sidebar-toggle {
  left: 0;
}

@media (max-width: 899px) {
  .sidebar-collapsed .sidebar-toggle {
    left: var(--safe-area-left);
  }

  .sidebar-toggle {
    left: var(--sidebar-width);
  }

  /* 收合時箭頭浮在最左緣（left:0），其下緣會探進 chat 頂列；
     替頂部兩條（portrait overlay 控制列 + ChatPanel header）讓出左側
     寬度，避免蓋住角色名第一個字。sidebar 展開時箭頭移到 sidebar 右側，
     不需讓位。 */
  .sidebar-collapsed .chat-overlay-bar {
    padding-left: calc(44px + var(--safe-area-left));
  }

  .sidebar-collapsed .chat-section :deep(.chat-header) {
    padding-left: calc(44px + var(--safe-area-left));
  }
}
</style>
