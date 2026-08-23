<script lang="ts">
/**
 * 背景捲動鎖的**跨 instance** 狀態。
 *
 * 這一段之所以是普通的 `<script>` 而不是 `<script setup>`：`<script setup>`
 * 整段會被編譯進 `setup()`，寫在它「頂層」的 `let` 其實是每個 instance 各一
 * 份。原本這兩個變數就在那裡，註解卻宣稱「模組層級的計數…讓最後一個關窗的人
 * 才還原」——那個保證從來不存在，計數器恆為 0→1→0，只是巢狀開關剛好都是
 * LIFO 才沒出事。真正會壞的是非 LIFO：外層先關時，它會把自己開窗前的
 * `overflow` 寫回去，而內層還開著——背景在一個 modal 底下又能捲了。
 *
 * 搬到這裡之後保證才成立：`previousBodyOverflow` 是**第一個**上鎖的人看到的
 * 值，`scrollLockCount` 歸零（最後一個關窗的人）才還原。
 *
 * 對照組：`holdsScrollLock` 刻意留在 `<script setup>`——它問的是「我這個
 * instance 有沒有持有鎖」，搬上來會讓第二個浮窗看到「已經持有」而整個跳過
 * 計數，先關的那個又直接把鎖解掉。同一段程式碼裡兩種作用域各有理由。
 */
let scrollLockCount = 0
let previousBodyOverflow = ''
</script>

<script setup lang="ts">
/**
 * 放大看圖的唯一入口（LB 系列，計畫 `IMAGE_LIGHTBOX_PLAN.md`）。
 *
 * 取代的是「點圖 → `target="_blank"` 開新分頁」。相簿要連續看圖時那條路是
 * 一張一張開再一張一張關；手機上每開一張多一個分頁，回不到原本的頁面。
 *
 * 這個元件刻意很薄：所有**有決定**的邏輯都在 `@/utils/lightbox`，因為前端
 * 測試 harness 是 SSR（沒有 jsdom），鍵盤、手勢、焦點、`history` 在測試裡
 * 一行都跑不到。這裡留下的只有「把瀏覽器事件翻成那些純函式的輸入」。
 *
 * 三條紅線寫在程式碼裡看不出來，所以寫在這裡：
 *
 * 1. **只掛 active 一張。** 一張 full 圖的解碼點陣是 `1024x1536x4` ≈ 6 MB，
 *    元素握著 URL 就一直付。舞台（`ImageStage`）掛 ±1 是因為它做 cross-fade
 *    ——轉場需要兩端同時在 DOM；浮窗沒有這個需求，照抄就是白付記憶體。
 *    鄰居只用 `new Image()` 預熱 HTTP 快取，不進 DOM。
 * 2. **「開原圖」不得消失。** 新分頁目前是唯一的「看原檔／另存」路徑，
 *    `href` 指原始 URL（不是 `?v=full` 那個重編碼變體），並過 `safeMediaHref()`。
 * 3. **圖片一律走 `UiImage variant="full"`**，不手刻 `<img>`。
 * 4. **鍵盤的攔截責任在浮窗，不在宿主。** 浮窗以 **capture 階段**掛在 `window`
 *    上，對它自己處理的鍵（Esc、以及有導覽時的 ←／→）呼叫 `stopPropagation()`。
 *    宿主不必知道浮窗存在，也不該為了它加讓位守衛——那種模式漏掉一個宿主
 *    不會有任何測試變紅（LB4 的 `KokoroGramOverlay` 就漏了：按 Esc 想關大圖
 *    卻把整個動態牆一起關掉，捲動位置與已載入分頁全部歸零）。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CloseOutlined, LeftOutlined, RightOutlined } from '@ant-design/icons-vue'

import UiImage, { imageVariantUrl } from './UiImage.vue'
import { releasedImageSrc } from '@/utils/offscreenImageRelease'
import { safeMediaHref } from '@/utils/safeMediaUrl'
import {
  LIGHTBOX_PREFETCH_DEBOUNCE_MS,
  classifyLightboxKey,
  classifyLightboxSwipe,
  lightboxItemAt,
  lightboxKeyStopsPropagation,
  lightboxLoadMoreState,
  lightboxNeighbourUrls,
  lightboxPrefetchSettled,
  lightboxResumeAction,
  shouldSendLightboxLoadMore,
  shouldShowLightboxCounter,
  shouldShowLightboxNav,
  shouldTrackLightboxPointer,
  stepLightboxIndex,
  wrapLightboxIndex,
  type LightboxItem,
  type LightboxPendingLoad,
} from '@/utils/lightbox'

const props = withDefaults(
  defineProps<{
    /** 開關。關閉時整個 overlay 不掛載，active 圖的點陣隨之釋放。 */
    visible: boolean
    /** 圖片清單。相簿等呼叫端可以讓它非同步成長。 */
    items?: readonly LightboxItem[]
    /** 目前索引，配 `v-model:index`。越界會被安全收進範圍。 */
    index?: number
    /** 清單之外還有沒載入的項目；為真時走到末端會發 `load-more` 而不是循環。 */
    hasMore?: boolean
    /** 續載進行中，畫面顯示狀態列。 */
    loadingMore?: boolean
    /**
     * 上一次續載的失敗訊息。
     *
     * 有這個 prop 是因為浮窗蓋住了整個呼叫端面板——呼叫端原本的錯誤列就在
     * overlay 底下，續載失敗時玩家只會看到載入字樣消失、然後什麼都沒發生。
     * 非空＝畫面顯示失敗與一顆「重試」，而不是無聲卡在最後一張。
     */
    loadMoreError?: string | null
    /** 對話框的 aria-label；不給就用通用的「圖片檢視」。 */
    label?: string
    /** 佔位比例的預設值，項目自己帶的 `aspectRatio` 優先。 */
    aspectRatio?: string | number | null
  }>(),
  {
    items: () => [],
    index: 0,
    hasMore: false,
    loadingMore: false,
    loadMoreError: null,
    label: '',
    aspectRatio: null,
  },
)

