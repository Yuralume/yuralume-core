/**
 * 放大看圖的導覽規則與浮窗骨架（LB1）。
 *
 * 這個 harness 是 SSR（`createSSRApp` + `renderToString`，沒有 jsdom、沒有
 * `@vue/test-utils`），所以能看到的只有**第一次渲染出來的 markup**。以下行為
 * **完全沒有自動閘**，落地時要手動驗收，並記進殘留：
 *
 * - ←／→ 鍵切換、Esc 關閉（規則本身有閘，見 `classifyLightboxKey`；真的把
 *   事件攔在宿主之前的那一段只有原始碼掃描）
 * - pointer swipe（規則本身有閘，見下面的 `classifyLightboxSwipe`；把
 *   `pointerdown`/`pointerup` 的座標餵給它的那一段沒有）
 * - 焦點 trap（Tab 不得跑到背景）與關閉後把焦點還給觸發者
 * - 手機返回鍵：`history.pushState` / `popstate` / 正常關閉收掉 entry
 * - 背景捲動鎖與巢狀開啟時的計數還原
 * - 鄰居預取（`new Image()`，不進 DOM）與它的靜止窗計時器
 * - **雙指縮放**：`touch-action` 的值有閘（下面掃 CSS），瀏覽器真的縮不縮放
 *   只能上手機看
 *
 * 這正是為什麼有決定的部分全被抽進 `@/utils/lightbox`：那一半才有閘。
 * 坦白風格沿用 `chatBubbleImage.test.ts` 的檔頭。
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import {
  LIGHTBOX_PREFETCH_DEBOUNCE_MS,
  LIGHTBOX_SWIPE_THRESHOLD_PX,
  classifyLightboxKey,
  classifyLightboxSwipe,
  lightboxItemAt,
  lightboxKeyStopsPropagation,
  lightboxLoadMoreState,
  lightboxNeighbourUrls,
  lightboxPrefetchSettled,
  lightboxResumeAction,
  isLightboxResumeOwed,
  resumeLightboxAfterLoad,
  shouldSendLightboxLoadMore,
  shouldShowLightboxCounter,
  shouldShowLightboxNav,
  shouldTrackLightboxPointer,
  stepLightboxIndex,
  wrapLightboxIndex,
  type LightboxItem,
} from '@/utils/lightbox'

function source(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8')
}

const LIGHTBOX_SOURCE = source('../src/components/ui/UiLightbox.vue')

const UiLightbox = (await import('@/components/ui/UiLightbox.vue')).default

const i18n = createI18n({
  legacy: false,
  locale: 'zh-TW',
  messages: { 'zh-TW': zhTW },
})

function items(...urls: string[]): LightboxItem[] {
  return urls.map((url) => ({ url }))
}

async function render(props: Record<string, unknown>): Promise<string> {
  const app = createSSRApp(UiLightbox, { visible: true, ...props })
  app.use(i18n)
  return renderToString(app)
}

// ------------------------------------------------------------- 純函式：循環

describe('走到集合末端（集合已封閉）', () => {
  it('往前走出最後一張會回到第一張', () => {
    expect(stepLightboxIndex({ index: 2, total: 3, delta: 1 })).toEqual({
      outcome: 'move',
      index: 0,
    })
  })

  it('從第一張往回走會到最後一張', () => {
    expect(stepLightboxIndex({ index: 0, total: 3, delta: -1 })).toEqual({
      outcome: 'move',
      index: 2,
    })
  })

  it('wrapLightboxIndex 把負數也繞回範圍內', () => {
    expect(wrapLightboxIndex(-1, 4)).toBe(3)
    expect(wrapLightboxIndex(9, 4)).toBe(1)
    expect(wrapLightboxIndex(0, 0)).toBe(0)
    expect(wrapLightboxIndex(Number.NaN, 4)).toBe(0)
  })
})

describe('集合還沒載完（hasMore）', () => {
  it('走到已載入的最後一張再往前，是去續載而不是循環', () => {
    // 循環回第一張會讓玩家以為整本相簿只有這幾張。
    expect(
      stepLightboxIndex({ index: 2, total: 3, delta: 1, hasMore: true }),
    ).toEqual({ outcome: 'load-more', index: 2 })
  })

  it('第一張再往回是停住，不繞到「目前載到的最後一張」', () => {
    expect(
      stepLightboxIndex({ index: 0, total: 3, delta: -1, hasMore: true }),
    ).toEqual({ outcome: 'stay', index: 0 })
  })

  it('集合中間照常前進', () => {
    expect(
      stepLightboxIndex({ index: 0, total: 3, delta: 1, hasMore: true }),
    ).toEqual({ outcome: 'move', index: 1 })
  })

  it('只有一張但還有下一頁時，往前仍然要求續載', () => {
    expect(
      stepLightboxIndex({ index: 0, total: 1, delta: 1, hasMore: true }),
    ).toEqual({ outcome: 'load-more', index: 0 })
  })

  it('載完之後（hasMore 轉假）才輪到循環', () => {
    expect(stepLightboxIndex({ index: 2, total: 3, delta: 1 }).outcome).toBe('move')
  })
})

describe('續載回來之後', () => {
  it('清單長了就把玩家往前推一格', () => {
    expect(
      resumeLightboxAfterLoad({ index: 2, totalBefore: 3, totalAfter: 6 }),
    ).toEqual({ outcome: 'move', index: 3 })
  })

  it('清單沒長就停在原地，不會被無聲丟回第一張', () => {
    expect(
      resumeLightboxAfterLoad({ index: 2, totalBefore: 3, totalAfter: 3 }),
    ).toEqual({ outcome: 'stay', index: 2 })
  })

  it('清單反而變短時，索引夾回範圍內', () => {
    expect(
      resumeLightboxAfterLoad({ index: 5, totalBefore: 6, totalAfter: 2 }),
    ).toEqual({ outcome: 'stay', index: 1 })
  })

  it('清單清空時是安全的停留', () => {
    expect(
      resumeLightboxAfterLoad({ index: 3, totalBefore: 4, totalAfter: 0 }),
    ).toEqual({ outcome: 'stay', index: 0 })
  })
})

describe('那一步還欠不欠玩家：位置這一半', () => {
  it('還站在按下去的那一張＝欠著，照補', () => {
    expect(isLightboxResumeOwed({ requestedFrom: 23, current: 23 })).toBe(true)
  })

  it('位置已經不同＝不欠（呼叫端直接改 index prop 也走這條）', () => {
    // 記號的作廢主要靠浮窗自己的 `setIndex`（玩家一走就丟），這支擋的是記號
    // 攔不到的那條路：相簿排序／舞台圖搬位置會由呼叫端直接改 `index`。
    expect(isLightboxResumeOwed({ requestedFrom: 23, current: 10 })).toBe(false)
  })

  it('沒有人在等（呼叫端自己捲到底續載）就不補', () => {
    expect(isLightboxResumeOwed({ requestedFrom: null, current: 10 })).toBe(false)
    expect(isLightboxResumeOwed({ requestedFrom: undefined, current: 0 })).toBe(false)
  })

  it('算不出位置時不動畫面——多按一次遠比圖自己跳掉便宜', () => {
    expect(isLightboxResumeOwed({ requestedFrom: Number.NaN, current: 0 })).toBe(false)
    expect(isLightboxResumeOwed({ requestedFrom: 0, current: Number.NaN })).toBe(false)
  })
})

describe('續載回來的整個判定（lightboxResumeAction）', () => {
  it('玩家停在原地等，那一頁回來就補走一格', () => {
    expect(lightboxResumeAction({
      pending: { total: 24, index: 23 },
      current: 23,
      totalAfter: 48,
    })).toEqual({ outcome: 'move', index: 24 })
  })

  it('等待期間往回翻走了，回來的那一頁不得推他', () => {
    // 票面那個劇本：相簿 24 張，玩家在第 24 張（index 23）按「下一張」，HTTP
    // 往返期間往回翻到第 11 張（index 10）在看。第二頁回來 items 24→48，未修
    // 時索引自己 10→11——玩家沒有任何操作，畫面自己跳掉一張。
    expect(lightboxResumeAction({
      pending: { total: 24, index: 23 },
      current: 10,
      totalAfter: 48,
    })).toEqual({ outcome: 'stay' })
  })

  it('沒有人在等就什麼都不做（呼叫端自己捲到底續載）', () => {
    expect(lightboxResumeAction({ pending: null, current: 5, totalAfter: 48 }))
      .toEqual({ outcome: 'stay' })
    expect(lightboxResumeAction({ pending: undefined, current: 5, totalAfter: 48 }))
      .toEqual({ outcome: 'stay' })
  })

  it('清單還沒長就繼續等——記號不能在這裡被收掉', () => {
    // 回了空的一頁、或這次變動根本不是續載造成的（刪掉一張）。收掉記號等於
    // 讓真正那一頁回來時沒有人記得玩家在等。
    expect(lightboxResumeAction({
      pending: { total: 24, index: 23 },
      current: 23,
      totalAfter: 24,
    })).toEqual({ outcome: 'wait' })
    expect(lightboxResumeAction({
      pending: { total: 24, index: 23 },
      current: 23,
      totalAfter: 20,
    })).toEqual({ outcome: 'wait' })
  })

  it('長了但只長到剛好，索引夾在範圍內', () => {
    expect(lightboxResumeAction({
      pending: { total: 3, index: 2 },
      current: 2,
      totalAfter: 4,
    })).toEqual({ outcome: 'move', index: 3 })
  })

  it('算不出位置就不動畫面，記號照收', () => {
    expect(lightboxResumeAction({
      pending: { total: 24, index: Number.NaN },
      current: 23,
      totalAfter: 48,
    })).toEqual({ outcome: 'stay' })
  })
})

// ------------------------------------------- 純函式：續載的請求與狀態（LB2）

describe('要不要真的送出續載請求', () => {
  it('走到末端且沒有請求在飛就送', () => {
    expect(shouldSendLightboxLoadMore({ hasMore: true, loadingMore: false })).toBe(true)
    expect(shouldSendLightboxLoadMore({ hasMore: true })).toBe(true)
  })

  it('已經有一次在飛就不送——這就是「載入中連按」的擋板', () => {
    expect(shouldSendLightboxLoadMore({ hasMore: true, loadingMore: true })).toBe(false)
  })

  it('失敗之後（loadingMore 回到 false）必須允許再送一次', () => {
    // 這條是整個續載最容易無聲卡住的地方：若去重改成看浮窗自己記的「我要過
    // 了」，失敗時沒有人翻回那個記號，玩家從此再也送不出第二次請求。
    expect(shouldSendLightboxLoadMore({ hasMore: true, loadingMore: false })).toBe(true)
  })

  it('集合已封閉就不送，免得白打一次 API', () => {
    expect(shouldSendLightboxLoadMore({ hasMore: false, loadingMore: false })).toBe(false)
    expect(shouldSendLightboxLoadMore({})).toBe(false)
  })
})

describe('續載狀態列', () => {
  it('沒事就不顯示', () => {
    expect(lightboxLoadMoreState({})).toBe('hidden')
    expect(lightboxLoadMoreState({ loadingMore: false, error: null })).toBe('hidden')
  })

  it('載入中顯示載入', () => {
    expect(lightboxLoadMoreState({ loadingMore: true })).toBe('loading')
  })

  it('失敗顯示失敗——不能什麼都不畫，那就是無聲卡住', () => {
    expect(lightboxLoadMoreState({ error: '載入相簿失敗' })).toBe('error')
  })

  it('重試在飛的時候，載入壓過上一次的失敗', () => {
    // 否則玩家按了重試卻還看著紅字，會以為重試也失敗了。
    expect(lightboxLoadMoreState({ loadingMore: true, error: '載入相簿失敗' })).toBe('loading')
  })

  it('空字串／全空白的訊息視同沒有失敗', () => {
    expect(lightboxLoadMoreState({ error: '' })).toBe('hidden')
    expect(lightboxLoadMoreState({ error: '   ' })).toBe('hidden')
  })
})

// ------------------------------------------------------------- 純函式：邊界

describe('邊界（不得丟例外）', () => {
  it('空集合哪裡都去不了', () => {
    expect(stepLightboxIndex({ index: 0, total: 0, delta: 1 })).toEqual({
      outcome: 'stay',
      index: 0,
    })
    expect(stepLightboxIndex({ index: 4, total: 0, delta: -1 })).toEqual({
      outcome: 'stay',
      index: 0,
    })
  })

  it('只有一張且已載完時沒有去處', () => {
    expect(stepLightboxIndex({ index: 0, total: 1, delta: 1 })).toEqual({
      outcome: 'stay',
      index: 0,
    })
    expect(stepLightboxIndex({ index: 0, total: 1, delta: -1 })).toEqual({
      outcome: 'stay',
      index: 0,
    })
  })

  it('索引越界先被收進範圍再走', () => {
    // 索引來自上一次 render，集合隨時可能被上游換掉——越界是正常狀態。
    expect(stepLightboxIndex({ index: 99, total: 3, delta: 1 })).toEqual({
      outcome: 'move',
      index: 1,
    })
    expect(stepLightboxIndex({ index: -5, total: 3, delta: 1 })).toEqual({
      outcome: 'move',
      index: 2,
    })
  })

  it('非數字輸入不會炸，也不會亂跳', () => {
    expect(stepLightboxIndex({ index: Number.NaN, total: 3, delta: 1 })).toEqual({
      outcome: 'move',
      index: 1,
    })
    expect(stepLightboxIndex({ index: 1, total: 3, delta: Number.NaN })).toEqual({
      outcome: 'stay',
      index: 1,
    })
    expect(
      stepLightboxIndex({ index: 1, total: Number.NaN, delta: 1 }),
    ).toEqual({ outcome: 'stay', index: 0 })
  })

  it('lightboxItemAt 對空集合與越界回 null / 繞回', () => {
    expect(lightboxItemAt([], 0)).toBeNull()
    expect(lightboxItemAt(null, 2)).toBeNull()
    expect(lightboxItemAt(items('a', 'b'), 3)?.url).toBe('b')
  })

  it('單張不顯示左右鍵，除非還有下一頁', () => {
    expect(shouldShowLightboxNav(1)).toBe(false)
    expect(shouldShowLightboxNav(1, true)).toBe(true)
    expect(shouldShowLightboxNav(0, true)).toBe(false)
    expect(shouldShowLightboxNav(2)).toBe(true)
    expect(shouldShowLightboxCounter(1)).toBe(false)
  })
})

// ------------------------------------------------------------- 純函式：手勢

describe('swipe 判定', () => {
  const T = LIGHTBOX_SWIPE_THRESHOLD_PX

  it('門檻與舞台一致', () => {
    expect(T).toBe(40)
  })

  it('往右滑是上一張、往左滑是下一張', () => {
    expect(classifyLightboxSwipe({ dx: T + 5, dy: 0 })).toBe('prev')
    expect(classifyLightboxSwipe({ dx: -(T + 5), dy: 0 })).toBe('next')
  })

  it('位移不到門檻就忽略（那是點擊不是滑動）', () => {
    expect(classifyLightboxSwipe({ dx: T - 1, dy: 0 })).toBe('ignore')
    expect(classifyLightboxSwipe({ dx: 0, dy: 0 })).toBe('ignore')
  })

  it('近垂直的橫向拖曳被忽略，頁面捲動才保得住', () => {
    expect(classifyLightboxSwipe({ dx: T + 5, dy: T + 60 })).toBe('close')
    expect(classifyLightboxSwipe({ dx: T + 5, dy: -(T + 60) })).toBe('ignore')
  })

  it('下滑過門檻是關閉', () => {
    expect(classifyLightboxSwipe({ dx: 0, dy: T + 5 })).toBe('close')
    expect(classifyLightboxSwipe({ dx: 10, dy: T + 5 })).toBe('close')
  })

  it('上滑刻意沒有語意——那是捲動，不是關窗', () => {
    expect(classifyLightboxSwipe({ dx: 0, dy: -(T + 5) })).toBe('ignore')
  })

  it('測不到位移時什麼都不做', () => {
    expect(classifyLightboxSwipe({ dx: Number.NaN, dy: 10 })).toBe('ignore')
    expect(classifyLightboxSwipe({ dx: 10, dy: Number.POSITIVE_INFINITY })).toBe('ignore')
  })

  it('門檻可以被呼叫端調整', () => {
    expect(classifyLightboxSwipe({ dx: 20, dy: 0, thresholdPx: 10 })).toBe('prev')
    expect(classifyLightboxSwipe({ dx: 20, dy: 0, thresholdPx: 0 })).toBe('ignore')
  })
})

describe('鄰居預取清單', () => {
  it('只取 ±1，且不含目前這張', () => {
    expect(lightboxNeighbourUrls(items('a', 'b', 'c', 'd'), 1)).toEqual(['c', 'a'])
  })

  it('集合封閉時會繞頭尾', () => {
    expect(lightboxNeighbourUrls(items('a', 'b', 'c'), 0)).toEqual(['b', 'c'])
  })

  it('集合未封閉時不繞——那兩張不是下一步會走到的圖', () => {
    expect(lightboxNeighbourUrls(items('a', 'b', 'c'), 0, true)).toEqual(['b'])
    expect(lightboxNeighbourUrls(items('a', 'b', 'c'), 2, true)).toEqual(['b'])
  })

  it('單張與空集合沒有鄰居', () => {
    expect(lightboxNeighbourUrls(items('a'), 0)).toEqual([])
    expect(lightboxNeighbourUrls([], 0)).toEqual([])
  })
})

// ------------------------------------------------------------- 元件 markup

const URL_A = '/v1/public/characters/abc/one.png'
const URL_B = '/v1/public/characters/abc/two.png'

describe('浮窗的 SSR markup', () => {
  it('是一個 modal 對話框，帶得出來的 aria-label', async () => {
    const html = await render({ items: items(URL_A) })
    expect(html).toContain('role="dialog"')
    expect(html).toContain('aria-modal="true"')
    expect(html).toMatch(/aria-label="[^"]+"/)
  })

  it('關閉時什麼都不渲染', async () => {
    const html = await render({ visible: false, items: items(URL_A) })
    expect(html).not.toContain('role="dialog"')
  })

  it('圖片走 UiImage variant="full"，不是手刻的 <img src=原檔>', async () => {
    const html = await render({ items: items(URL_A) })
    // `full` 是同樣的像素、WebP 重編（2313 KB → 218 KB）。
    expect(html).toContain(`src="${URL_A}?v=full"`)
    // full 沒有 srcset（w 描述子需要真實像素寬，前端不知道原圖尺寸）。
    expect(html).not.toContain('srcset')
    expect(html).toContain('decoding="async"')
  })

  it('只掛 active 一張——鄰居不進 DOM', async () => {
    const html = await render({ items: items(URL_A, URL_B) })
    expect(html).toContain(URL_A)
    expect(html).not.toContain(URL_B)
  })

  it('多張時顯示 `3 / 12` 這種位置指示，而不是 dots', async () => {
    const html = await render({
      items: items(URL_A, URL_B, '/c.png'),
      index: 2,
    })
    expect(html).toContain('3 / 3')
    expect(html).not.toContain('dot')
  })

  it('單張時不畫左右鍵也不畫位置指示', async () => {
    const html = await render({ items: items(URL_A) })
    expect(html).not.toContain('ui-lightbox__nav')
    expect(html).not.toContain('ui-lightbox__counter"')
  })

  it('單張但還有下一頁時，左右鍵仍在（按下去會去載第二頁）', async () => {
    const html = await render({ items: items(URL_A), hasMore: true })
    expect(html).toContain('ui-lightbox__nav')
  })

  it('caption 顯示在圖片下方的 figcaption', async () => {
    const html = await render({
      items: [{ url: URL_A, caption: '傍晚的天空' }],
    })
    expect(html).toContain('<figcaption')
    expect(html).toContain('傍晚的天空')
  })

  it('沒有 alt 就退回 caption，再退回預設字串', async () => {
    const captioned = await render({ items: [{ url: URL_A, caption: '傍晚的天空' }] })
    expect(captioned).toContain('alt="傍晚的天空"')
    const bare = await render({ items: items(URL_A) })
    expect(bare).toMatch(/alt="[^"]+"/)
    expect(bare).not.toContain('alt=""')
  })

  it('續載中會說出來', async () => {
    const html = await render({
      items: items(URL_A, URL_B),
      hasMore: true,
      loadingMore: true,
    })
    expect(html).toContain('ui-lightbox__status')
    expect(html).toContain(zhTW.lightbox.loadingMore)
  })

  it('續載失敗會說出來，並給一顆重試（LB2：不能無聲卡在最後一張）', async () => {
    const html = await render({
      items: items(URL_A, URL_B),
      hasMore: true,
      loadingMore: false,
      loadMoreError: '載入相簿失敗',
    })
    // 呼叫端的訊息優先——它常帶伺服端 detail，比通用句有用。
    expect(html).toContain('載入相簿失敗')
    expect(html).toContain('role="alert"')
    expect(html).toContain(zhTW.lightbox.retry)
  })

  it('沒有訊息但確實失敗時，退回通用句', async () => {
    const html = await render({
      items: items(URL_A, URL_B),
      hasMore: true,
      loadMoreError: '   ',
    })
    // 全空白＝呼叫端把錯誤清掉了，不該畫一條沒有字的失敗列。
    expect(html).not.toContain(zhTW.lightbox.retry)
  })

  it('重試在飛的時候只顯示載入，不並排上一次的失敗', async () => {
    const html = await render({
      items: items(URL_A, URL_B),
      hasMore: true,
      loadingMore: true,
      loadMoreError: '載入相簿失敗',
    })
    expect(html).toContain(zhTW.lightbox.loadingMore)
    expect(html).not.toContain('載入相簿失敗')
  })

  it('空集合不會炸，且留得下一顆關閉鍵', async () => {
    const html = await render({ items: [] })
    expect(html).toContain('role="dialog"')
    expect(html).toContain('ui-lightbox__empty')
  })
})

describe('「開原圖」——目前唯一的看原檔／另存路徑', () => {
  it('是一個指向原始 URL 的新分頁連結，不是尺寸變體', async () => {
    const html = await render({ items: items(URL_A) })
    expect(html).toContain(`href="${URL_A}"`)
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener"')
  })

  it('href 過 safeMediaHref 護欄：javascript: 塌成空字串就不畫連結', async () => {
    const html = await render({ items: items('javascript:alert(1)') })
    expect(html).not.toContain('javascript:')
    expect(html).not.toContain('target="_blank"')
  })

  it('控制字元夾在 scheme 中間也擋得住', async () => {
    const html = await render({ items: items('java\tscript:alert(1)') })
    expect(html).not.toContain('script:alert')
    expect(html).not.toContain('target="_blank"')
  })
})

// ------------------------------------------------- 純函式：鍵盤歸屬（FIX-A）

describe('哪些鍵屬於浮窗', () => {
  it('Esc 是關窗、←→ 是切換', () => {
    const open = { visible: true, showNav: true }
    expect(classifyLightboxKey({ key: 'Escape', ...open })).toBe('close')
    expect(classifyLightboxKey({ key: 'ArrowLeft', ...open })).toBe('prev')
    expect(classifyLightboxKey({ key: 'ArrowRight', ...open })).toBe('next')
  })

  it('沒有導覽時（單圖卡）←→ 仍然被吃掉，不放行去切角色卡', () => {
    // aria-modal 的 inert 語意：浮窗開著時底下那一層在鍵盤上等於不存在。
    // 放行的代價是 CharacterCardGalleryModal 會切卡，`:key` 重掛卡面元件，
    // 玩家正在看的那張大圖在按鍵瞬間消失。
    const single = { visible: true, showNav: false }
    expect(classifyLightboxKey({ key: 'ArrowLeft', ...single })).toBe('swallow')
    expect(classifyLightboxKey({ key: 'ArrowRight', ...single })).toBe('swallow')
    expect(lightboxKeyStopsPropagation('swallow')).toBe(true)
  })

  it('Tab 要處理（焦點 trap）但不得阻斷傳播', () => {
    expect(classifyLightboxKey({ key: 'Tab', visible: true, showNav: true })).toBe('trap-tab')
    expect(lightboxKeyStopsPropagation('trap-tab')).toBe(false)
  })

  it('無關的鍵原樣放行——開著看圖浮窗不該讓其他快捷鍵全部失效', () => {
    expect(classifyLightboxKey({ key: 'a', visible: true, showNav: true })).toBe('ignore')
    expect(classifyLightboxKey({ key: 'Enter', visible: true })).toBe('ignore')
    expect(lightboxKeyStopsPropagation('ignore')).toBe(false)
  })

  it('浮窗關著時什麼都不吃（監聽可能還掛著）', () => {
    expect(classifyLightboxKey({ key: 'Escape', visible: false, showNav: true })).toBe('ignore')
    expect(classifyLightboxKey({ key: 'ArrowLeft' })).toBe('ignore')
  })

  it('會被浮窗處理的鍵一律阻斷傳播', () => {
    expect(lightboxKeyStopsPropagation('close')).toBe(true)
    expect(lightboxKeyStopsPropagation('prev')).toBe(true)
    expect(lightboxKeyStopsPropagation('next')).toBe(true)
  })
})

// ------------------------------------------- 純函式：手勢起點（FIX-A2／A3）

describe('這一次 pointerdown 該不該開始記錄手勢', () => {
  it('單指、左鍵、不在圖說上＝記', () => {
    expect(shouldTrackLightboxPointer({})).toBe(true)
    expect(
      shouldTrackLightboxPointer({
        pointerType: 'touch',
        isPrimary: true,
        activePointerCount: 1,
      }),
    ).toBe(true)
    expect(shouldTrackLightboxPointer({ pointerType: 'mouse', button: 0 })).toBe(true)
  })

  it('第二指落下就放棄——那是雙指縮放，不是換圖', () => {
    expect(shouldTrackLightboxPointer({ pointerType: 'touch', activePointerCount: 2 })).toBe(false)
    expect(shouldTrackLightboxPointer({ pointerType: 'touch', isPrimary: false })).toBe(false)
  })

  it('畫面已經被放大時，單指拖曳是平移不是換圖', () => {
    // 手指離開之後畫面仍停在放大狀態，此時 clientX 的位移隨便就過 40px；
    // 判成換圖的話玩家正在放大檢視的那張圖會當場消失。
    expect(shouldTrackLightboxPointer({ pointerType: 'touch', viewportScale: 2.4 })).toBe(false)
    // 沒放大時不得誤擋（scale 幾乎不會剛好是 1）。
    expect(shouldTrackLightboxPointer({ pointerType: 'touch', viewportScale: 1 })).toBe(true)
    expect(shouldTrackLightboxPointer({ pointerType: 'touch', viewportScale: 1.002 })).toBe(true)
    // 取不到 visualViewport 的舊 WebView 一樣要能滑。
    expect(shouldTrackLightboxPointer({ pointerType: 'touch', viewportScale: Number.NaN })).toBe(true)
  })

  it('滑鼠的非左鍵不算手勢', () => {
    expect(shouldTrackLightboxPointer({ pointerType: 'mouse', button: 2 })).toBe(false)
    expect(shouldTrackLightboxPointer({ pointerType: 'mouse', button: 1 })).toBe(false)
    // 觸控的 button 是 0 以外的值也不該被誤擋（規則只針對 mouse）。
    expect(shouldTrackLightboxPointer({ pointerType: 'touch', button: -1 })).toBe(true)
  })

  it('起點落在圖說裡就放棄——玩家在選字，不是在滑', () => {
    expect(shouldTrackLightboxPointer({ withinCaption: true })).toBe(false)
  })
})

// ------------------------------------------------- 純函式：預取靜止窗（A4）

describe('鄰居預取的靜止窗', () => {
  it('窗沒過就不送——按住 → 三秒不該送出 90–180 筆 218 KB 的請求', () => {
    expect(lightboxPrefetchSettled({ changedAt: 1000, now: 1000 + 30 })).toBe(false)
    expect(
      lightboxPrefetchSettled({ changedAt: 1000, now: 1000 + LIGHTBOX_PREFETCH_DEBOUNCE_MS - 1 }),
    ).toBe(false)
  })

  it('玩家停下來就送', () => {
    expect(
      lightboxPrefetchSettled({ changedAt: 1000, now: 1000 + LIGHTBOX_PREFETCH_DEBOUNCE_MS }),
    ).toBe(true)
    expect(lightboxPrefetchSettled({ changedAt: 1000, now: 5000 })).toBe(true)
  })

  it('窗長度可以被呼叫端指定', () => {
    expect(lightboxPrefetchSettled({ changedAt: 0, now: 10, debounceMs: 5 })).toBe(true)
    expect(lightboxPrefetchSettled({ changedAt: 0, now: 10, debounceMs: 50 })).toBe(false)
  })

  it('量不到時間就不送流量', () => {
    expect(lightboxPrefetchSettled({ changedAt: Number.NaN, now: 10 })).toBe(false)
    expect(lightboxPrefetchSettled({ changedAt: 0, now: Number.POSITIVE_INFINITY })).toBe(false)
  })

  it('靜止窗是「玩家停在這張」的量級，不是隨手挑的數字', () => {
    expect(LIGHTBOX_PREFETCH_DEBOUNCE_MS).toBeGreaterThanOrEqual(150)
    expect(LIGHTBOX_PREFETCH_DEBOUNCE_MS).toBeLessThanOrEqual(250)
  })
})

// ------------------------------------------------------------- 原始碼掃描

describe('攔截責任在浮窗，不在宿主（原始碼掃描）', () => {
  // 這裡釘的是 FIX-A 的治本改動。SSR harness 看不到事件傳播，但「監聽掛在哪一
  // 個階段」「有沒有 stopPropagation」是原始碼裡讀得出來的事實。

  it('window keydown 掛在 capture 階段——事件路徑的第一站', () => {
    expect(LIGHTBOX_SOURCE).toContain('const KEYDOWN_OPTIONS = { capture: true } as const')
    expect(LIGHTBOX_SOURCE).toContain(
      "window.addEventListener('keydown', onKeydown, KEYDOWN_OPTIONS)",
    )
  })

  it('移除時帶同一個 capture 值，否則監聽永遠拆不掉', () => {
    expect(LIGHTBOX_SOURCE).toContain(
      "window.removeEventListener('keydown', onKeydown, KEYDOWN_OPTIONS)",
    )
  })

  it('只對真的處理掉的鍵停止傳播，且用 stopPropagation 不是 stopImmediatePropagation', () => {
    expect(LIGHTBOX_SOURCE).toContain(
      'if (lightboxKeyStopsPropagation(action)) event.stopPropagation()',
    )
    // stopImmediatePropagation 會連同一節點同階段的其他監聽（巢狀外層浮窗）
    // 一起打死，那不是要擋的東西。
    expect(LIGHTBOX_SOURCE).not.toContain('.stopImmediatePropagation(')
  })

  it('三個宿主都不再有手寫讓位——那套模式漏一個不會有測試變紅', () => {
    const panel = source('../src/components/CharacterImagesPanel.vue')
    const face = source('../src/components/CharacterCardFace.vue')
    const modal = source('../src/components/CharacterCardGalleryModal.vue')
    expect(panel).not.toContain('stageZoomOpen.value) return')
    expect(face).not.toContain('zoom-open-change')
    expect(modal).not.toContain('cardZoomOpen')
  })
})

describe('雙指縮放交給瀏覽器原生（計畫 §4.2，原始碼掃描）', () => {
  it('touch-action 帶 pinch-zoom——只寫 pan-y 等於整個浮窗不能雙指放大', () => {
    // 改版前那張圖是開新分頁的原圖、可以 pinch 到 100%。把它做壞就是拆掉
    // 「看清楚原圖」這個浮窗存在的理由。
    expect(LIGHTBOX_SOURCE).toMatch(/touch-action:\s*pan-y\s+pinch-zoom\s*;/)
    expect(LIGHTBOX_SOURCE).not.toMatch(/touch-action:\s*pan-y\s*;/)
  })

  it('CSS 沒有手寫 -webkit- 前綴（Vite 8 的 lightningcss 會吃掉標準行）', () => {
    const style = LIGHTBOX_SOURCE.slice(LIGHTBOX_SOURCE.indexOf('<style scoped>'))
    expect(style).not.toContain('-webkit-')
  })
})

describe('預取有節流，且關窗／卸載會收掉計時器（原始碼掃描）', () => {
  it('索引變動走的是排程而不是立刻送出請求', () => {
    expect(LIGHTBOX_SOURCE).toContain('schedulePrefetch()')
    // watch 裡不得再直接呼叫 prefetchNeighbours()——那就是沒有節流。
    const watchAt = LIGHTBOX_SOURCE.indexOf('watch([activeIndex, () => props.items]')
    expect(watchAt).toBeGreaterThan(-1)
    expect(LIGHTBOX_SOURCE.slice(watchAt, watchAt + 200)).not.toContain('prefetchNeighbours()')
  })

  it('關窗與卸載都會 cancelPrefetch——留著的計時器會在關窗後才送出流量', () => {
    const closeAt = LIGHTBOX_SOURCE.indexOf('function onClose()')
    expect(closeAt).toBeGreaterThan(-1)
    expect(LIGHTBOX_SOURCE.slice(closeAt, closeAt + 300)).toContain('cancelPrefetch()')
    const unmountAt = LIGHTBOX_SOURCE.indexOf('onBeforeUnmount(')
    expect(LIGHTBOX_SOURCE.slice(unmountAt)).toContain('cancelPrefetch()')
  })

  it('預熱請求標成低優先，不跟玩家正在等的那一張搶頻寬', () => {
    expect(LIGHTBOX_SOURCE).toContain("fetchPriority = 'low'")
  })
})

/** `UiLightbox.vue` 裡某支函式從宣告到下一個 top-level 宣告之間的原始碼。 */
function lightboxFunction(name: string, until: string): string {
  const start = LIGHTBOX_SOURCE.indexOf(name)
  expect(start).toBeGreaterThan(-1)
  const end = LIGHTBOX_SOURCE.indexOf(until, start)
  expect(end).toBeGreaterThan(start)
  return LIGHTBOX_SOURCE.slice(start, end)
}

