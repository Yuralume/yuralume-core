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

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'

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

  it('still opens the original in a new tab', async () => {
    const html = await renderOneImage()
    expect(html).toContain(`href="${IMAGE_URL}"`)
    expect(html).toContain('rel="noopener"')
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
  })

  it('renders no picture markup at all when there is nothing attached', async () => {
    const html = await renderBubble([])
    expect(html).not.toContain('bubble-images')
    expect(html).toContain('你看，這是今天的天空。')
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
