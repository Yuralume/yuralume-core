/**
 * LB2 —— 相簿接入放大浮窗（計畫 `IMAGE_LIGHTBOX_PLAN.md` §5）。
 *
 * **這個檔案為什麼有一半是原始碼掃描，而不是渲染出來看。**
 *
 * 前端 harness 是 SSR（`createSSRApp` + `renderToString`，沒有 jsdom），而
 * `AlbumPanel` 的清單是 `onMounted` 之前那個 `watch(characterId, reload,
 * { immediate: true })` **非同步**抓回來的——`reload()` 在第一個 `await` 就
 * 讓出去了，SSR 不會等它。實測過：不管把 `listAlbum` mock 成多快回，渲染出來
 * 的永遠是 `album-empty` 的「載入中…」，相簿格一格都不存在。所以「格子改成
 * 按鈕了嗎」「浮窗接上續載了嗎」這類問題在這個 harness 裡渲染不出來，只能掃
 * 原始碼——慣例沿用 `uiImage.test.ts`（它也是用掃描釘住熱點檔案不得出現裸
 * `<img`）。
 *
 * 真正有決定的那一半（走到末端要不要續載、載回來要不要前進、失敗後能不能
 * 重試）全部在 `@/utils/lightbox` 的純函式裡，閘在 `lightbox.test.ts`。
 *
 * **沒有自動閘**（落地時手動驗收）：點縮圖真的開得起來、←／→ 走到末端真的
 * 觸發續載、換角色時浮窗真的關掉、focus 還原回被點的那一格。
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SOURCE = readFileSync(
  fileURLToPath(new URL('../src/components/AlbumPanel.vue', import.meta.url)),
  'utf-8',
)

/** 取 `<script setup>` 裡某個函式從宣告到下一個 top-level 宣告之間的原始碼。 */
function functionBody(name: string, until: string): string {
  const start = SOURCE.indexOf(name)
  expect(start).toBeGreaterThan(-1)
  const end = SOURCE.indexOf(until, start)
  expect(end).toBeGreaterThan(start)
  return SOURCE.slice(start, end)
}

describe('相簿格不再開新分頁', () => {
  it('整個面板沒有 target="_blank" 了', () => {
    // 相簿本來就是拿來連續看的：一張一張開再一張一張關，手機上每開一張多一個
    // 分頁。「看原檔／另存」那條路沒有消失，改由浮窗固定的「開原圖」承接。
    expect(SOURCE).not.toContain('target="_blank"')
  })

  it('縮圖是按鈕，不是被拔掉 href 的連結', () => {
    // 換掉 anchor 就得自己把鍵盤可達與 focus 環補回來；`<button type="button">`
    // 兩者都內建，剩下的 focus 環在 scoped CSS 裡。
    expect(SOURCE).toContain('class="album-image-button"')
    expect(SOURCE).toContain('type="button"')
    expect(SOURCE).toContain('@click="openZoom(index)"')
    expect(SOURCE).toContain('.album-image-button:focus-visible')
  })

  it('按鈕帶得出來的無障礙名稱，不是只有一張沒有說明的圖', () => {
    expect(SOURCE).toContain("t('common.actions.zoomAria'")
  })

  it('safeMediaHref 的 import 隨著 anchor 一起清掉（護欄改由浮窗內部負責）', () => {
    expect(SOURCE).not.toContain('safeMediaUrl')
  })
})

describe('浮窗接的是已載入的相簿項目', () => {
  it('集合＝目前 items，不預先展開到 total', () => {
    // 沒載到的項目沒有 URL，放進來只會變成一格永遠空白的圖。
    expect(SOURCE).toContain('items.value.map(item => ({ url: item.url, caption: item.caption }))')
    expect(SOURCE).toContain(':items="lightboxItems"')
  })

  it('走到已載入的末端會續載，接的是既有的 loadMore()', () => {
    // 與往下捲共用同一支：兩邊各自維護 keyset 游標會讓同一頁被要兩次。
    expect(SOURCE).toContain(':has-more="hasMore"')
    expect(SOURCE).toContain('@load-more="onLightboxLoadMore"')
    const body = functionBody('function onLightboxLoadMore()', 'async function reload()')
    expect(body).toContain('void loadMore()')
  })

  it('續載中與續載失敗都回報給浮窗', () => {
    // 浮窗蓋住整個面板，面板自己的錯誤列在 overlay 底下看不到。
    expect(SOURCE).toContain(':loading-more="loadingMore"')
    expect(SOURCE).toContain(':load-more-error="loadMoreError"')
  })

  it('續載失敗的訊息是 loadMore 自己的，不是面板共用的那條', () => {
    // errorMsg 同時承載刪除／晉升的失敗，整條餵給浮窗會讓重試鍵重試錯的東西。
    const body = functionBody('async function loadMore()', 'async function handleDelete')
    expect(body).toContain('loadMoreError.value = null')
    expect(body).toContain('loadMoreError.value = message')
  })

  it('索引就是 v-model，面板不再需要准駁點', () => {
    expect(SOURCE).toContain('v-model:index="zoomIndex"')
    expect(SOURCE).not.toContain('@update:index=')
  })
})

