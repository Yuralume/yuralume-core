/**
 * LB4：LumeGram 貼文圖從「開新分頁」改成點圖開浮窗（`UiLightbox`）。
 *
 * 測試 harness 是 SSR（`createSSRApp` + `renderToString`，沒有 jsdom），所以
 * 點擊、浮窗實際開合、←→ 鍵、swipe 都測不到——這些留給 `lightbox.ts` 的純
 * 函式閘（`UiLightbox.vue` 檔頭已說明分工）。這裡能釘住的是初始 SSR
 * markup 的兩件事：
 *
 * 1. 點擊目標存在，且**不再是**「開新分頁的 `<a>`」——那是 LB4 要換掉的
 *    行為，換錯回去（例如漏刪 `target="_blank"`）不會有任何其他測試變紅。
 * 2. 影片分支（`post.video_url`）完全不受影響——兩條路徑在同一則貼文上
 *    互斥，圖片分支的改動不該波及它。
 */

import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import type { FeedPost } from '@/types/feed'

// `useAuth()` reads `localStorage` at module scope (no `typeof window`
// guard) — safe in the browser, but this test runs under Node with no
// DOM. Mirrors the mock in `chatBubbleImage.test.ts`.
vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({ currentUser: ref(null) }),
}))

const FeedCard = (await import('@/components/FeedCard.vue')).default

const i18n = createI18n({
  legacy: false,
  locale: 'zh-TW',
  messages: { 'zh-TW': zhTW },
})

const IMAGE_URL = '/v1/public/characters/char-1/feed/moment.png'
const VIDEO_URL = '/v1/public/characters/char-1/feed/moment.mp4'

function basePost(overrides: Partial<FeedPost> = {}): FeedPost {
  return {
    id: 'post-1',
    character_id: 'char-1',
    kind: 'daily',
    content_text: '今天天氣真好，出門走了一圈。',
    source: { kind: 'schedule', ref_id: null },
    image_url: null,
    image_prompt: null,
    video_url: null,
    video_prompt: null,
    reactions: { likes: 0, comments: 0 },
    reactions_seen_at: null,
    viewed_at: null,
    created_at: new Date().toISOString(),
    liked: false,
    ...overrides,
  }
}

async function renderCard(post: FeedPost): Promise<string> {
  const app = createSSRApp(FeedCard, { post })
  app.use(i18n)
  return renderToString(app)
}

describe('a LumeGram post picture', () => {
  it('is a button, not a link that opens a new tab', async () => {
    const html = await renderCard(basePost({ image_url: IMAGE_URL }))
    expect(html).toContain('feed-card-image-button')
    expect(html).toMatch(/<button[^>]*class="feed-card-image-button"/)
    // The old `<a target="_blank">` escape hatch must be gone entirely —
    // leaving it behind would silently reopen the tab-per-image failure
    // mode LB4 exists to close.
    expect(html).not.toContain('target="_blank"')
    expect(html).not.toContain('feed-card-image-link')
  })

  it('gives the trigger button an accessible name', async () => {
    const html = await renderCard(basePost({ image_url: IMAGE_URL }))
    expect(html).toMatch(/<button[^>]*class="feed-card-image-button"[^>]*aria-label="[^"]+"/)
  })

  it('still renders the picture through UiImage inside the button', async () => {
    const html = await renderCard(basePost({ image_url: IMAGE_URL }))
    expect(html).toContain(`${IMAGE_URL}?v=w320`)
  })

  it('renders no image button or lightbox markup when the post has no image', async () => {
    const html = await renderCard(basePost())
    expect(html).not.toContain('feed-card-image-button')
    expect(html).not.toContain('ui-lightbox')
  })
})

describe('the video branch on the same post type', () => {
  it('is untouched by the image-lightbox change', async () => {
    const html = await renderCard(basePost({ video_url: VIDEO_URL }))
    expect(html).toContain('feed-card-video')
    expect(html).toContain(`src="${VIDEO_URL}"`)
    expect(html).toContain('controls')
  })

  it('takes priority over the image field and never renders the zoom button', async () => {
    const html = await renderCard(
      basePost({ video_url: VIDEO_URL, image_url: IMAGE_URL }),
    )
    expect(html).toContain('feed-card-video')
    expect(html).not.toContain('feed-card-image-button')
  })
})
