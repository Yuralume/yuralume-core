import { afterEach, describe, expect, it } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import FusionStoryExitHub from '@/components/fusion-story/FusionStoryExitHub.vue'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { STUDIO_EXIT_HUB_COACHMARK_KEY } from '@/utils/arcDiscovery'

// No DOM test infra exists in this repo (@vue/test-utils / jsdom are not
// installed), so component coverage is done via SSR: renderToString runs
// setup() + the template, which is enough to assert the exit affordances,
// the celebration gate, and the coachmark gate all render as wired. Click
// → emit simulation needs a DOM; the emit handlers here are trivial
// `emit()` passthroughs and the util/coachmark logic they carry is
// unit-tested separately (fusionSeed.test.ts, arcDiscovery.test.ts).

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  })
}

async function render(props: Record<string, unknown> = {}): Promise<string> {
  const app = createSSRApp(FusionStoryExitHub, props)
  app.use(i18n())
  return renderToString(app)
}

const L = (zhTW as { fusionStory: { exitHub: Record<string, string> } })
  .fusionStory.exitHub

afterEach(() => {
  // Clean up any window stub a test installed.
  delete (globalThis as { window?: unknown }).window
})

describe('FusionStoryExitHub (SSR render)', () => {
  it('renders at least four exits on a finished story', async () => {
    const html = await render()
    // 1) primary CTA — adapt to arc
    expect(html).toContain(L.adapt)
    // 2) continue this story
    expect(html).toContain(L.continue)
    // 3) branch into another format
    expect(html).toContain(L.branch)
    // 4) export + share group
    expect(html).toContain(L.exportLabel)
    expect(html).toContain(zhTW.fusionStory.shareCard.openButton)
    expect(html).toContain(zhTW.fusionStory.viewer.exportFormats.markdown)
    expect(html).toContain(zhTW.fusionStory.viewer.exportFormats.epub)
  })

  it('hides the celebration banner by default', async () => {
    const html = await render()
    expect(html).not.toContain(L.celebrateTitle)
  })

  it('shows the celebration banner when celebrate=true', async () => {
    const html = await render({ celebrate: true })
    expect(html).toContain(L.celebrateTitle)
    expect(html).toContain(L.celebrateBody)
  })

  it('shows the loading label while adapting', async () => {
    const html = await render({ adaptingToArc: true })
    expect(html).toContain(L.adapting)
  })

  it('shows the one-shot coachmark when it has not been dismissed', async () => {
    const html = await render()
    expect(html).toContain(L.coachmark)
  })

  it('hides the coachmark once storage marks it dismissed', async () => {
    ;(globalThis as { window?: unknown }).window = {
      localStorage: {
        getItem: (key: string) =>
          key === STUDIO_EXIT_HUB_COACHMARK_KEY ? '1' : null,
        setItem: () => undefined,
      },
    }
    const html = await render()
    expect(html).not.toContain(L.coachmark)
    // The exits themselves still render — only the coachmark is gone.
    expect(html).toContain(L.adapt)
  })
})
