/**
 * 放大看圖的導覽規則（LB 系列，計畫 `IMAGE_LIGHTBOX_PLAN.md` §4.4）。
 *
 * 這裡是「有決定的那一半」。前端測試 harness 是 SSR（`createSSRApp` +
 * `renderToString`，沒有 jsdom、沒有 `@vue/test-utils`）——鍵盤事件、pointer
 * 手勢、焦點、`history` 全都跑不起來，所以只要規則還留在 `UiLightbox.vue`
 * 裡，它就永遠沒有自動閘。分工照 `offscreenImageRelease.ts` 的既有成例：
 * 元件只負責把瀏覽器事件翻成這裡的輸入、再把輸出翻回畫面，判斷本身一律
 * 是這裡的純函式。
 *
 * 唯一比舞台（`ImageStage`）多出來的語意是**集合會非同步成長**：相簿帶
 * keyset 分頁，浮窗走到已載入的最後一張時要能觸發續載。這件事沒有辦法用
 * 「索引 + 總數」兩個數字表達，所以下面的判定一律吃 `hasMore`。
 */

/**
 * 判定「這是一次橫滑／下滑」的位移門檻，CSS px。
 *
 * 40 是抄來的，不是挑的：`ImageStage` 的 swipe 用同一個值，而浮窗跟舞台在
 * 玩家手裡是同一種東西（一張大圖、左右換頁）。兩處給不同的手感只會讓人以為
 * 其中一邊壞掉。
 */
export const LIGHTBOX_SWIPE_THRESHOLD_PX = 40

/**
 * 一張可放大的圖。
 *
 * `url` 恆為**原始 URL**，不是尺寸變體——`UiImage` 自己會從它推出 `?v=full`，
 * 而「開原圖」那顆按鈕要的正是這個沒有加工過的值。呼叫端把變體 URL 傳進來，
 * 會讓開原圖變成開一個較小的重編碼檔，是無聲的功能倒退。
 */
export interface LightboxItem {
  url: string
  /** 圖說，顯示在圖片下方。 */
  caption?: string | null
  /** 無障礙描述；沒有就退回 caption，再退回「圖片 N」。 */
  alt?: string | null
  /**
   * 載入完成前的佔位比例（如 `'3 / 2'`）。本產品同時有 1024x1536 直式立繪與
   * 1536x1024 橫式場景圖，`UiImage` 的 `full` 預設是直式的 `2 / 3`，橫式圖集
   * （LB6）要自己說清楚，否則載入前的佔位框是錯的形狀。
   */
  aspectRatio?: string | number | null
}

/** 導覽一步之後，呼叫端該做什麼。 */
export type LightboxStepOutcome =
  /** 換到 `index` 這張。 */
  | 'move'
  /** 已載入的最後一張，但集合還沒完——去要下一頁，索引先停在原地。 */
  | 'load-more'
  /** 沒有去處。空集合、單張、或開放集合的最前面再往前。 */
  | 'stay'

export interface LightboxStep {
  outcome: LightboxStepOutcome
  /** 這一步之後應該停留的索引。`load-more` / `stay` 時等於原地。 */
  index: number
}

export interface LightboxStepInput {
  index: number
  total: number
  /** 通常是 -1 / +1；其他值也接受，語意是「往這個方向走這麼多格」。 */
  delta: number
  /** 集合之外還有沒載入的項目（相簿分頁）。預設 false＝集合已封閉。 */
  hasMore?: boolean
}

/** 會把負數也繞回範圍內的取餘，語意沿用 `ImageStage.wrapIndex`。 */
export function wrapLightboxIndex(index: number, total: number): number {
  if (!Number.isFinite(index) || !Number.isFinite(total) || total <= 0) return 0
  const size = Math.trunc(total)
  if (size <= 0) return 0
  const whole = Math.trunc(index)
  return ((whole % size) + size) % size
}

