/**
 * What a chat picture is actually asked for (ticket IV5-C, plan D5/D6).
 *
 * The measured problem: `.bubble-image` displays ~303x360 CSS px and was
 * loading the 1024x1536 original — 17x the pixels, 2313 KB on the wire, 6.0 MB
 * of decoded bitmap held for as long as the message stays in the thread. This
 * renders the bubble and reads the attributes that decide all three.
 *
 * SSR (`createSSRApp` + `renderToString`) per the repo convention. That means
 * the *initial* markup is observable and nothing else: no layout, no
 * `IntersectionObserver`, so the release itself is manual acceptance (see
 * `offscreenImageRelease.test.ts`). What is provable here is that a released
 * image would have a box to fall back on, because the box is in the markup.
 */

import { readFileSync } from 'node:fs'

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { chatImageLightboxItems } from '@/utils/chatImageLightbox'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({ isAdmin: ref(false) }),
}))

vi.mock('@/composables/useCloudCredits', () => ({
  refreshCloudCreditsAfterAction: vi.fn(),
}))

vi.mock('@/utils/api/observability', () => ({
  updateTurnOperatorFeedback: vi.fn(),
}))

vi.mock('@/utils/api/tts', () => ({
  synthesizeCharacterTTS: vi.fn(),
  TTSDisabledError: class TTSDisabledError extends Error {},
}))

const ChatBubble = (await import('@/components/ChatBubble.vue')).default

const IMAGE_URL = '/v1/public/characters/abc/moment.png'

const i18n = createI18n({
  legacy: false,
  locale: 'zh-TW',
  messages: { 'zh-TW': zhTW },
})

async function renderBubble(
  attachments: Array<Record<string, unknown>>,
): Promise<string> {
  const app = createSSRApp(ChatBubble, {
    message: {
      role: 'assistant',
      content: '你看，這是今天的天空。',
      attachments,
    },
    characterId: 'char-1',
  })
  app.use(i18n)
  return renderToString(app)
}