const emit = defineEmits<{
  close: []
  'update:index': [value: number]
  /** 走到已載入的最後一張又要往前——呼叫端去載下一頁。監聽寫 `@load-more`。 */
  loadMore: []
}>()

const { t } = useI18n()

/**
 * Teleport 需要一個真的 `body`；SSR（以及以 SSR 為基礎的元件測試）裡沒有，
 * 對話框會靜靜地渲染到空氣裡。退回原地渲染讓兩邊的 markup 都是真的。
 * 慣例沿用 `StudioGuideModal`。
 */
const canTeleport = typeof document !== 'undefined'

const dialogEl = ref<HTMLElement | null>(null)

const total = computed(() => props.items.length)
const activeIndex = computed(() =>
  total.value === 0 ? 0 : wrapLightboxIndex(props.index, total.value),
)
const activeItem = computed(() => lightboxItemAt(props.items, props.index))

/**
 * 顯示與「開原圖」共用同一個經過護欄的 URL。
 *
 * 共用是刻意的：兩處若各自解析，就會出現「圖看得到但連結被擋」或反過來的
 * 分歧狀態。`safeMediaHref()` 是 S2 的第二層防線——`.lumebackup` 還原會落地
 * 攻擊者提供的列，`javascript:` / `data:` 這類 scheme 一律塌成空字串。
 * 代價是 `blob:` 也進不來，而那正是計畫 §6 明文排除的上傳暫存預覽。
 */
const activeUrl = computed(() => safeMediaHref(activeItem.value?.url))

/**
 * 空字串而不是 `null`：`UiImage` 把空 `src` 渲染成**完全沒有 `src` 屬性**，
 * 那是釋放解碼點陣的正路（`src=""` 會解析成目前文件的 URL，瀏覽器會把 SPA
 * 自己的 HTML 餵給圖片解碼器）。`visible` 為假時一併放掉——`v-if` 已經會拆掉
 * 元素，這裡是給「呼叫端把 overlay 留在 `<KeepAlive>` 之類」的第二層保險，
 * 同時也順手處理了「項目沒有 url」這個真實邊界。
 */
const activeSrc = computed(() =>
  releasedImageSrc(activeUrl.value, !props.visible),
)

const activeAlt = computed(() => {
  const item = activeItem.value
  if (item?.alt) return item.alt
  if (item?.caption) return item.caption
  return t('lightbox.imageAlt', { n: activeIndex.value + 1 })
})

const activeAspectRatio = computed(() => {
  const own = activeItem.value?.aspectRatio
  if (own !== undefined && own !== null && own !== '') return own
  if (props.aspectRatio !== null && props.aspectRatio !== '') return props.aspectRatio
  return undefined
})

/** 原始 URL，不是尺寸變體（`?v=full` 是重編碼過的）；被護欄擋掉就不畫連結。 */
const originalHref = computed(() => activeUrl.value)

const showNav = computed(() => shouldShowLightboxNav(total.value, props.hasMore))
const showCounter = computed(() =>
  shouldShowLightboxCounter(total.value, props.hasMore),
)
const positionText = computed(() =>
  t('lightbox.position', { current: activeIndex.value + 1, total: total.value }),
)
const positionAria = computed(() =>
  t('lightbox.positionAria', { current: activeIndex.value + 1, total: total.value }),
)
const dialogLabel = computed(() => props.label || t('lightbox.label'))

const loadMoreState = computed(() =>
  lightboxLoadMoreState({
    loadingMore: props.loadingMore,
    error: props.loadMoreError,
  }),
)