function safeTotal(total: number): number {
  if (!Number.isFinite(total)) return 0
  return Math.max(0, Math.trunc(total))
}

/**
 * 走一步。
 *
 * 三條規則，順序就是優先序：
 *
 * 1. **集合是空的或只有一張**——沒有去處，停在原地。單張時呼叫端據此不畫
 *    左右鍵（見 `shouldShowLightboxNav`）。
 * 2. **`hasMore` 為真＝集合還沒封閉**，所以「循環」這個概念根本不成立：往前
 *    走到已載入的最後一張再往前，回報 `load-more`（去載下一頁），**不能繞回
 *    第一張**——那會讓玩家以為整本相簿只有這幾張。同理往回走到第 0 張就停住，
 *    繞到「目前載到的最後一張」同樣是在謊報集合的邊界。
 * 3. **集合封閉**（`hasMore` 為假）才輪到循環，語意與舞台一致：第 0 張往前是
 *    最後一張。
 *
 * 索引越界不丟例外：先用 `wrapLightboxIndex` 收進範圍再算。呼叫端的索引來自
 * 上一次 render，而集合隨時可能被上游換掉（切角色、重新整理相簿），越界是
 * 正常狀態不是錯誤狀態。
 */
export function stepLightboxIndex(input: LightboxStepInput): LightboxStep {
  const total = safeTotal(input.total)
  if (total <= 0) return { outcome: 'stay', index: 0 }

  const current = wrapLightboxIndex(input.index, total)
  const delta = Number.isFinite(input.delta) ? Math.trunc(input.delta) : 0
  if (delta === 0) return { outcome: 'stay', index: current }

  const hasMore = input.hasMore === true
  const target = current + delta

  if (!hasMore) {
    if (total === 1) return { outcome: 'stay', index: 0 }
    return { outcome: 'move', index: wrapLightboxIndex(target, total) }
  }

  if (target > total - 1) return { outcome: 'load-more', index: total - 1 }
  if (target < 0) return { outcome: 'stay', index: 0 }
  return { outcome: 'move', index: target }
}

export interface LightboxResumeInput {
  /** 觸發續載時停留的索引。 */
  index: number
  /** 觸發續載時的項目數。 */
  totalBefore: number
  /** 續載回來之後的項目數。 */
  totalAfter: number
}

/**
 * 續載回來之後，該不該把玩家往前推一格。
 *
 * 長了就繼續走——玩家按下的是「下一張」，這一步只是被 HTTP 往返推遲了。
 *
 * **沒長就停在原地**，這條比看起來重要：後端回了一頁空的（或 `has_more`
 * 本來就報錯了）時，若改成把 `stepLightboxIndex` 重跑一次，此時 `hasMore`
 * 已經變成 false，規則 3 會讓玩家被無聲地丟回第一張圖。停在原地至少是誠實的
 * ——畫面沒動，玩家會再按一次。
 */
export function resumeLightboxAfterLoad(input: LightboxResumeInput): LightboxStep {
  const before = safeTotal(input.totalBefore)
  const after = safeTotal(input.totalAfter)
  const current = wrapLightboxIndex(input.index, Math.max(after, 1))
  if (after <= 0) return { outcome: 'stay', index: 0 }
  if (after <= before) return { outcome: 'stay', index: Math.min(current, after - 1) }
  return { outcome: 'move', index: Math.min(current + 1, after - 1) }
}

export interface LightboxResumeOwedInput {
  /**
   * 觸發這次續載時，玩家停在哪一張。
   *
   * `null`＝沒有人在等這一步（例如呼叫端自己捲到底續載，或浮窗根本沒開）。
   */
  requestedFrom: number | null | undefined
  /** 續載回來的當下，玩家停在哪一張。 */
  current: number
}