describe('手勢：圖說與多指（原始碼掃描）', () => {
  it('pointerdown 把 isPrimary／同時按著幾指／是不是圖說都餵給純函式', () => {
    const body = lightboxFunction('function onPointerDown', 'function onPointerUp')
    expect(body).toContain('shouldTrackLightboxPointer({')
    expect(body).toContain('isPrimary: event.isPrimary')
    expect(body).toContain('activePointerCount: activePointerIds.size')
    expect(body).toContain('viewportScale: currentViewportScale()')
    expect(body).toContain('withinCaption: startedInCaption(event)')
  })

  it('第二指落下會把已經記到一半的起點丟掉', () => {
    const body = lightboxFunction('function onPointerDown', 'function onPointerUp')
    expect(body).toContain('forgetSwipeStart()')
  })

  it('新的一次接觸無條件清掉上一次的手勢旗標（在任何 return 之前）', () => {
    // 旗標原本只在「開始記錄手勢」跑到底時歸零，另一頭只有 onOverlayClick 會
    // 消費它——而那支在點到的不是背景本身時先 return。於是橫滑起訖都落在圖片
    // 上（後續 click 的 target 是圖片）時旗標留在 true：玩家接著點背景想關窗，
    // 第一次被靜靜吞掉，要點第二次。
    const body = lightboxFunction('function onPointerDown', 'function onPointerUp')
    const reset = body.indexOf('swipeHandled = false')
    const guard = body.indexOf('shouldTrackLightboxPointer')
    expect(reset).toBeGreaterThan(-1)
    expect(guard).toBeGreaterThan(-1)
    expect(reset).toBeLessThan(guard)
  })

  it('圖說的判定用 closest，descendant 也算在圖說裡', () => {
    expect(LIGHTBOX_SOURCE).toContain("const CAPTION_SELECTOR = '.ui-lightbox__caption'")
    expect(LIGHTBOX_SOURCE).toContain('target.closest(CAPTION_SELECTOR) !== null')
  })
})