/** 呼叫端的訊息通常帶伺服端 detail，比通用句有用；沒有才退回通用句。 */
const loadMoreErrorText = computed(
  () => props.loadMoreError?.trim() || t('lightbox.loadMoreFailed'),
)

// ---------------------------------------------------------------- 導覽

/**
 * 還在等的那次續載：**當時有幾張，以及玩家當時停在哪一張**。
 *
 * 續載是非同步的，玩家按下「下一張」到清單真的變長中間隔著一次 HTTP 往返，
 * 所以要記住當時的長度，回來才知道有沒有長。但長度只是必要條件——那段空窗裡
 * 浮窗仍然可以導覽，所以還要記住位置，回來時才知道這一步**還欠不欠他**。
 * 判定全在 `lightboxResumeAction`。
 */
const pendingLoad = ref<LightboxPendingLoad | null>(null)

/**
 * 換索引的唯一出口，也是等待記號唯一的作廢點。
 *
 * 送得出這一次換圖，就代表玩家自己已經走了一步（左右鍵、箭頭鈕、swipe 最後
 * 都收束到這裡），被推遲的那一步當場作廢——他要的下一張已經被他自己的操作
 * 取代。少了這一行，續載回來時浮窗會照著一個過期的記號把畫面往前推一格，而
 * 玩家早就翻到別處了。
 *
 * 記號記位置（而不是只靠這裡清）的理由見 `isLightboxResumeOwed`：呼叫端直接
 * 改 `index` prop 時，這一支根本不會被呼叫到。
 */
function setIndex(next: number) {
  if (next === props.index) return
  pendingLoad.value = null
  emit('update:index', next)
}

function step(delta: -1 | 1) {
  const result = stepLightboxIndex({
    index: activeIndex.value,
    total: total.value,
    delta,
    hasMore: props.hasMore,
  })
  if (result.outcome === 'move') {
    setIndex(result.index)
    return
  }
  if (result.outcome === 'load-more') requestLoadMore()
}

/**
 * 唯一一處對呼叫端喊「去載下一頁」。失敗後的「重試」鍵走的也是這一支——那顆
 * 鍵的語意就是「再按一次下一張」，只是不必玩家自己去猜。
 *
 * 記號一律先記下（「按下去的當時有幾張、他站在哪一張」），**即使這一次因為
 * 已有請求在飛而不重送**：玩家等的仍是那一次回來之後的下一張。去重只看呼叫端
 * 回報的 `loadingMore`，不看這個記號，理由見 `shouldSendLightboxLoadMore`。
 */
function requestLoadMore() {
  if (!props.hasMore) return
  pendingLoad.value = { total: total.value, index: activeIndex.value }
  if (!shouldSendLightboxLoadMore({
    hasMore: props.hasMore,
    loadingMore: props.loadingMore,
  })) return
  emit('loadMore')
}

function goPrev() {
  step(-1)
}

function goNext() {
  step(1)
}

/**
 * 清單長大了——那次還在等的續載該不該把玩家往前推一格。
 *
 * 判定整支在 `lightboxResumeAction`：這是浮窗唯一會自己動畫面的地方，判錯的
 * 症狀是「玩家沒碰任何東西，圖自己跳掉」，所以規則不留在這裡。
 */
watch(total, (after) => {
  const action = lightboxResumeAction({
    pending: pendingLoad.value,
    current: activeIndex.value,
    totalAfter: after,
  })
  if (action.outcome === 'wait') return
  pendingLoad.value = null
  if (action.outcome === 'move') setIndex(action.index)
})

/**
 * 續載結束卻沒長（空的一頁、或 `has_more` 本來就報錯了）——把等待狀態收掉，
 * 玩家再按一次就會重試。不在這裡自動換圖：見 `resumeLightboxAfterLoad` 的註解。
 */
watch(
  () => props.loadingMore,
  (now, was) => {
    const pending = pendingLoad.value
    if (was && !now && pending !== null) {
      if (total.value <= pending.total) pendingLoad.value = null
    }
  },
)

/** 集合封閉了就沒有續載這回事，等待記號留著只會誤導後面的判斷。 */
watch(
  () => props.hasMore,
  (hasMore) => {
    if (!hasMore) pendingLoad.value = null
  },
)

/**
 * 失敗回報進來就把等待記號收掉。上面那條 `loadingMore` 的 watch 通常已經做過
 * 一次，但那條依賴「呼叫端有回報載入狀態」；只回報錯誤的呼叫端若留著記號，
 * 之後任何來源（例如面板自己捲到底續載）讓清單長大，都會把玩家莫名往前推一格。
 */
watch(
  () => props.loadMoreError,
  (error) => {
    if (typeof error === 'string' && error.trim() !== '') pendingLoad.value = null
  },
)

function requestClose() {
  emit('close')
}

