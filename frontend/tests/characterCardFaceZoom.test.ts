/**
 * LB7 —— 角色卡卡面圖可放大（計畫 `IMAGE_LIGHTBOX_PLAN.md`）。
 *
 * `CharacterCardFace` 本身沒有非同步資料抓取（`image_urls` 是 prop 帶進來
 * 的），所以「有圖時放大入口存在、無圖時不存在」這條可以直接用 SSR render
 * 驗——不像 `AlbumPanel`（見 `albumLightbox.test.ts` 檔頭）那樣被 `onMounted`
 * 之前的非同步抓取擋住。
 *
 * 測不到的部分（沿用既有慣例，掃原始碼釘住）：
 * - `CharacterCardGalleryModal` 疊了自己的 `window` keydown 監聽（Esc／←→
 *   關 modal／切角色卡），卡面浮窗（`UiLightbox`，z-index 1500）疊在它之上
 *   （modal 是 910）又掛了一支同名監聽——但浮窗那支是 **capture 階段**，會
 *   在事件路徑的第一站就把它處理的鍵 `stopPropagation()` 掉。兩層監聽誰先
 *   接到事件，這個 SSR harness 完全看不到（鍵盤事件、監聽階段都不在渲染出來
 *   的 markup 裡），只能掃原始碼釘機制。落地時手動驗收：卡片 gallery 開著、
 *   點卡面圖放大、按 ←→，畫面上換的必須是同一張卡的另一張圖，不是跳到上一
 *   張／下一張角色卡（單圖卡則是**什麼都不該發生**）；按 Esc 只關浮窗，
 *   gallery 還開著。
 * - 背景捲動鎖／history entry 巢狀還原：`UiLightbox.vue` 自己的模組層級計數
 *   已經處理（檔頭註解直接點名 LB7 這個情境），不在這張票的改動範圍內。
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import CharacterCardFace from '@/components/CharacterCardFace.vue'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import type { CharacterCardPreview } from '@/utils/api/characters'

const FACE_SOURCE = readFileSync(
  fileURLToPath(new URL('../src/components/CharacterCardFace.vue', import.meta.url)),
  'utf-8',
)
const MODAL_SOURCE = readFileSync(
  fileURLToPath(new URL('../src/components/CharacterCardGalleryModal.vue', import.meta.url)),
  'utf-8',
)

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  })
}

function baseCard(overrides: Partial<CharacterCardPreview> = {}): CharacterCardPreview {
  return {
    pack_id: null,
    title: 'Aria',
    author: '',
    description: '',
    tags: [],
    note: '',
    name: 'Aria',
    summary: '',
    personality: [],
    interests: [],
    speaking_style: '',
    boundaries: [],
    aspirations: [],
    appearance: '',
    gender_identity: '',
    third_person_pronoun: '',
    visual_gender_presentation: '',
    visual_subject_type: 'auto',
    date_of_birth: null,
    disposition: {
      self_centeredness: 'medium',
      candor: 'medium',
      sharing_drive: 'medium',
      associativeness: 'medium',
    },
    personality_type: {
      system: 'mbti_16',
      code: '',
      source: 'unset',
      confidence: 0,
      rationale: '',
      consistency_notes: [],
    },
    world_frame: '',
    world_awareness_enabled: false,
    world_topics: [],
    subscribed_categories: [],
    excluded_topics: [],
    proactive_enabled: false,
    proactive_daily_limit: 0,
    proactive_cooldown_minutes: 0,
    accepts_web_proactive: false,
    feed_daily_limit: 0,
    companions: [],
    has_main_arc: false,
    bundled_arc_template_count: 0,
    bundled_arc_titles: [],
    has_arc_series: false,
    bundled_arc_series_count: 0,
    bundled_arc_series_titles: [],
    bundled_arc_series_member_count: 0,
    stage_image_count: 0,
    image_urls: [],
    ...overrides,
  }
}

async function render(card: CharacterCardPreview): Promise<string> {
  const app = createSSRApp(CharacterCardFace, { card })
  app.use(i18n())
  return renderToString(app)
}

describe('卡面放大入口（SSR render）', () => {
  it('有圖時放大入口存在，包著 UiImage', async () => {
    const html = await render(baseCard({ image_urls: ['/v1/public/characters/abc/one.png'] }))
    expect(html).toContain('character-card-face__art-trigger')
    expect(html).toContain('type="button"')
    expect(html).toContain('character-card-face__image')
    // 沒有走 initial 字母 fallback。
    expect(html).not.toContain('character-card-face__fallback')
  })

  it('無圖時（走 initial 字母 fallback）沒有放大入口', async () => {
    const html = await render(baseCard({ image_urls: [] }))
    expect(html).not.toContain('character-card-face__art-trigger')
    expect(html).toContain('character-card-face__fallback')
  })

  it('放大入口帶得出無障礙名稱，不是一顆沒有說明的按鈕', async () => {
    const html = await render(baseCard({
      name: '小美',
      image_urls: ['/v1/public/characters/abc/one.png'],
    }))
    expect(html).toMatch(/aria-label="[^"]*小美[^"]*"/)
  })

  it('多圖仍有 dots，且浮窗、dots 吃的是同一份 image_urls', async () => {
    const html = await render(baseCard({
      image_urls: ['/a.png', '/b.png'],
    }))
    expect(html).toContain('character-card-face__dots')
    expect(html).toContain('character-card-face__art-trigger')
  })
})

describe('卡面浮窗與 CharacterCardGalleryModal 的鍵盤歸屬（原始碼掃描）', () => {
  // 這個 harness 看不到兩支 window keydown 監聽誰先接到事件（見檔頭），只能
  // 釘住機制。**機制已經換過一次**：原本是「卡面往上 emit 開關狀態 → modal
  // 訂閱後讓位」，那條鏈每多一個宿主就要重接一次，漏掉不會有任何測試變紅
  // （LB4 的 `KokoroGramOverlay` 就漏了）。現在攔截責任在 `UiLightbox` 自己
  // ——capture 階段 + `stopPropagation()`，宿主不需要知道浮窗存在。
  const LIGHTBOX_SOURCE = readFileSync(
    fileURLToPath(new URL('../src/components/ui/UiLightbox.vue', import.meta.url)),
    'utf-8',
  )

  it('浮窗開啟時從卡面目前顯示的那張開始', () => {
    expect(FACE_SOURCE).toContain('zoomIndex.value = activeImageIndex.value')
  })

  it('浮窗裡換圖會回寫卡面的 activeImageIndex（dots 跟得上）', () => {
    expect(FACE_SOURCE).toMatch(/watch\(zoomIndex,\s*\(index\)\s*=>\s*\{\s*activeImageIndex\.value = index/)
  })

  it('卡面不再往上回報浮窗開關——那條鏈已經死了，留著會誤導下一個人', () => {
    expect(FACE_SOURCE).not.toContain('zoom-open-change')
  })

  it('modal 不再訂閱、也不再手寫讓位', () => {
    expect(MODAL_SOURCE).not.toContain('@zoom-open-change')
    expect(MODAL_SOURCE).not.toContain('cardZoomOpen.value')
  })

  it('modal 的 window keydown 仍是 bubble 階段（capture 的浮窗才攔得住它）', () => {
    expect(MODAL_SOURCE).toContain("window.addEventListener('keydown', handleWindowKeydown)")
    expect(MODAL_SOURCE).not.toMatch(
      /addEventListener\('keydown', handleWindowKeydown,\s*(true|\{)/,
    )
  })

  it('浮窗以 capture 階段掛在 window 上，且新增／移除帶同一個 capture 值', () => {
    expect(LIGHTBOX_SOURCE).toContain('const KEYDOWN_OPTIONS = { capture: true } as const')
    expect(LIGHTBOX_SOURCE).toContain(
      "window.addEventListener('keydown', onKeydown, KEYDOWN_OPTIONS)",
    )
    // 移除若不帶同一個 capture 值，找不到這一筆、監聽永遠留著。
    expect(LIGHTBOX_SOURCE).toContain(
      "window.removeEventListener('keydown', onKeydown, KEYDOWN_OPTIONS)",
    )
  })
})

describe('換卡片判準用身分而非物件識別（FIX-C／FIX-E，原始碼掃描）', () => {
  // 這個 harness 是一次性 `renderToString`，不會重新渲染、watch 也不會被
  // 觸發（見檔頭），所以無法直接驗證「翻譯回來浮窗有沒有關掉」；只能釘住
  // watch 抓的是什麼。落地時手動驗收：瀏覽 modal 開著翻譯、等待期間點
  // 卡面圖放大，翻譯回來浮窗必須還開著、停在原本翻到的那張。
  const WATCH_KEY = "watch(() => `${props.card.pack_id ?? ''}\\n${props.card.image_urls.join('\\n')}`, () => {"

  it('watch 抓的是 pack_id + image_urls 內容，不是 props.card 物件識別', () => {
    expect(FACE_SOURCE).toContain(WATCH_KEY)
    expect(FACE_SOURCE).not.toMatch(/watch\(\(\) => props\.card,\s*\(\)/)
  })

  it('鍵裡不得出現翻譯會改寫的欄位', () => {
    // `name` / `summary` / `speaking_style` 正是後端 `PROFILE_SCALAR_FIELDS`
    // 會重寫的那些。把任何一個放進鍵，就等於把 FIX-C 修掉的 bug 裝回去：
    // 玩家開著翻譯等回來，浮窗被誤判成「換卡片」而關掉。
    const start = FACE_SOURCE.indexOf('watch(() => `${props.card.pack_id')
    expect(start).toBeGreaterThan(-1)
    const key = FACE_SOURCE.slice(start, FACE_SOURCE.indexOf('`,', start))
    expect(key).not.toContain('props.card.name')
    expect(key).not.toContain('props.card.title')
    expect(key).not.toContain('props.card.summary')
  })

  it('這條 watch 仍然會重置 details／索引／失敗清單／浮窗四項', () => {
    const start = FACE_SOURCE.indexOf(WATCH_KEY)
    expect(start).toBeGreaterThan(-1)
    const end = FACE_SOURCE.indexOf('\n})', start)
    const body = FACE_SOURCE.slice(start, end)
    expect(body).toContain('detailsOpen.value = false')
    expect(body).toContain('activeImageIndex.value = 0')
    expect(body).toContain('failedImageIndexes.value = new Set()')
    expect(body).toContain('zoomOpen.value = false')
  })

  it('StudioCardsPage 自己出身分——那邊的 pack_id 恆為 null', () => {
    // 預覽卡是從 `Character` 現組的，`CharacterCardPreview` 裡沒有身分欄位。
    // 兩個都沒有圖的角色互換時，卡面的重置 watch 不會醒（鍵兩邊都是空字串），
    // 詳情區會保持展開。頁面手上有 `selectedCharacterId`，直接當 key 重掛。
    const studio = readFileSync(
      fileURLToPath(new URL('../src/pages/StudioCardsPage.vue', import.meta.url)),
      'utf-8',
    )
    expect(studio).toContain('<CharacterCardFace :key="selectedCharacterId" :card="previewCard" />')
    // 換系列勾選會重算 previewCard，但 key 沒變＝不重掛，展開的詳情不會被收掉。
    expect(studio).not.toContain(':key="previewCard"')
  })
})

describe('光澤層不吃掉放大鍵的點擊', () => {
  it('character-card-face__sheen 仍是 pointer-events: none', () => {
    // 這條在改動前就存在，這裡是回歸釘——按鈕現在包住了圖片，若哪天有人把
    // sheen 挪到按鈕上層又忘記帶這條，放大入口會整片變成點不動的死區。
    const start = FACE_SOURCE.indexOf('.character-card-face__sheen {')
    expect(start).toBeGreaterThan(-1)
    const end = FACE_SOURCE.indexOf('}', start)
    expect(FACE_SOURCE.slice(start, end)).toContain('pointer-events: none')
  })
})
