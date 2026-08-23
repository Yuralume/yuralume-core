/**
 * LB5 —— 舞台圖與生成候選圖接入放大浮窗（計畫 `IMAGE_LIGHTBOX_PLAN.md` §5）。
 *
 * 這張票補的是一個**從來沒有的能力**：這兩處以前連新分頁都沒有，玩家看不到
 * 大圖。所以這裡釘的不是「換掉了沒」，而是「加上去的東西有沒有壓到既有語意」。
 *
 * 兩種寫法混用的理由：
 *
 * - **舞台格**渲染得出來（SSR 只吃 props），所以「縮圖是不是按鈕」「四顆動作
 *   鍵有沒有被包進那顆按鈕裡」是真的渲染出來看的。
 * - **候選格**渲染不出來——`candidateUrls` 是按下「生成」之後才有的元件內部
 *   狀態，props 餵不進去。那一半只能掃原始碼，慣例沿用 `albumLightbox.test.ts`
 *   與 `uiImage.test.ts`。
 * - 有決定的那一半（清單被換掉時浮窗要跟還是要關）在 `@/utils/imageZoomSync`
 *   的純函式裡，直接測。
 *
 * **沒有自動閘**（落地時手動驗收）：點縮圖真的開得起來、候選圖的放大鍵在手機
 * 上按得到且不會誤觸循環挑選、Esc 在浮窗開著時只關浮窗、關窗後背景真的捲得動。
 */

import { describe, expect, it, vi } from 'vitest'
import { computed, createSSRApp, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { syncZoomAfterUrlsChange } from '@/utils/imageZoomSync'

vi.mock('@/composables/useRuntimeLimits', () => ({
  useRuntimeLimits: () => ({
    supported: computed(() => true),
    ttsEnabled: computed(() => true),
    albumGenerationEnabled: computed(() => true),
    videoGenerationEnabled: computed(() => true),
    characterSlots: computed(() => null),
    characterDailyCreates: computed(() => null),
    storyScenesDaily: computed(() => null),
    sessionMessageLimit: computed(() => null),
    characterCreationBlocked: computed(() => null),
    ensureLoaded: vi.fn(),
    refresh: vi.fn(),
    reset: vi.fn(),
  }),
}))

vi.mock('@/composables/useCloudCredits', () => ({
  refreshCloudCreditsAfterAction: vi.fn(),
  useCloudCredits: () => ({
    total: computed(() => 0),
    hasBalance: computed(() => false),
    stale: computed(() => false),
  }),
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    isAdmin: ref(false),
    cloudMode: ref(true),
    portalUrl: ref<string | null>(null),
    currentUser: ref<Record<string, unknown> | null>({ id: 'user-1' }),
  }),
}))

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

// 不是因為這裡會發請求：import 進來的真 router 模組會在 import 時建瀏覽器 history。
vi.mock('@/utils/authedFetch', () => ({ authedFetch: vi.fn() }))

vi.mock('@/utils/api/characters', () => ({
  commitPortraitCandidates: vi.fn(),
  deleteCharacterImage: vi.fn(),
  generatePortraitCandidates: vi.fn(),
  reorderCharacterImages: vi.fn(),
  uploadCharacterImage: vi.fn(),
}))

vi.mock('@/utils/api/album', () => ({ transferStageToAlbum: vi.fn() }))

const CharacterImagesPanel
  = (await import('@/components/CharacterImagesPanel.vue')).default

const SOURCE = readFileSync(
  fileURLToPath(new URL('../src/components/CharacterImagesPanel.vue', import.meta.url)),
  'utf-8',
)

const CATALOGS = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP } as const

async function render(props: Record<string, unknown>): Promise<string> {
  const app = createSSRApp(
    CharacterImagesPanel as Parameters<typeof createSSRApp>[0],
    props,
  )
  app.use(createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: CATALOGS,
  }))
  return renderToString(app)
}

const CHARACTER = {
  id: 'char-1',
  name: '芊璃',
  image_urls: [
    'https://media.example/objects/a.png',
    'https://media.example/objects/b.png',
  ],
}

/** 取一段從 `from` 到其後第一個 `until` 的原始碼／markup。 */
function sliceBetween(text: string, from: string, until: string): string {
  const start = text.indexOf(from)
  expect(start).toBeGreaterThan(-1)
  const end = text.indexOf(until, start)
  expect(end).toBeGreaterThan(start)
  return text.slice(start, end)
}