function onOverlayClick(event: MouseEvent) {
  // 只有點到背景本身才關；點在圖片、圖說或按鈕上不算。
  if (event.target !== event.currentTarget) return
  // 旗標的歸零點在 `onPointerDown`（無條件，見那裡的註解）——這一支只有在
  // 點到背景時才跑得到，靠它收拾就會漏掉「起訖都在圖片上的橫滑」。這裡再收
  // 一次只是把它消費掉。
  if (swipeHandled) {
    swipeHandled = false
    return
  }
  requestClose()
}

// ---------------------------------------------------------------- 手勢

const CAPTION_SELECTOR = '.ui-lightbox__caption'

let swipeStartX: number | null = null
let swipeStartY: number | null = null
/** 這一次拖曳已經被當成手勢處理過，隨後的 click 不該再被解讀成「點背景」。 */
let swipeHandled = false
/**
 * 目前按著的 pointer id。雙指縮放要能被認出來（見
 * `shouldTrackLightboxPointer`），而 `PointerEvent` 是一指一串事件，不像
 * `TouchEvent` 每次都帶完整的 `touches`——只能自己記。
 */
const activePointerIds = new Set<number>()

function forgetSwipeStart() {
  swipeStartX = null
  swipeStartY = null
}

/** 起點落在圖說裡＝玩家在選字。DOM 查詢留在元件，判斷在純函式。 */
function startedInCaption(event: PointerEvent): boolean {
  const target = event.target
  if (!(target instanceof Element)) return false
  return target.closest(CAPTION_SELECTOR) !== null
}

/** 畫面被 pinch 放大了沒有。沒有 `visualViewport`（舊 WebView）就當作沒放大。 */
function currentViewportScale(): number {
  if (typeof window === 'undefined') return 1
  return window.visualViewport?.scale ?? 1
}

function onPointerDown(event: PointerEvent) {
  // 新的一次接觸＝上一次拖曳的判定作廢，**無條件**，而且要在下面任何一條
  // return 之前。旗標原本只在「開始記錄手勢」那一支跑到底時才歸零，另一頭又
  // 只有 `onOverlayClick` 會消費它——而那支在點到的不是背景本身時先 return。
  // 於是橫滑起訖都落在圖片上（後續 click 的 target 是圖片，不是 overlay）時，
  // 旗標就留在 true：玩家接著點背景想關窗，第一次被靜靜吞掉，要點第二次。
  swipeHandled = false
  // primary＝這一輪手勢的第一次接觸。順手清掉上一輪可能漏收的 id（pointerup
  // 落在浮窗之外時收不到），否則一個洩漏的 id 會讓之後每一次手勢都被當成多指。
  if (event.isPrimary) activePointerIds.clear()
  activePointerIds.add(event.pointerId)
  if (
    !shouldTrackLightboxPointer({
      pointerType: event.pointerType,
      button: event.button,
      isPrimary: event.isPrimary,
      activePointerCount: activePointerIds.size,
      viewportScale: currentViewportScale(),
      withinCaption: startedInCaption(event),
    })
  ) {
    // 第二指落下時要把**已經記到一半**的起點也丟掉：那是縮放的開始，不是換圖。
    forgetSwipeStart()
    return
  }
  swipeStartX = event.clientX
  swipeStartY = event.clientY
}

function onPointerUp(event: PointerEvent) {
  activePointerIds.delete(event.pointerId)
  if (swipeStartX === null || swipeStartY === null) return
  const dx = event.clientX - swipeStartX
  const dy = event.clientY - swipeStartY
  forgetSwipeStart()
  const action = classifyLightboxSwipe({ dx, dy })
  if (action === 'ignore') return
  swipeHandled = true
  if (action === 'prev') goPrev()
  else if (action === 'next') goNext()
  else requestClose()
}

function onPointerCancel(event: PointerEvent) {
  // 瀏覽器接手成原生手勢（pinch-zoom、pan）時發的就是 pointercancel。
  activePointerIds.delete(event.pointerId)
  forgetSwipeStart()
}

// ---------------------------------------------------------------- 鍵盤與焦點

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'

function focusableNodes(): HTMLElement[] {
  const root = dialogEl.value
  if (!root) return []
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
}

/**
 * Tab 不得跑到背景去。焦點跑掉的浮窗在鍵盤操作者眼裡等於沒有浮窗——他們會
 * 一路 Tab 進看不見的頁面裡。
 */
function trapTab(event: KeyboardEvent) {
  const nodes = focusableNodes()
  const root = dialogEl.value
  if (!root) return
  if (nodes.length === 0) {
    event.preventDefault()
    root.focus()
    return
  }
  const first = nodes[0]
  const last = nodes[nodes.length - 1]
  const active = typeof document === 'undefined' ? null : document.activeElement
  const inside = active instanceof HTMLElement && root.contains(active)
  if (!inside) {
    event.preventDefault()
    ;(event.shiftKey ? last : first).focus()
    return
  }
  if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus()
    return
  }
  if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