/**
 * 續載回來的那一步，還欠不欠玩家——**位置這一半**。
 *
 * `resumeLightboxAfterLoad` 只問「清單有沒有長」，而那是**必要條件不是充分
 * 條件**：HTTP 往返期間浮窗仍然可以導覽——玩家按完「下一張」再往回翻幾張、
 * 停在中間某張等它回來，是完全正常的操作。此時把 +1 照套下去，畫面會在玩家
 * 沒有任何動作的情況下自己往前跳一張。集合愈大、網路愈慢，這個空窗愈長。
 *
 * 所以判準是「玩家還站在他按下去的那一張嗎」，不是「當時有幾張」。
 *
 * **這只是判準的一半。** 另一半在浮窗自己身上：只要它送出過一次換圖，等待
 * 記號當場作廢（見 `UiLightbox.setIndex`）——玩家自己走的那一步已經取代了被
 * 推遲的那一步，走開又走回來也一樣，畫面不該在他停手之後自己再動一次。這支
 * 函式擋的是**記號攔不到的那條路**：呼叫端直接改 `index` prop（相簿排序、
 * 舞台圖搬位置都會這樣做），浮窗沒有送出過任何 `update:index`，記號還在，
 * 但玩家已經不在原處了。
 *
 * 非有限數字一律當成不欠：算不出「站在哪」的時候，不動畫面是唯一不會嚇到人的
 * 選擇（多按一次下一張的代價，遠小於圖自己跳掉）。
 */
export function isLightboxResumeOwed(input: LightboxResumeOwedInput): boolean {
  const from = input.requestedFrom
  if (from === null || from === undefined) return false
  if (!Number.isFinite(from) || !Number.isFinite(input.current)) return false
  return Math.trunc(from) === Math.trunc(input.current)
}

/**
 * 一次「還在等的續載」。
 *
 * 兩個欄位缺一不可，而且問的是兩件不同的事：`total` 用來判斷**清單有沒有真的
 * 長**（同一次續載可能回空頁），`index` 用來判斷**這一步還欠不欠玩家**。
 * 只記 total 是這個機制最初的寫法，它會在玩家等待期間往回翻之後照樣把他推
 * 一格；只記 index 則分不出「回來了但沒長」與「還沒回來」。
 */
export interface LightboxPendingLoad {
  /** 觸發續載時的項目數。 */
  total: number
  /** 觸發續載時玩家停在哪一張。 */
  index: number
}

/**
 * 清單長大之後，那次還在等的續載該收成什麼結果。
 *
 * 三態刻意分開「記號留不留」與「畫面動不動」——兩者不是同一件事：
 *
 * - `wait`：清單還沒長（回了空頁、或這次變長根本不是續載造成的）。**記號留著**，
 *   真正那一頁回來時才輪到它。
 * - `stay`：記號收掉，畫面不動。
 * - `move`：記號收掉，換到 `index`。
 */
export type LightboxResumeAction =
  | { outcome: 'wait' }
  | { outcome: 'stay' }
  | { outcome: 'move'; index: number }

export interface LightboxResumeActionInput {
  /** 還在等的那次續載；`null`＝沒有人在等。 */
  pending: LightboxPendingLoad | null | undefined
  /** 現在玩家停在哪一張。 */
  current: number
  /** 清單變動之後的項目數。 */
  totalAfter: number
}

/**
 * 續載回來的整個判定，一支函式。
 *
 * 收攏在這裡而不是散在浮窗的 watcher 裡，是因為這是整個浮窗**唯一會自己動
 * 畫面**的地方：判錯的症狀是「玩家沒碰任何東西，圖自己跳掉一張」，而那在
 * SSR harness 裡一行都跑不到。把它做成純函式，那個劇本才有閘。
 *
 * 沒有人在等時回 `stay`（不是 `wait`）：沒有記號可留，也沒有畫面該動；呼叫端
 * 照著收記號是無操作。
 */
