/**
 * Memoir 頭像不需要 1024x1536 原圖 (IV 系列漏網修復，ticket OC6-IV).
 *
 * Both spots (`MemoirPage.vue`, `MemoirOverlay.vue`) paint a 32-36px circular
 * avatar through a plain CSS `background-image` — not an `<img>`, so `UiImage`
 * cannot take it over directly. They reuse the same URL-derivation helper
 * `UiImage.vue` exports (`imageVariantUrl`, already re-exported from
 * `@/components/ui` — no new util needed) and ask for `w320`, the smallest
 * variant, instead of the character portrait original.
 *
 * `MemoirOverlay` takes `character` as a synchronous prop, so its rendered
 * markup is directly observable through this repo's SSR harness
 * (`createSSRApp` + `renderToString`; no jsdom / @vue/test-utils by choice —
 * see `uiImage.test.ts`).
 *
 * `MemoirPage` is different: it fetches the character from `onMounted`
 * (`loadCharacter` → `getCharacter`), and SSR never invokes `onMounted` — the
 * ref stays null no matter what is mocked, so no render of this page can ever
 * show the filled-in avatar under this harness. That is the same limitation
 * `offscreenImageRelease.test.ts` documents for `ChatPanel`; the convention
 * there is a source read pinning the wiring instead of an unreachable render
 * assertion, which is what the second `describe` below does.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import type { Character } from '@/types/character'

// MemoirContent drags in the memoir API, vue-router and useConfirmDialog
// (which wants ant-design-vue's Modal, which wants a document). None of that
// is under test here — only the overlay header's avatar is — so it is
// replaced with a probe, the same technique
// `citySearchInvalidationWiring.test.ts` uses for an unrelated child.
vi.mock('@/components/memoir/MemoirContent.vue', () => ({
  default: defineComponent({
    name: 'MemoirContentProbe',
    setup() {
      return () => h('div', { class: 'probe' })
    },
  }),
}))

const MemoirOverlay = (await import('@/components/MemoirOverlay.vue')).default

const PORTRAIT_URL = '/v1/public/characters/char-1/portrait.png'

const CHARACTER = {
  id: 'char-1',
  name: '芊璃',
  image_urls: [PORTRAIT_URL],
} as unknown as Character

const i18n = createI18n({
  legacy: false,
  locale: 'zh-TW',
  messages: { 'zh-TW': zhTW },
})

async function renderOverlay(character: Character | null): Promise<string> {
  const app = createSSRApp(MemoirOverlay, { open: true, character })
  app.use(i18n)
  return renderToString(app)
}

describe('memoir overlay avatar', () => {
  it('asks for the smallest variant instead of the 1024x1536 original', async () => {
    const html = await renderOverlay(CHARACTER)
    expect(html).toContain(`background-image:url(${PORTRAIT_URL}?v=w320)`)
    expect(html).not.toContain(`background-image:url(${PORTRAIT_URL})`)
  })

  it('renders no avatar span when the character has no portrait yet', async () => {
    const bare = { id: 'char-2', name: '無頭像', image_urls: [] } as unknown as Character
    const html = await renderOverlay(bare)
    expect(html).not.toContain('mo-avatar')
  })
})

// ----------------------------------------------------------------------
// MemoirPage — onMounted data, unreachable under SSR; read from source
// instead (see file header).
// ----------------------------------------------------------------------

function source(file: string): string {
  return readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
}

describe('memoir page avatar wiring (source read)', () => {
  const page = source('pages/MemoirPage.vue')

  it('derives the avatar URL through the shared variant helper, not the raw original', () => {
    expect(page).toContain("import { UiButton, imageVariantUrl } from '@/components/ui'")
    expect(page).toMatch(/imageVariantUrl\(original, 'w320'\)/)
  })

  it('still binds the background-image style to that computed URL', () => {
    expect(page).toContain('backgroundImage: `url(${portraitUrl})`')
  })
})