/**
 * `window` 的 **capture 階段**監聽（見檔頭紅線 4）。
 *
 * 事件路徑是 window → document → … → target，再回程 bubble 回 window。掛在
 * 路徑第一站的 capture 監聽是**最先**跑的，在這裡 `stopPropagation()` 之後，
 * 事件既到不了 target，也回不到宿主掛在 `window`／`document` 上的 bubble
 * 監聽（`KokoroGramOverlay`、`MemoirOverlay`、`CharacterImagesPanel`、
 * `CharacterCardGalleryModal`、`StageNudgeControl`… 全部是 bubble）。
 *
 * 用 `stopPropagation()` 而不是 `stopImmediatePropagation()`：同一個節點、
 * 同一個階段的其他監聽仍該跑（例如巢狀開了兩層浮窗時的外層），要擋的只是
 * 「路徑上的下一站」。
 */
function onKeydown(event: KeyboardEvent) {
  const action = classifyLightboxKey({
    key: event.key,
    visible: props.visible,
    showNav: showNav.value,
  })
  if (action === 'ignore') return

  if (lightboxKeyStopsPropagation(action)) event.stopPropagation()

  switch (action) {
    case 'close':
      event.preventDefault()
      requestClose()
      return
    case 'trap-tab':
      trapTab(event)
      return
    case 'prev':
      event.preventDefault()
      goPrev()
      return
    case 'next':
      event.preventDefault()
      goNext()
      return
    default:
      // `swallow`：沒有導覽時的 ←／→。不 `preventDefault()`——瀏覽器原生的
      // 捲動仍該留給浮窗自己的可捲動區域（長圖說）。
  }
}

/** 開窗前的焦點，關窗時還回去。 */
let returnFocusTo: HTMLElement | null = null

function captureReturnFocus() {
  if (typeof document === 'undefined') return
  const active = document.activeElement
  returnFocusTo = active instanceof HTMLElement ? active : null
}

function restoreReturnFocus() {
  const target = returnFocusTo
  returnFocusTo = null
  if (!target) return
  if (typeof document !== 'undefined' && !document.contains(target)) return
  target.focus()
}

// ---------------------------------------------------------------- 背景捲動鎖

/**
 * 這個 **instance** 有沒有持有鎖，避免同一個元件重複加減計數。
 *
 * 計數與「上鎖那一刻的 overflow」在上面那個普通 `<script>` 區塊裡（真正的模組
 * 層級，理由見該處）；只有這一個旗標必須是 per-instance。LB7 會在既有 modal
 * 之上再開一層浮窗，那兩層是兩個 instance，各自記各自的持有狀態。
 */
let holdsScrollLock = false

function lockBodyScroll() {
  if (typeof document === 'undefined' || holdsScrollLock) return
  if (scrollLockCount === 0) {
    previousBodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
  }
  scrollLockCount += 1
  holdsScrollLock = true
}

function unlockBodyScroll() {
  if (!holdsScrollLock) return
  holdsScrollLock = false
  scrollLockCount = Math.max(0, scrollLockCount - 1)
  if (scrollLockCount === 0 && typeof document !== 'undefined') {
    document.body.style.overflow = previousBodyOverflow
  }
}

// ---------------------------------------------------------------- 手機返回鍵

/**
 * 開窗時 push 一個 history entry，`popstate` 時關窗而不是離開頁面。
 *
 * 這條是整張票的重點之一：缺了它，手機使用者的直覺動作（返回鍵）會從「關掉
 * 這個分頁」變成「離開整個 app」——比原本的新分頁更糟。
 *
 * 保留 `history.state` 的既有內容：vue-router 的 `createWebHistory` 把自己的
 * 記帳（`position` / `current` / `scroll`…）放在 state 裡，整個蓋掉會讓它在
 * 這個 entry 上失憶。URL 不變（`pushState` 第三參數省略），所以 router 收到
 * popstate 時解析出的是同一條路由，等於沒有導覽。
 */
const LIGHTBOX_HISTORY_FLAG = '__uiLightbox'

let historyEntryPushed = false

function pushHistoryEntry() {
  if (typeof window === 'undefined' || !window.history) return
  try {
    const base =
      window.history.state && typeof window.history.state === 'object'
        ? window.history.state
        : {}
    window.history.pushState({ ...base, [LIGHTBOX_HISTORY_FLAG]: true }, '')
    historyEntryPushed = true
  } catch {
    // 某些內嵌瀏覽器會擋 pushState；沒有返回鍵支援也不該讓浮窗開不起來。
    historyEntryPushed = false
  }
  window.addEventListener('popstate', onPopState)
}

function onPopState() {
  // entry 已經被瀏覽器彈掉了，別再 back 一次把使用者送出這一頁。
  historyEntryPushed = false
  if (props.visible) requestClose()
}