describe('舞台圖：90px 縮圖現在點得開', () => {
  it('縮圖被包成按鈕，而且仍然是 UiImage 在渲染', async () => {
    const html = await render({ character: CHARACTER })

    expect(html).toContain('image-zoom-button')
    // 縮圖不得退回裸 <img> 直取原檔——一張角色圖是 2.3 MB PNG。
    expect(html).toContain('ui-image')
    expect(SOURCE).toContain('@click="openStageZoom(index)"')
  })

  it('按鈕帶得出無障礙名稱，不是一張沒有說明的圖', () => {
    expect(SOURCE).toContain("t('common.actions.zoomAria'")
    expect(SOURCE).toContain("t('common.actions.zoom')")
  })

  it('排序／封存／刪除四顆鍵是這顆按鈕的兄弟，不在它裡面', async () => {
    // 這是票面點名的那條：包進去的話，◀▶📁× 按下去會一路冒泡成「開浮窗」。
    const html = await render({ character: CHARACTER })
    const zoomButton = sliceBetween(html, 'image-zoom-button', '</button>')

    expect(zoomButton).not.toContain('tile-btn')
    // 四顆鍵本身仍在格子上（計畫 §3.3：primitive 不開 actions slot）。
    expect(html).toContain('image-actions')
    expect(SOURCE).toContain('@click="move(index, -1)"')
    expect(SOURCE).toContain('@click="move(index, 1)"')
    expect(SOURCE).toContain('@click="handleArchive(url)"')
    expect(SOURCE).toContain('@click="handleDelete(url)"')
  })

  it('busyUrl 的忙碌狀態沒有被打亂', () => {
    // 四顆鍵各一條，一條都不能因為多包一層按鈕而掉。
    const matches = SOURCE.match(/busyUrl === url/g) ?? []
    expect(matches).toHaveLength(4)
  })
})

describe('候選圖：放大不得搶走「循環挑選」', () => {
  it('整格的主點擊仍然是 cycleCandidate', () => {
    // 這是挑圖流程的主要操作（採用／捨棄／進相簿三態循環）。
    expect(SOURCE).toContain('@click="cycleCandidate(url)"')
  })

  it('放大另有明確入口，而且把冒泡擋掉', () => {
    expect(SOURCE).toContain('@click.stop="openCandidateZoom(index)"')
    const zoomButton = sliceBetween(SOURCE, 'candidate-zoom-btn', '</button>')
    expect(zoomButton).not.toContain('cycleCandidate')
  })

  it('放大鍵不藏在 hover 後面（觸控裝置觸發不了 hover）', () => {
    const style = sliceBetween(SOURCE, '.candidate-zoom-btn {', '}')
    expect(style).not.toContain('opacity: 0;')
    expect(style).toContain('width: 44px')
  })
})