describe('續載的等待記號活在浮窗裡（FIX-E，原始碼掃描）', () => {
  // 判定本身有純函式閘（上面的 `lightboxResumeAction`）；這裡釘的是接線——
  // 記號記了什麼、什麼時候作廢。這一段曾經被迫做在消費者（`AlbumPanel`）身上：
  // 面板拆掉 `v-model` 自己開一個 flush 窗去否決不該收的那次 `update:index`。
  // 那是邊界補償——產生點仍在浮窗，而且靠 Vue 的 flush 順序才成立、每個會非
  // 同步成長的新消費者都得重抄一遍。
  it('記號同時記下「當時有幾張」與「他站在哪一張」', () => {
    const body = lightboxFunction('function requestLoadMore()', 'function goPrev()')
    expect(body).toContain('pendingLoad.value = { total: total.value, index: activeIndex.value }')
  })

  it('浮窗只要真的換過一次圖，記號當場作廢', () => {
    // 這是根因那一行：玩家自己走的那一步取代了被推遲的那一步。少了它，續載
    // 回來時浮窗會照著過期的記號把畫面往前推，而玩家早就翻到別處了。
    const body = lightboxFunction('function setIndex(next: number)', 'function step(')
    expect(body).toContain('pendingLoad.value = null')
    const cleared = body.indexOf('pendingLoad.value = null')
    const emitted = body.indexOf("emit('update:index'")
    expect(emitted).toBeGreaterThan(cleared)
  })

  it('清單長大之後的判定整支交給純函式，watcher 只負責照做', () => {
    const body = lightboxFunction('watch(total, (after)', '/**')
    expect(body).toContain('lightboxResumeAction({')
    expect(body).toContain('pending: pendingLoad.value')
    expect(body).toContain('current: activeIndex.value')
    expect(body).toContain('totalAfter: after')
    // wait＝記號留著；其餘兩態才收記號。
    expect(body).toContain("if (action.outcome === 'wait') return")
    expect(body).toContain("if (action.outcome === 'move') setIndex(action.index)")
  })

  it('關窗把記號收掉——下次開窗不該欠上一次的一步', () => {
    const body = lightboxFunction('function onClose()', 'watch(')
    expect(body).toContain('pendingLoad.value = null')
  })
})