export function lightboxResumeAction(
  input: LightboxResumeActionInput,
): LightboxResumeAction {
  const pending = input.pending
  if (!pending) return { outcome: 'stay' }

  const before = safeTotal(pending.total)
  const after = safeTotal(input.totalAfter)
  if (after <= before) return { outcome: 'wait' }

  if (!isLightboxResumeOwed({ requestedFrom: pending.index, current: input.current })) {
    return { outcome: 'stay' }
  }

  const step = resumeLightboxAfterLoad({
    index: input.current,
    totalBefore: before,
    totalAfter: after,
  })
  return step.outcome === 'move'
    ? { outcome: 'move', index: step.index }
    : { outcome: 'stay' }
}

// ------------------------------------------------------------- 續載的請求與狀態

export interface LightboxLoadMoreRequestInput {
  /** 集合之外還有沒載入的項目。 */
  hasMore?: boolean
  /** 呼叫端回報的「續載進行中」。 */
  loadingMore?: boolean
}

/**
 * 走到已載入的末端時，該不該真的向呼叫端要下一頁。
 *
 * 去重的依據**只能**是呼叫端回報的 `loadingMore`，不能是浮窗自己記的「我已經
 * 要過了」——後者在請求失敗時沒有人會把它翻回來，玩家從此再也送不出第二次
 * 請求，畫面就無聲卡在最後一張（而這正是 LB2 票面點名要避免的失敗模式）。
 * 失敗會讓呼叫端的 `loadingMore` 回到 false，於是下一次按下去自然就是重試。
 *
 * `hasMore` 為假時不送：集合已封閉，再要一頁是白打一次 API。
 */
export function shouldSendLightboxLoadMore(
  input: LightboxLoadMoreRequestInput,
): boolean {
  if (input.hasMore !== true) return false
  return input.loadingMore !== true
}

/** 續載狀態列的三態。 */
export type LightboxLoadMoreState = 'hidden' | 'loading' | 'error'

export interface LightboxLoadMoreStateInput {
  loadingMore?: boolean
  /** 呼叫端回報的失敗訊息；空字串／全空白視同沒有失敗。 */
  error?: string | null
}

/**
 * 續載狀態列要顯示什麼。
 *
 * **載入中壓過失敗**：重試已經在飛，還把上一次的失敗留在畫面上，玩家會以為
 * 重試也失敗了。失敗訊息只有在「沒有請求在飛」時才是當下的事實。
 *
 * 全空白的訊息視同沒有失敗——呼叫端傳 `''` 幾乎都是「我把錯誤清掉了」的意思，
 * 照著它渲染一條沒有字的失敗列只會讓人以為又壞了一次。
 */
export function lightboxLoadMoreState(
  input: LightboxLoadMoreStateInput,
): LightboxLoadMoreState {
  if (input.loadingMore === true) return 'loading'
  const message = typeof input.error === 'string' ? input.error.trim() : ''
  return message === '' ? 'hidden' : 'error'
}

// ------------------------------------------------------------- 鍵盤：誰吃掉哪些鍵

/**
 * 浮窗對一顆鍵做什麼。
 *
 * `swallow` 是這裡唯一不直覺的一項：浮窗**沒有**左右鍵可走（單張卡）時，
 * ←／→ 仍然要被吃掉。理由是 `aria-modal="true"` 的 inert 語意——浮窗開著時
 * 底下那一層在鍵盤上等於不存在。放行去讓底下的 `CharacterCardGalleryModal`
 * 切角色卡，會把玩家正在看的那張大圖在按鍵瞬間換掉（modal 用 `:key` 重掛
 * 卡面元件，浮窗連同它一起被拆），比「按了沒反應」糟得多。
 */
export type LightboxKeyAction =
  /** Esc：關窗。 */
  | 'close'
  /** ←：上一張。 */
  | 'prev'
  /** →：下一張。 */
  | 'next'
  /** Tab：焦點 trap 要處理，但**不得**阻斷傳播（見 `lightboxKeyStopsPropagation`）。 */
  | 'trap-tab'
  /** 浮窗不走這顆鍵，但也不讓宿主拿去用。 */
  | 'swallow'
  /** 與浮窗無關，原樣放行。 */
  | 'ignore'