describe('兩處的集合是分開的', () => {
  it('舞台浮窗吃 image_urls，候選浮窗吃 candidateUrls', () => {
    // 混成一份會讓左右鍵從候選圖滑進舞台圖，等於謊報集合的邊界。
    expect(SOURCE).toContain(':items="stageLightboxItems"')
    expect(SOURCE).toContain(':items="candidateLightboxItems"')
    expect(SOURCE).toMatch(
      /stageLightboxItems = computed<LightboxItem\[\]>\(\(\) =>\s*\n\s*props\.character\.image_urls/,
    )
    expect(SOURCE).toMatch(
      /candidateLightboxItems = computed<LightboxItem\[\]>\(\(\) =>\s*\n\s*candidateUrls\.value/,
    )
  })

  it('兩個浮窗各有自己的索引', () => {
    expect(SOURCE).toContain('v-model:index="stageZoomIndex"')
    expect(SOURCE).toContain('v-model:index="candidateZoomIndex"')
  })
})

describe('Esc 在浮窗開著時不得抹掉整批候選圖', () => {
  // 這條錯了的代價是玩家不可復原地失去一批剛花點數生出來的候選圖，所以要釘
  // 兩面：面板這支監聽仍是 bubble 階段（會被浮窗的 capture 攔在前面），而且
  // 面板**不再**自己寫讓位守衛——攔截責任已經收回 `UiLightbox`。
  const LIGHTBOX_SOURCE = readFileSync(
    fileURLToPath(new URL('../src/components/ui/UiLightbox.vue', import.meta.url)),
    'utf-8',
  )

  it('面板的 keydown 是 bubble 階段，排在浮窗的 capture 之後', () => {
    expect(SOURCE).toContain("window.addEventListener('keydown', onKeydown)")
    // 若哪天有人把它改成 capture，浮窗就攔不住了——那正是這條要擋的回歸。
    expect(SOURCE).not.toMatch(/addEventListener\('keydown', onKeydown,\s*(true|\{)/)
  })

  it('浮窗以 capture 階段攔截，且對 Escape 停止傳播', () => {
    expect(LIGHTBOX_SOURCE).toContain('const KEYDOWN_OPTIONS = { capture: true } as const')
    expect(LIGHTBOX_SOURCE).toContain(
      "window.addEventListener('keydown', onKeydown, KEYDOWN_OPTIONS)",
    )
    expect(LIGHTBOX_SOURCE).toContain('event.stopPropagation()')
  })

  it('面板不再手寫讓位守衛——那套模式漏一個就是一次不可復原的破壞', () => {
    const body = sliceBetween(SOURCE, 'function onKeydown', 'function applyCandidateScrollLock')
    expect(body).not.toContain('candidateZoomOpen.value ||')
    expect(body).not.toContain('stageZoomOpen.value) return')
  })
})

describe('浮窗與面板自己的背景捲動鎖不打架', () => {
  it('候選 modal 收掉時把鎖狀態說兩次', () => {
    // UiLightbox 解鎖時還原的是「它上鎖那一刻的 overflow」——正是這個 modal 寫
    // 下的 hidden，而子元件的收尾一律排在本元件之後。只寫一次會留下一個沒有任何
    // modal 卻捲不動的頁面。
    expect(SOURCE).toContain('void nextTick(() => applyCandidateScrollLock(locked))')
    expect(SOURCE).toContain('void nextTick(() => applyCandidateScrollLock(false))')
  })
})

describe('syncZoomAfterUrlsChange：清單被換掉時浮窗要跟還是要關', () => {
  const A = 'https://media.example/objects/a.png'
  const B = 'https://media.example/objects/b.png'
  const C = 'https://media.example/objects/c.png'

  it('沒開窗就什麼都不動', () => {
    // 關著的浮窗沒有「正在看的那張」，去改索引只會讓下次開窗停在沒點過的位置。
    expect(syncZoomAfterUrlsChange({
      open: false, index: 1, before: [A, B], after: [A],
    })).toEqual({ outcome: 'stay' })
  })

  it('同位置還是同一張就停在原地', () => {
    expect(syncZoomAfterUrlsChange({
      open: true, index: 1, before: [A, B], after: [A, B],
    })).toEqual({ outcome: 'stay' })
  })

  it('排序把同一張換了位置，索引跟著走', () => {
    // ◀▶ 交換兩張圖：玩家在看的還是那張，只是位置變了。
    expect(syncZoomAfterUrlsChange({
      open: true, index: 0, before: [A, B], after: [B, A],
    })).toEqual({ outcome: 'move', index: 1 })
  })

  it('正在看的那張被刪除／封存掉就關窗', () => {
    // 留在同一個索引上，畫面會變成「同一格位置的另一張圖」，而玩家不會知道。
    expect(syncZoomAfterUrlsChange({
      open: true, index: 0, before: [A, B], after: [B],
    })).toEqual({ outcome: 'close' })
  })

  it('整份換人（換角色）就關窗', () => {
    expect(syncZoomAfterUrlsChange({
      open: true, index: 1, before: [A, B], after: [C],
    })).toEqual({ outcome: 'close' })
  })

  it('集合變空（候選圖被 commit／捨棄）就關窗', () => {
    expect(syncZoomAfterUrlsChange({
      open: true, index: 0, before: [A, B], after: [],
    })).toEqual({ outcome: 'close' })
  })

  it('推不出玩家在看哪一張時關窗，而不是猜一張', () => {
    expect(syncZoomAfterUrlsChange({
      open: true, index: 0, before: [], after: [A],
    })).toEqual({ outcome: 'close' })
    expect(syncZoomAfterUrlsChange({
      open: true, index: 0, before: null, after: [A],
    })).toEqual({ outcome: 'close' })
  })

  it('索引越界不丟例外，沿用浮窗的收斂方式', () => {
    // 索引來自上一次 render，而清單隨時可能被上游換掉——越界是正常狀態。
    expect(syncZoomAfterUrlsChange({
      open: true, index: 5, before: [A, B], after: [A, B],
    })).toEqual({ outcome: 'stay' })
    expect(syncZoomAfterUrlsChange({
      open: true, index: -1, before: [A, B], after: [B, A],
    })).toEqual({ outcome: 'move', index: 0 })
  })

  it('清單裡有重複 URL 時，原地那張才是玩家在看的那張', () => {
    expect(syncZoomAfterUrlsChange({
      open: true, index: 1, before: [A, A], after: [A, A],
    })).toEqual({ outcome: 'stay' })
  })
})
