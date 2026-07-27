import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import type { FusionStory } from '@/types/fusionStory'

/**
 * The display half of plan U4b: a background studio run that died for lack of
 * credits must show the shared top-up card, and every other failure must keep
 * the existing generic reason line.
 *
 * SSR-only, matching fusionStoryExitHub.test.ts — see that file for why.
 */
vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    cloudMode: ref(true),
    portalUrl: ref('https://app.yuralume.com'),
  }),
}))

vi.mock('@/composables/useCloudCredits', () => ({
  refreshCloudCreditsAfterAction: vi.fn(),
}))

vi.mock('@/utils/api/fusionStory', () => ({
  exportFusionStory: vi.fn(),
  iterateFusionBeat: vi.fn(),
  iterateFusionOutline: vi.fn(),
  polishFusionStory: vi.fn(),
  restoreFusionStoryVersion: vi.fn(),
}))

const FusionStoryViewer
  = (await import('@/components/fusion-story/FusionStoryViewer.vue')).default

const L = zhTW

function story(overrides: Partial<FusionStory>): FusionStory {
  return {
    id: 'story-1',
    character_ids: ['char-1'],
    prompt: 'p',
    title: 'Rainy bookshop',
    premise: 'They get stuck in the rain.',
    theme: 'friendship',
    status: 'failed',
    head_version: 1,
    full_text: '',
    error_message: 'upstream exploded',
    progress: { stage: 'writing', beats_total: 4, beats_done: 1, percent: 25 },
    beats: [],
    versions: [],
    created_at: '2026-07-26T00:00:00Z',
    updated_at: '2026-07-26T00:00:00Z',
    ...overrides,
  } as FusionStory
}

async function render(props: Record<string, unknown>): Promise<string> {
  const app = createSSRApp(FusionStoryViewer, props)
  app.use(createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  }))
  return renderToString(app)
}

describe('FusionStoryViewer failure surface', () => {
  it('swaps in the top-up card when the run ran out of credits', async () => {
    const html = await render({
      story: story({ error_code: 'insufficient_credits' }),
      characters: [],
    })

    expect(html).toContain(L.credits.insufficient.title)
    expect(html).toContain(L.credits.insufficient.body)
    expect(html).toContain('https://app.yuralume.com/credits')
    // The raw upstream message must not leak alongside the promise.
    expect(html).not.toContain('upstream exploded')
  })

  it('keeps the generic reason line for an ordinary crash', async () => {
    const html = await render({
      story: story({ error_code: null }),
      characters: [],
    })

    expect(html).toContain('upstream exploded')
    expect(html).not.toContain(L.credits.insufficient.title)
  })

  it('treats an unrecognised code as an ordinary failure', async () => {
    const html = await render({
      story: story({ error_code: 'model_quarantined' }),
      characters: [],
    })

    expect(html).toContain('upstream exploded')
    expect(html).not.toContain(L.credits.insufficient.title)
  })

  it('ignores a stale credit code while the job is running again', async () => {
    const html = await render({
      story: story({
        status: 'writing',
        error_code: 'insufficient_credits',
        error_message: null,
      }),
      characters: [],
    })

    expect(html).not.toContain(L.credits.insufficient.title)
  })
})