function releaseHistoryEntry() {
  if (typeof window === 'undefined') return
  window.removeEventListener('popstate', onPopState)
  if (!historyEntryPushed) return
  historyEntryPushed = false
  // 中途若有別的東西又 push 了新的 entry，最上面那個就不是我們的了——這時
  // 呼叫 back() 會把使用者送離一個他沒要離開的畫面，寧可留下一個多餘的 entry。
  const state = window.history.state
  const ours =
    state && typeof state === 'object' && (state as Record<string, unknown>)[LIGHTBOX_HISTORY_FLAG] === true
  if (!ours) return
  window.history.back()
}

// ---------------------------------------------------------------- 預取

/**
 * 鄰居只預熱 HTTP 快取，**不進 DOM**——見檔頭紅線 1。送達路由帶
 * `immutable` + ETag，所以真的換過去時是快取命中。
 * 預熱的必須是 `UiImage` 待會兒真的會要的那個 URL（`?v=full`），否則暖到的
 * 是另一筆快取條目，等於白做。
 */
function prefetchNeighbours() {
  if (typeof window === 'undefined' || typeof Image === 'undefined') return
  for (const url of lightboxNeighbourUrls(props.items, activeIndex.value, props.hasMore)) {
    const safe = safeMediaHref(url)
    if (!safe) continue
    const warm = new Image()
    warm.decoding = 'async'
    // 預熱是投機的，不該跟玩家正在等的那一張搶頻寬。
    if ('fetchPriority' in warm) {
      ;(warm as HTMLImageElement & { fetchPriority: string }).fetchPriority = 'low'
    }
    warm.src = imageVariantUrl(safe, 'full')
  }
}

/**
 * 預取的靜止窗。**這不是效能微調，是修一個「預取比不預取還慢」的洞**：按住
 * → 三秒（自動重複約 30/s）會送出約 90–180 筆 218 KB 的請求，玩家真正停下來
 * 的那一張排在佇列最後面。理由與數字在 `LIGHTBOX_PREFETCH_DEBOUNCE_MS`。
 *
 * 時間戳用單調時鐘（`performance.now()`）：`Date.now()` 會被系統校時往回撥，
 * 一次校時就能讓靜止判定永遠不成立。
 */
let prefetchTimer: ReturnType<typeof setTimeout> | null = null
let prefetchChangedAt = 0

function monotonicNow(): number {
  if (typeof performance !== 'undefined' && typeof performance.now === 'function') {
    return performance.now()
  }
  return Date.now()
}

function cancelPrefetch() {
  if (prefetchTimer === null) return
  clearTimeout(prefetchTimer)
  prefetchTimer = null
}

function schedulePrefetch() {
  if (typeof window === 'undefined') return
  prefetchChangedAt = monotonicNow()
  if (prefetchTimer !== null) return
  prefetchTimer = setTimeout(runPrefetchIfSettled, LIGHTBOX_PREFETCH_DEBOUNCE_MS)
}

function runPrefetchIfSettled() {
  prefetchTimer = null
  if (!props.visible) return
  const now = monotonicNow()
  if (!lightboxPrefetchSettled({ changedAt: prefetchChangedAt, now })) {
    // 計時器醒來時玩家又按了一下——重排，不要在連按中途送出流量。
    const wait = Math.max(
      1,
      Math.ceil(prefetchChangedAt + LIGHTBOX_PREFETCH_DEBOUNCE_MS - now),
    )
    prefetchTimer = setTimeout(runPrefetchIfSettled, wait)
    return
  }
  prefetchNeighbours()
}

// ---------------------------------------------------------------- 生命週期

/**
 * `capture: true` 是這支監聽的全部重點——不是效能，是**攔截順序**。移掉它，
 * 宿主那些 bubble 監聽會先接到 Esc（見檔頭紅線 4）。移除與新增必須帶同一個
 * `capture` 值，否則 `removeEventListener` 找不到這一筆、監聽永遠留著。
 */
const KEYDOWN_OPTIONS = { capture: true } as const

function bindWindow() {
  if (typeof window === 'undefined') return
  window.addEventListener('keydown', onKeydown, KEYDOWN_OPTIONS)
}

function unbindWindow() {
  if (typeof window === 'undefined') return
  window.removeEventListener('keydown', onKeydown, KEYDOWN_OPTIONS)
}

function onOpen() {
  captureReturnFocus()
  bindWindow()
  lockBodyScroll()
  pushHistoryEntry()
  void nextTick(() => {
    dialogEl.value?.focus()
    schedulePrefetch()
  })
}

function onClose() {
  pendingLoad.value = null
  unbindWindow()
  cancelPrefetch()
  unlockBodyScroll()
  releaseHistoryEntry()
  restoreReturnFocus()
}

watch(
  () => props.visible,
  (visible, was) => {
    if (visible) onOpen()
    else if (was) onClose()
  },
  { immediate: true },
)

