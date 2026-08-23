/**
 * BD7 — the branching-drama ending's exits (plus the BD9 gallery entrance).
 *
 * Same constraint as `fusionStoryExitHub.test.ts`: no DOM test infra exists
 * in this repo, so the component is covered by SSR render (setup + template)
 * and the *effects* of its emits are pinned where they actually land — the
 * conversion request on the wire, and the pure transcript composer
 * (`dramaTranscript.test.ts`).
 */
// First: the replay CTA now carries a price chip, and `ActionPriceHint` pulls
// in `useAuth`, which reads `localStorage` at module scope.
import './fixtures/browserGlobals'

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import DramaExitHub from '@/components/branching-drama/DramaExitHub.vue'
import { adaptDramaSessionToArc } from '@/utils/api/branchingDrama'
import { authedFetch } from '@/utils/authedFetch'
import {
  DRAMA_OPERATOR_POSITIONS,
  DRAMA_POSITION_TO_ARC_MODE,
} from '@/types/branchingDrama'
import { FUSION_TO_ARC_OPERATOR_MODES } from '@/types/fusionStory'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

vi.mock('@/utils/authedFetch', () => ({ authedFetch: vi.fn() }))

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  })
}

async function render(props: Record<string, unknown> = {}): Promise<string> {
  const app = createSSRApp(DramaExitHub, { dramaTitle: 'Glass Signal', ...props })
  app.use(i18n())
  return renderToString(app)
}

const L = zhTW.branchingDrama.exitHub

describe('DramaExitHub (SSR render)', () => {
  it('offers every exit out of a finished playthrough', async () => {
    const html = await render()
    // 1) walk it again, differently
    expect(html).toContain(L.replay)
    // 2) keep it as a script the characters can live through
    expect(html).toContain(L.adapt)
    // 3) take the text away
    expect(html).toContain(L.exportLabel)
    expect(html).toContain(L.exportFormats.txt)
    // 4) look at what this run collected (BD9) — the ending is where the
    //    gallery means the most, and it used to be reachable only from the
    //    drama's own page.
    expect(html).toContain(L.gallery)
    // …and the button says where it takes them, because it leaves the VN.
    expect(html).toContain(L.galleryHint)
    // and the way back, which is all the ending used to offer
    expect(html).toContain(zhTW.branchingDrama.player.backToList)
  })

  it('asks how the player enters the arc, rather than deciding for them', async () => {
    const html = await render()
    expect(html).toContain(L.playerModeLabel)
    expect(html).toContain(L.playerModeWriteIn)
    expect(html).toContain(L.playerModeObserver)
    expect(html).toContain(L.playerModeUnchanged)
    // ARIA radiogroup, so a screen reader announces it as one choice.
    expect(html).toContain('role="radiogroup"')
    expect((html.match(/role="radio"/g) ?? []).length).toBe(
      FUSION_TO_ARC_OPERATOR_MODES.length,
    )
  })

  it('pre-selects the mode the drama was actually played under', async () => {
    // 旁觀者 played → 「保持原樣」 pre-checked, and its hint is the one shown.
    const html = await render({
      defaultMode: DRAMA_POSITION_TO_ARC_MODE.absent,
    })

    expect(html).toContain(L.playerModeUnchangedHint)
    expect(html).not.toContain(L.playerModeWriteInHint)
    // Exactly one chip is checked — a radiogroup with two is a broken one.
    expect((html.match(/aria-checked="true"/g) ?? []).length).toBe(1)
  })

  it('locks the mode chips and the CTA while a conversion is in flight', async () => {
    const html = await render({ adaptingToArc: true })

    expect(html).toContain(L.adapting)
    expect((html.match(/disabled/g) ?? []).length).toBeGreaterThanOrEqual(
      FUSION_TO_ARC_OPERATOR_MODES.length,
    )
  })
})

describe('drama position → conversion mode', () => {
  it('maps every position to a real mode', () => {
    for (const position of DRAMA_OPERATOR_POSITIONS) {
      expect(FUSION_TO_ARC_OPERATOR_MODES).toContain(
        DRAMA_POSITION_TO_ARC_MODE[position],
      )
    }
    // 主演 → 把我寫進戲裡; 旁觀者 → 保持原樣. A player who was never in the
    // drama must not be written into the arc it becomes (紅線 5).
    expect(DRAMA_POSITION_TO_ARC_MODE.central).toBe('write_in')
    expect(DRAMA_POSITION_TO_ARC_MODE.present).toBe('observer')
    expect(DRAMA_POSITION_TO_ARC_MODE.absent).toBe('unchanged')
  })
})

describe('adaptDramaSessionToArc transport', () => {
  it('posts the chosen mode to the session it came from', async () => {
    mockedAuthedFetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'draft-1' }),
    } as unknown as Response)

    await adaptDramaSessionToArc('drama-1', 'sess-1', {
      operator_mode: 'observer',
    })

    const [url, init] = mockedAuthedFetch.mock.calls[0]
    expect(url).toBe(
      '/api/v1/branching-dramas/drama-1/sessions/sess-1/adapt-to-arc',
    )
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      operator_mode: 'observer',
    })
  })

  it('sends no mode at all when nobody chose one', async () => {
    mockedAuthedFetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'draft-1' }),
    } as unknown as Response)

    await adaptDramaSessionToArc('drama-1', 'sess-1')

    const [, init] = mockedAuthedFetch.mock.calls[0]
    // An empty body is a caller who never saw the chips; the server answers
    // that with how the drama was played, not with a blanket default.
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({})
  })
})

describe('exit-hub copy ships in every locale', () => {
  const catalogues = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP }

  it('has every key the ending renders', () => {
    const keys = [
      'heading', 'replay', 'replayHint', 'adapt', 'adapting', 'adaptSaved',
      'playerModeLabel', 'playerModeWriteIn', 'playerModeObserver',
      'playerModeUnchanged', 'playerModeWriteInHint',
      'playerModeObserverHint', 'playerModeUnchangedHint',
      'exportLabel', 'exporting', 'gallery', 'galleryHint', 'beatFallback',
    ]
    for (const [locale, catalogue] of Object.entries(catalogues)) {
      const hub = catalogue.branchingDrama.exitHub as unknown as
        Record<string, unknown>
      for (const key of keys) {
        expect(hub[key], `${locale}.${key}`).toBeTruthy()
      }
      const formats = hub.exportFormats as Record<string, string>
      expect(formats.txt, `${locale}.exportFormats.txt`).toBeTruthy()
      expect(formats.md, `${locale}.exportFormats.md`).toBeTruthy()
    }
  })
})