export interface LightboxKeyInput {
  key: string
  /** 浮窗開著沒有。關著時一律 `ignore`——監聽可能還掛著。 */
  visible?: boolean
  /** 有沒有左右導覽（`shouldShowLightboxNav`）。 */
  showNav?: boolean
}

/**
 * 把一顆鍵翻成浮窗的動作。
 *
 * 這支函式存在的理由不是「元件太長」，是**責任歸屬**：在它之前，「浮窗開著
 * 時 Esc／←→ 屬於浮窗」這件事是由每一個宿主自己記得讓位來達成的
 * （`CharacterImagesPanel` 一條守衛、`CharacterCardFace` → `CharacterCardGalleryModal`
 * 一條 emit 鏈）。漏掉一個宿主不會有任何測試變紅，而漏掉的代價是玩家按 Esc
 * 想關大圖、結果整個 LumeGram 動態牆一起關掉，捲動位置與已載入分頁全部歸零。
 *
 * 責任收回浮窗之後，宿主不需要知道浮窗存在：浮窗以 **capture 階段**掛在
 * `window` 上（事件路徑的第一站），對它自己會處理的鍵呼叫 `stopPropagation()`，
 * 事件根本到不了宿主那些 bubble 階段的監聽。
 */
export function classifyLightboxKey(input: LightboxKeyInput): LightboxKeyAction {
  if (input.visible !== true) return 'ignore'
  switch (input.key) {
    case 'Escape':
      return 'close'
    case 'Tab':
      return 'trap-tab'
    case 'ArrowLeft':
      return input.showNav === true ? 'prev' : 'swallow'
    case 'ArrowRight':
      return input.showNav === true ? 'next' : 'swallow'
    default:
      return 'ignore'
  }
}

/**
 * 這個動作要不要阻斷傳播。
 *
 * **只對真的處理掉的鍵停止傳播。** 兩個例外各有理由：
 *
 * - `trap-tab`：焦點 trap 靠 `preventDefault()` + `focus()` 就完成了，不需要
 *   獨佔這顆鍵；吞掉它會讓宿主（以及未來任何全域快捷鍵）在浮窗開著時失去
 *   Tab，而那不是浮窗該做的決定。
 * - `ignore`：與浮窗無關的鍵一律原樣放行。開著一個看圖浮窗不該讓玩家的其他
 *   快捷鍵全部失效。
 */
export function lightboxKeyStopsPropagation(action: LightboxKeyAction): boolean {
  return action === 'close' || action === 'prev' || action === 'next' || action === 'swallow'
}

/** 一次 pointer 拖曳的語意。 */
export type LightboxSwipeAction = 'prev' | 'next' | 'close' | 'ignore'

export interface LightboxSwipeInput {
  /** 終點減起點的水平位移，CSS px。 */
  dx: number
  /** 終點減起點的垂直位移，CSS px（往下為正）。 */
  dy: number
  thresholdPx?: number
}

/**
 * 把一次拖曳翻成動作。
 *
 * 橫向先判：條件與 `ImageStage` 逐字相同——位移要過門檻，且 `|dy|` 不得大於
 * `|dx|`。往右拖是「上一張」（內容跟著手指往右走，前一張從左邊進來）。
 *
 * 往下拖過門檻且垂直意圖較強＝關閉，這是手機上唯一不用瞄準關閉鍵的關法。
 * **往上拖刻意不給語意**：上滑在瀏覽器裡是捲動，把它也接成關閉，會讓想看
 * 長圖說的人一滑就把窗關掉。
 *
 * 非有限數字一律 `ignore`——那代表測到位移的東西自己就不知道手指在哪，此時
 * 什麼都不做是唯一安全的選擇（誤關的失敗模式是玩家看到一半的圖不見了）。
 */