describe('背景捲動鎖：跨 instance 的那兩個變數真的在模組層級（FIX-E）', () => {
  // `<script setup>` 整段會被編譯進 `setup()`，寫在它「頂層」的 `let` 是
  // per-instance。原本註解宣稱「模組層級的計數…讓最後一個關窗的人才還原」，
  // 那個保證並不存在（計數恆為 0→1→0），只是巢狀開關剛好都是 LIFO 才沒出事。
  const NORMAL_SCRIPT = LIGHTBOX_SOURCE.slice(
    LIGHTBOX_SOURCE.indexOf('<script lang="ts">'),
    LIGHTBOX_SOURCE.indexOf('<script setup lang="ts">'),
  )

  it('計數與「上鎖那一刻的 overflow」宣告在普通 <script> 區塊裡', () => {
    expect(NORMAL_SCRIPT).toContain('let scrollLockCount = 0')
    expect(NORMAL_SCRIPT).toContain("let previousBodyOverflow = ''")
  })

  it('每個變數只宣告一次——setup 裡不得再有一份同名的影子', () => {
    expect(LIGHTBOX_SOURCE.match(/let scrollLockCount/g)).toHaveLength(1)
    expect(LIGHTBOX_SOURCE.match(/let previousBodyOverflow/g)).toHaveLength(1)
  })

  it('holdsScrollLock 刻意留在 setup 裡——它問的是「我這個 instance」', () => {
    // 搬上去會讓第二個浮窗看到「已經持有」而整個跳過計數，先關的那個又直接
    // 把鎖解掉。同一段程式碼裡兩種作用域各有理由。
    expect(NORMAL_SCRIPT).not.toContain('let holdsScrollLock')
    expect(LIGHTBOX_SOURCE).toContain('let holdsScrollLock = false')
  })

  it('還原仍然只發生在計數歸零時', () => {
    const body = lightboxFunction('function unlockBodyScroll()', '// ---')
    expect(body).toContain('scrollLockCount = Math.max(0, scrollLockCount - 1)')
    expect(body).toContain('if (scrollLockCount === 0')
  })
})