describe('續載途中玩家往回翻，那段空窗的規矩不在面板這裡（FIX-E）', () => {
  // 這裡曾經有一整套邊界補償：面板記「這次續載是從哪一張按出來的」、拆掉
  // `v-model` 換成單向 `:index` + `@update:index` 以取得准駁點、再開一個
  // `nextTick` 的 flush 窗把「續載補走的那一步」與「玩家自己按的下一張」分開。
  // 它擋得住那個劇本，但產生點仍在浮窗——浮窗照樣記錯的東西、照樣送出不該送的
  // 更新，而且整套依賴 Vue 的 flush 順序，下一個會非同步成長的消費者得重抄。
  //
  // 根因修在 `UiLightbox` 之後（記號改記位置、玩家一走就作廢），面板回到單純的
  // `v-model`。**那個劇本的閘沒有被刪掉，是搬家了**：判定在
  // `lightbox.test.ts` 的 `lightboxResumeAction`（24 張→在 index 23 按下一張→
  // 往回翻到 index 10→第二頁回來 48 張→`stay`），接線在同一支測試的
  // 「續載的等待記號活在浮窗裡」原始碼掃描。
  it('面板不再持有續載的等待記號', () => {
    expect(SOURCE).not.toContain('loadMoreFromIndex')
    expect(SOURCE).not.toContain('resumeWindowOpen')
    expect(SOURCE).not.toContain('armResumeWindow')
    expect(SOURCE).not.toContain('isLightboxResumeOwed')
  })

  it('也不再需要那個 flush 窗——面板裡沒有 nextTick', () => {
    // 依賴 flush 順序的補償最容易在別人改一行無關的東西時無聲失效。
    expect(SOURCE).not.toContain('nextTick')
  })

  it('續載入口只剩「去要下一頁」這件事', () => {
    const body = functionBody('function onLightboxLoadMore()', 'async function reload()')
    expect(body).toContain('void loadMore()')
    expect(body).not.toContain('zoomIndex')
  })
})

describe('續載失敗訊息綁「這次開窗」，不綁面板', () => {
  it('開窗先清掉上一次的失敗', () => {
    // 否則玩家改點第 3 張縮圖，一開窗就在圖底下看到「載入相簿失敗」——當下根本
    // 沒有任何請求發生過，而那顆重試鍵還是活的。
    const body = functionBody('function openZoom(at: number)', 'function closeZoom')
    expect(body).toContain('loadMoreError.value = null')
  })

  it('關窗與開窗對稱地收掉', () => {
    const body = functionBody('function closeZoom()', 'function onLightboxLoadMore')
    expect(body).toContain('zoomOpen.value = false')
    expect(body).toContain('loadMoreError.value = null')
    expect(SOURCE).toContain('@close="closeZoom"')
  })
})

describe('換角色時浮窗要關掉', () => {
  it('reload() 一開頭就收掉浮窗', () => {
    // 索引指的是舊角色那份清單；留著就會在新清單落地的瞬間變成同一格位置的
    // 另一個人的圖。reload 是 characterId watch 的 handler，關在這裡最省。
    const body = functionBody('async function reload()', 'async function loadMore()')
    expect(body).toContain('zoomOpen.value = false')
    expect(body).toContain('loadMoreError.value = null')
  })
})

describe('EC2-C 託管角色分支不得被動到', () => {
  it('「晉升為舞台圖」仍然只在非 managed 時渲染', () => {
    expect(SOURCE).toContain('v-if="!managed"')
    expect(SOURCE).toContain("t('characterEdit.managed.albumNotice')")
  })

  it('刪除／晉升留在縮圖格子上，沒有被搬進浮窗', () => {
    // 計畫 §3.3：primitive 不開 actions slot。兩顆按鈕仍在 .album-actions 裡。
    const grid = SOURCE.slice(SOURCE.indexOf('class="album-grid"'), SOURCE.indexOf('album-sentinel'))
    expect(grid).toContain("handlePromote(item)")
    expect(grid).toContain("handleDelete(item)")
  })
})