watch([activeIndex, () => props.items], () => {
  if (!props.visible) return
  schedulePrefetch()
})

onBeforeUnmount(() => {
  if (props.visible) onClose()
  else {
    unbindWindow()
    cancelPrefetch()
  }
})
</script>

<template>
  <Teleport to="body" :disabled="!canTeleport">
    <div
      v-if="visible"
      ref="dialogEl"
      class="ui-lightbox"
      role="dialog"
      aria-modal="true"
      :aria-label="dialogLabel"
      tabindex="-1"
      @click="onOverlayClick"
      @pointerdown="onPointerDown"
      @pointerup="onPointerUp"
      @pointercancel="onPointerCancel"
      @pointerleave="onPointerCancel"
    >
      <div class="ui-lightbox__bar">
        <span
          v-if="showCounter"
          class="ui-lightbox__counter"
          :aria-label="positionAria"
        >{{ positionText }}</span>
        <span v-else class="ui-lightbox__counter-spacer" aria-hidden="true" />

        <div class="ui-lightbox__tools">
          <!-- 「看原檔／另存」目前唯一的路。href 是原始 URL，不是 ?v=full。 -->
          <a
            v-if="originalHref"
            class="ui-lightbox__tool"
            :href="originalHref"
            target="_blank"
            rel="noopener"
            :title="t('lightbox.openOriginalHint')"
          >{{ t('lightbox.openOriginal') }}</a>
          <button
            type="button"
            class="ui-lightbox__tool ui-lightbox__tool--icon"
            :aria-label="t('lightbox.close')"
            @click="requestClose"
          >
            <CloseOutlined aria-hidden="true" />
          </button>
        </div>
      </div>

      <figure v-if="activeItem" class="ui-lightbox__figure">
        <UiImage
          :key="`${activeIndex}:${activeItem.url}`"
          class="ui-lightbox__image"
          variant="full"
          :src="activeSrc"
          :alt="activeAlt"
          :aspect-ratio="activeAspectRatio"
          :draggable="false"
        />
        <figcaption v-if="activeItem.caption" class="ui-lightbox__caption">
          {{ activeItem.caption }}
        </figcaption>
      </figure>
      <p v-else class="ui-lightbox__empty">{{ t('lightbox.empty') }}</p>

      <template v-if="showNav">
        <button
          type="button"
          class="ui-lightbox__nav ui-lightbox__nav--prev"
          :aria-label="t('lightbox.prev')"
          @click="goPrev"
        >
          <LeftOutlined aria-hidden="true" />
        </button>
        <button
          type="button"
          class="ui-lightbox__nav ui-lightbox__nav--next"
          :aria-label="t('lightbox.next')"
          @click="goNext"
        >
          <RightOutlined aria-hidden="true" />
        </button>
      </template>

      <p
        v-if="loadMoreState === 'loading'"
        class="ui-lightbox__status"
        role="status"
      >{{ t('lightbox.loadingMore') }}</p>
      <!-- 續載失敗。呼叫端自己的錯誤列在 overlay 底下看不到，這裡不說就是無聲
           卡住；重試鍵讓玩家不必自己猜「再按一次下一張」。 -->
      <p
        v-else-if="loadMoreState === 'error'"
        class="ui-lightbox__status ui-lightbox__status--error"
        role="alert"
      >
        <span>{{ loadMoreErrorText }}</span>
        <button
          type="button"
          class="ui-lightbox__retry"
          @click="requestLoadMore"
        >{{ t('lightbox.retry') }}</button>
      </p>
    </div>
  </Teleport>
</template>

<style scoped>
/* 1500：現有 modal 群落在 900–1200（CharacterImagesPanel 1200、
   StoryArcPanel 1200、卡片 gallery 910），最高的 FirstLoginLocaleGate 是
   2000。浮窗必須疊得過前者（LB5／LB7 就是那個情境），又不該蓋掉語系閘。 */
.ui-lightbox {
  position: fixed;
  inset: 0;
  height: 100dvh;
  z-index: 1500;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  padding:
    max(56px, var(--safe-area-top))
    max(16px, var(--safe-area-right))
    max(24px, var(--safe-area-bottom))
    max(16px, var(--safe-area-left));
  background: rgba(0, 0, 0, 0.86);
  backdrop-filter: blur(6px);
  /* 拖曳是換圖與關窗的手勢，不該順手選到圖說或把圖片拖成一個殘影。 */
  user-select: none;
  /* 直向留給瀏覽器（下滑關窗走 pointer 判定，不依賴這裡），橫向由我們接手。
     `pinch-zoom` **不能省**：計畫 §4.2 明文「雙指縮放交給瀏覽器原生」，而
     `pan-y` 的語法值不含它——只寫 `pan-y` 等於整個浮窗不能雙指放大。改版前
     那張圖是開新分頁的原圖、可以 pinch 到 100%，把它做壞就是拆掉「看清楚
     原圖」這個浮窗存在的理由。（`index.html` 的 viewport 沒有
     `user-scalable=no`，`style.css` 給的是 `manipulation`，瀏覽器層級的 pinch
     本來就是開著的。） */
  touch-action: pan-y pinch-zoom;
}