function renderOneImage(): Promise<string> {
  return renderBubble([
    { kind: 'image', url: IMAGE_URL, mime_type: 'image/png', caption: null },
  ])
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('a picture in a chat bubble', () => {
  it('offers the two variant widths instead of the original', async () => {
    const html = await renderOneImage()
    expect(html).toContain(`${IMAGE_URL}?v=w320 320w`)
    expect(html).toContain(`${IMAGE_URL}?v=w768 768w`)
    // The `src` is the fallback and the resolution base — the cheap one.
    expect(html).toContain(`src="${IMAGE_URL}?v=w320"`)
  })

  it('states the width it paints at, so 100vw is never assumed', async () => {
    // Without `sizes` the browser assumes the full viewport and takes w768 for
    // a picture that paints 240px wide.
    expect(await renderOneImage()).toMatch(/sizes="[^"]*vw[^"]*"/)
    expect(await renderOneImage()).toContain('320px')
  })

  it('decodes off the main thread and waits until it is near', async () => {
    const html = await renderOneImage()
    expect(html).toContain('decoding="async"')
    expect(html).toContain('loading="lazy"')
  })

  it('reserves a box before anything has loaded', async () => {
    // The precondition for release: with no reserved box, dropping `src`
    // collapses the row and the thread jumps under the reader.
    expect(await renderOneImage()).toContain('aspect-ratio')
  })

  it('keeps the class the panel styles it through', async () => {
    // `.bubble-image` carries max-height, the border and the radius. A wrapper
    // or a renamed class silently unstyles every picture in the product.
    expect(await renderOneImage()).toContain('bubble-image')
  })

  it('carries the caption as alt text, and a default when there is none', async () => {
    const captioned = await renderBubble([
      { kind: 'image', url: IMAGE_URL, mime_type: 'image/png', caption: '傍晚的天空' },
    ])
    expect(captioned).toContain('alt="傍晚的天空"')
    expect(await renderOneImage()).toMatch(/alt="[^"]+"/)
  })

  it('leaves non-image attachments on the file path', async () => {
    const html = await renderBubble([
      { kind: 'file', url: '/v1/public/a.txt', mime_type: 'text/plain', caption: null },
    ])
    expect(html).toContain('bubble-file')
    expect(html).not.toContain('bubble-image')
    // A file attachment is a download, not something to look at: it keeps the
    // new tab the pictures just gave up (LB3).
    expect(html).toContain('href="/v1/public/a.txt"')
    expect(html).toContain('target="_blank"')
  })

  it('renders no picture markup at all when there is nothing attached', async () => {
    const html = await renderBubble([])
    expect(html).not.toContain('bubble-images')
    expect(html).toContain('你看，這是今天的天空。')
  })
})

// ----------------------------------------------------------------------
// LB3 — the picture opens the lightbox instead of a new tab
// ----------------------------------------------------------------------

const bubbleSource = readFileSync(
  new URL('../src/components/ChatBubble.vue', import.meta.url),
  'utf8',
)

describe('a chat picture opens the lightbox', () => {
  it('is a button, not a link to somewhere else', async () => {
    const html = await renderOneImage()
    expect(html).toContain('class="bubble-image-link"')
    expect(html).toContain('type="button"')
    // The old path opened the file itself in a new tab; "open the original"
    // now lives inside the lightbox (and goes through `safeMediaHref()` there,
    // which this anchor never did).
    expect(html).not.toContain(`href="${IMAGE_URL}"`)
  })

  it('renders no overlay markup while it is closed', async () => {
    // The point of `v-if="visible"`: a closed lightbox holds no element, so it
    // holds no decoded bitmap either.
    expect(await renderOneImage()).not.toContain('ui-lightbox')
  })
})

describe('what reaches the lightbox is the original URL', () => {
  // This is the trap of the ticket. The thumbnail's own `src` goes through
  // `imageSrcFor()`, which returns '' once the strip scrolls out of view
  // (IV5-C release). Feeding that display value to the lightbox would make a
  // picture scrolled far enough away open blank — and the SSR harness has no
  // `IntersectionObserver`, so a release never happens here and every other
  // assertion in this file would stay green.

  it('maps attachments to their untouched url and caption', () => {
    expect(chatImageLightboxItems([
      { kind: 'image', url: IMAGE_URL, mime_type: 'image/png', caption: '傍晚的天空' },
    ])).toEqual([{ url: IMAGE_URL, caption: '傍晚的天空' }])
  })

  it('keeps only the pictures, in the order the thumbnails are in', () => {
    // The thumbnail `v-for` runs over the image attachments, so the index the
    // click hands over only means anything if this filter matches it.
    expect(chatImageLightboxItems([
      { kind: 'image', url: '/a.png', mime_type: 'image/png', caption: null },
      { kind: 'file', url: '/b.txt', mime_type: 'text/plain', caption: null },
      { kind: 'image', url: '/c.png', mime_type: 'image/png', caption: null },
    ]).map(item => item.url)).toEqual(['/a.png', '/c.png'])
  })

  it('survives a message with no attachments at all', () => {
    expect(chatImageLightboxItems(undefined)).toEqual([])
    expect(chatImageLightboxItems([])).toEqual([])
  })

  it('is what the bubble actually hands the lightbox', () => {
    // Read from source: the collection is built from the attachments, never
    // from the released display src.
    // `\r?` throughout: the checkout is CRLF on Windows and a bare `\n` in the
    // pattern silently matches nothing, turning this gate green-by-absence.
    const itemsComputed = bubbleSource.match(
      /const lightboxItems = computed[\s\S]*?\r?\n\)/,
    )?.[0] ?? ''
    expect(itemsComputed).not.toBe('')
    expect(itemsComputed).toContain('chatImageLightboxItems(imageAttachments.value)')
    expect(itemsComputed).not.toContain('imageSrcFor')
    expect(bubbleSource).toContain(':items="lightboxItems"')
  })

  it('opens at the picture that was clicked', () => {
    expect(bubbleSource).toContain('@click="openZoom(imageIdx)"')
    expect(bubbleSource).toContain('v-model:index="zoomIndex"')
  })

  it('closes the overlay when the attachment list is replaced', () => {
    // The index points into the previous list; leaving it open turns into
    // "the same slot, a different picture" the moment a new one lands.
    expect(bubbleSource).toMatch(
      /watch\(\s*\(\) => imageAttachments\.value\.map[\s\S]{0,200}zoomOpen\.value = false/,
    )
  })
})

describe('the root element the panel addresses', () => {
  it('is still `.bubble`', async () => {
    // `ChatPanel` puts `content-visibility: auto` on
    // `.messages-container > .bubble`, which works because Vue stamps the
    // parent's scope id onto a child component's root. Rename this class and
    // that rule silently stops matching: no error, no test failure anywhere
    // else, and every off-screen message quietly goes back into layout.
    expect(await renderBubble([])).toMatch(/^<div class="bubble assistant"/)
  })
})