export function classifyLightboxSwipe(input: LightboxSwipeInput): LightboxSwipeAction {
  const { dx, dy } = input
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) return 'ignore'
  const threshold =
    Number.isFinite(input.thresholdPx) && (input.thresholdPx as number) > 0
      ? (input.thresholdPx as number)
      : LIGHTBOX_SWIPE_THRESHOLD_PX

  const absX = Math.abs(dx)
  const absY = Math.abs(dy)

  if (absX >= threshold && absY <= absX) {
    return dx > 0 ? 'prev' : 'next'
  }
  if (dy >= threshold && absY > absX) {
    return 'close'
  }
  return 'ignore'
}

export interface LightboxPointerStartInput {
  /** `PointerEvent.pointerType`。 */
  pointerType?: string
  /** `PointerEvent.button`；滑鼠只認左鍵。 */
  button?: number
  /** `PointerEvent.isPrimary`；多指觸控時只有第一指為真。 */
  isPrimary?: boolean
  /** 含這一次在內、目前按著的 pointer 數。 */
  activePointerCount?: number
  /** 起點落在圖說（`.ui-lightbox__caption`）裡。 */
  withinCaption?: boolean
  /**
   * `visualViewport.scale`。>1＝畫面已經被 pinch 放大，此時的單指拖曳是**平移**
   * ——玩家在放大的圖上找細節看，不是要換圖。省略時當作 1（沒放大）。
   */
  viewportScale?: number
}

/** `visualViewport.scale` 幾乎不會剛好是 1，留一點餘裕再判定「放大了」。 */
const VIEWPORT_ZOOM_EPSILON = 0.01

/**
 * 這一次 `pointerdown` 該不該開始記錄手勢。
 *
 * 三種要放棄的情形：
 *
 * 1. **多指／非 primary。** 雙指縮放是瀏覽器原生行為（浮窗的
 *    `touch-action: pan-y pinch-zoom` 把它交還給瀏覽器），縮放進行中的兩指
 *    位移不是換圖手勢。
 * 2. **畫面已經被放大（`viewportScale > 1`）。** 這是第 1 條的下半場：手指離開
 *    之後畫面仍然停在放大狀態，接下來的**單指**拖曳是在平移看細節，`clientX`
 *    的位移隨便就過 40px。判成換圖的話，玩家正在放大檢視的那張圖會當場消失
 *    ——而「看清楚原圖」正是浮窗存在的理由。
 * 3. **滑鼠的非左鍵。** 右鍵是選單，中鍵是貼上／捲動。
 * 4. **起點在圖說裡。** 圖說是刻意開放選取的（`user-select: text`），玩家橫向
 *    拖過去是在選字；判成換圖會讓圖片當場換掉、選取內容一起消失。
 */
export function shouldTrackLightboxPointer(input: LightboxPointerStartInput): boolean {
  if (input.isPrimary === false) return false
  const active = input.activePointerCount
  if (typeof active === 'number' && Number.isFinite(active) && active > 1) return false
  const scale = input.viewportScale
  if (typeof scale === 'number' && Number.isFinite(scale) && scale > 1 + VIEWPORT_ZOOM_EPSILON) {
    return false
  }
  if (input.pointerType === 'mouse' && (input.button ?? 0) !== 0) return false
  if (input.withinCaption === true) return false
  return true
}

// ------------------------------------------------------------- 鄰居預取的節流

/**
 * 鄰居預取的靜止窗，毫秒。
 *
 * 按住 → 的自動重複約 30 次／秒。沒有節流時，三秒的長按會送出約 90–180 筆
 * `full` 變體請求（一筆 218 KB，合計約 20–39 MB），而且全部無法中止——玩家
 * 真正停下來的那一張排在佇列最後面，行動網路下會盯著佔位框好幾秒。**比不
 * 預取還慢**，而手機資料用量是實付的。
 *
 * 200 ms 的語意正好是「玩家停在這張了」：連按時一筆都不送，停下來才預熱。
 */