.ui-lightbox:focus {
  outline: none;
}

.ui-lightbox__bar {
  position: absolute;
  top: max(8px, var(--safe-area-top));
  left: max(12px, var(--safe-area-left));
  right: max(12px, var(--safe-area-right));
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  /* 這條 bar 橫跨整個頂端。不讓它吃掉點擊，否則頂端一整條變成「點了沒反應」
     的死區——backdrop 關窗靠的是點到 overlay 本身。 */
  pointer-events: none;
}

.ui-lightbox__counter,
.ui-lightbox__tools {
  pointer-events: auto;
}

.ui-lightbox__counter {
  font-size: var(--font-sm);
  color: rgba(255, 255, 255, 0.82);
  font-variant-numeric: tabular-nums;
  padding: 4px 10px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.45);
}

.ui-lightbox__counter-spacer {
  display: block;
}

.ui-lightbox__tools {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

/* 局部小型控制項，不是通用按鈕：它們浮在圖片上，需要自己的對比底色。
   刻意不重貼 .btn / .btn-sm 之類的名字（那是 UiButton 的單一來源）。 */
.ui-lightbox__tool {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid rgba(255, 255, 255, 0.22);
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.45);
  color: rgba(255, 255, 255, 0.9);
  font-size: var(--font-sm);
  line-height: 1;
  text-decoration: none;
  cursor: pointer;
}

.ui-lightbox__tool:hover {
  background: rgba(0, 0, 0, 0.7);
  border-color: rgba(var(--color-primary-rgb), 0.6);
  color: #fff;
}

.ui-lightbox__tool--icon {
  width: 32px;
  padding: 0;
  font-size: 15px;
}

.ui-lightbox__figure {
  margin: 0;
  min-width: 0;
  max-width: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
}

/* contain，不是 cover：本產品同時有 1024x1536 直式立繪與 1536x1024 橫式
   場景圖，cover 會把其中一種裁掉。 */
.ui-lightbox__image {
  display: block;
  max-width: 100%;
  max-height: 82dvh;
  width: auto;
  height: auto;
  object-fit: contain;
  border-radius: 8px;
}

.ui-lightbox__caption {
  margin: 0;
  max-width: min(720px, 92vw);
  text-align: center;
  font-size: var(--font-sm);
  line-height: 1.6;
  color: rgba(255, 255, 255, 0.82);
  overflow-wrap: anywhere;
  user-select: text;
}

.ui-lightbox__empty {
  margin: 0;
  color: rgba(255, 255, 255, 0.7);
  font-size: var(--font-md);
}

/* 左右鍵，位置與尺寸沿用舞台的 .stage-nav，讓兩處是同一種東西。 */
.ui-lightbox__nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 40px;
  height: 56px;
  border: none;
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.4);
  color: #fff;
  font-size: 18px;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  opacity: 0.75;
  transition: opacity 0.2s, background 0.2s;
}

.ui-lightbox__nav:hover {
  opacity: 1;
  background: rgba(0, 0, 0, 0.65);
}

.ui-lightbox__nav--prev {
  left: max(8px, var(--safe-area-left));
}

.ui-lightbox__nav--next {
  right: max(8px, var(--safe-area-right));
}

.ui-lightbox__status {
  position: absolute;
  bottom: max(16px, var(--safe-area-bottom));
  left: 50%;
  transform: translateX(-50%);
  margin: 0;
  padding: 6px 14px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.55);
  color: rgba(255, 255, 255, 0.85);
  font-size: var(--font-sm);
  pointer-events: none;
}

/* 失敗列要能被按到（重試鍵在裡面），所以把上面那條 pointer-events: none 收回。 */
.ui-lightbox__status--error {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  max-width: min(560px, 92vw);
  background: rgba(120, 24, 16, 0.78);
  border: 1px solid rgba(255, 138, 117, 0.5);
  color: #ffd9d2;
  pointer-events: auto;
}

.ui-lightbox__retry {
  flex: none;
  padding: 3px 10px;
  border: 1px solid rgba(255, 255, 255, 0.35);
  border-radius: 999px;
  background: transparent;
  color: inherit;
  font-size: var(--font-sm);
  line-height: 1.4;
  cursor: pointer;
}

.ui-lightbox__retry:hover {
  background: rgba(255, 255, 255, 0.16);
}

@media (max-width: 640px) {
  .ui-lightbox__image {
    max-height: 72dvh;
  }

  .ui-lightbox__nav {
    width: 34px;
    height: 48px;
  }
}
</style>