export const LIGHTBOX_PREFETCH_DEBOUNCE_MS = 200

export interface LightboxPrefetchSettleInput {
  /** 最後一次索引／集合變動的時間戳（單調時鐘，如 `performance.now()`）。 */
  changedAt: number
  /** 現在。 */
  now: number
  debounceMs?: number
}

/**
 * 靜止窗過了沒有——過了才真的去預熱鄰居。
 *
 * 排定的計時器有可能在「玩家又按了一下」之後才醒來（重排計時器與事件之間
 * 沒有原子性），所以醒來時還要再問一次現在的事實，而不是相信自己被排定的
 * 那一刻。時間非有限值時回 `false`：量不到時間就不要送出流量。
 */
export function lightboxPrefetchSettled(input: LightboxPrefetchSettleInput): boolean {
  const { changedAt, now } = input
  if (!Number.isFinite(changedAt) || !Number.isFinite(now)) return false
  const window =
    Number.isFinite(input.debounceMs) && (input.debounceMs as number) >= 0
      ? (input.debounceMs as number)
      : LIGHTBOX_PREFETCH_DEBOUNCE_MS
  return now - changedAt >= window
}

/** 取索引上的項目；越界／空集合回 `null`，不丟例外。 */
export function lightboxItemAt(
  items: readonly LightboxItem[] | null | undefined,
  index: number,
): LightboxItem | null {
  if (!items || items.length === 0) return null
  return items[wrapLightboxIndex(index, items.length)] ?? null
}

/**
 * 左右鍵要不要出現。
 *
 * 單張時不畫——一顆按下去什麼都不會發生的按鈕比沒有按鈕更糟。但「單張 ＋
 * 還有下一頁」要畫：那顆「下一張」按下去會去載第二頁，是真的有去處。
 */
export function shouldShowLightboxNav(total: number, hasMore = false): boolean {
  const size = safeTotal(total)
  if (size <= 0) return false
  return size > 1 || hasMore
}

/** `3 / 12` 這種位置指示要不要出現：只有一張又載完了就沒有意義。 */
export function shouldShowLightboxCounter(total: number, hasMore = false): boolean {
  return shouldShowLightboxNav(total, hasMore)
}

/**
 * 該預熱 HTTP 快取的鄰居 URL（**只給 `new Image()`，不進 DOM**）。
 *
 * 紅線的另一面：一張 full 圖的解碼點陣是 `1024x1536x4` ≈ 6 MB，只要 DOM 裡
 * 的元素還握著 URL 就一直付，所以浮窗只掛 active 一張。要讓左右鍵不等待，
 * 能做的是先把位元組放進 HTTP 快取（送達路由帶 `immutable` + ETag，回訪
 * ~26 ms），而不是先把圖掛起來。
 *
 * 只取 ±1：每一個自動動作都是 ±1（左右鍵、箭頭鈕、swipe），再多預取就是在
 * 替玩家不會走到的地方付流量。集合未封閉時不繞回頭尾——那兩張根本不是下一步
 * 會走到的圖。
 */
export function lightboxNeighbourUrls(
  items: readonly LightboxItem[] | null | undefined,
  index: number,
  hasMore = false,
): string[] {
  if (!items || items.length === 0) return []
  const total = items.length
  const current = wrapLightboxIndex(index, total)
  const wanted: number[] = []
  for (const delta of [1, -1] as const) {
    const step = stepLightboxIndex({ index: current, total, delta, hasMore })
    if (step.outcome !== 'move') continue
    wanted.push(step.index)
  }
  const urls: string[] = []
  for (const at of wanted) {
    const url = items[at]?.url
    if (!url) continue
    if (at === current) continue
    if (urls.includes(url)) continue
    urls.push(url)
  }
  return urls
}
