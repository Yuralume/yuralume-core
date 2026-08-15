import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useActionPricing } from '@/composables/useActionPricing'
import { authedFetch } from '@/utils/authedFetch'
import { fetchCloudPricing } from '@/utils/api/cloudPricing'
import {
  StorySceneError,
  endStoryScene,
  getStoryScene,
  openStoryScene,
} from '@/utils/api/storyScene'
import {
  STORY_SCENE_DAILY_LIMIT_KEY,
  isStaleSceneState,
  storySceneErrorKey,
} from '@/utils/storySceneErrors'
import { InsufficientCreditsError } from '@/utils/api/insufficientCredits'
import { PriceChangedError } from '@/utils/api/priceChanged'
import { isSceneNarration, sceneHeadingIndex } from '@/utils/sceneMessages'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

/**
 * The story-scene transport (plan SC, ticket SC2) against the SC1-A contract.
 *
 * Two things are pinned here that no rendering test can reach: that every
 * refusal keeps its structured `code` all the way to the display boundary,
 * and that the state read answers `null` — the shape the backend uses to say
 * "no scene", which a client that assumed an object would turn into a crash
 * on the happy path.
 */
vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))
vi.mock('@/utils/api/cloudPricing', () => ({ fetchCloudPricing: vi.fn() }))

const mockedAuthedFetch = vi.mocked(authedFetch)
const mockedPricing = vi.mocked(fetchCloudPricing)

/** Publish a 起幕 price the way the hosted proxy would (SC3-C). */
async function seedScenePrice(priceCr: number): Promise<void> {
  mockedPricing.mockResolvedValueOnce({
    kind: 'ok',
    snapshot: {
      stale: false,
      tiers: [{
        tier_name: 'standard',
        billing_shape: 'action_fixed',
        actions: [{
          action_key: 'story_scene_open',
          unit: 'per_scene',
          price_cr: priceCr,
          overage: false,
        }],
      }],
    },
  })
  await useActionPricing().ensureLoaded()
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function refusal(status: number, code: string): Response {
  return jsonResponse(status, { detail: { code, message: `${code} (server)` } })
}

const SESSION = {
  id: 'scene-1',
  character_id: 'char-1',
  conversation_id: 'conv-1',
  status: 'open',
  source_layer: 'beat',
  arc_id: 'arc-1',
  beat_id: 'beat-1',
  title: '頂樓的獨奏',
  location: '頂樓天台',
  mood: '欲言又止',
  scene_type: null,
  dramatic_question: null,
  opened_at: '2026-08-01T00:00:00Z',
  last_activity_at: '2026-08-01T00:00:00Z',
  closed_at: null,
  closed_reason: null,
}

beforeEach(() => {
  vi.clearAllMocks()
  useActionPricing().reset()
})

describe('openStoryScene', () => {
  it('posts with no body when nothing is quotable', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(201, {
      session: SESSION,
      narration: {
        role: 'assistant',
        content: 'The lift doors open onto the roof.',
        attachments: [],
        turn_record_id: null,
        kind: 'scene_narration',
      },
      character_message: {
        role: 'assistant',
        content: 'You came.',
        attachments: [],
        turn_record_id: null,
        kind: 'chat',
      },
      suggested_actions: [{ text: 'Sit down beside her' }],
    }))

    const response = await openStoryScene('char-1')

    const [url, init] = mockedAuthedFetch.mock.calls[0]
    expect(url).toBe('/api/v1/characters/char-1/story-scene')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBeUndefined()
    expect(response.session.title).toBe('頂樓的獨奏')
    expect(response.narration.kind).toBe('scene_narration')
    expect(response.suggested_actions).toHaveLength(1)
  })

  it('carries the price this screen was quoting', async () => {
    // R-9: the charge binds to the number the player saw, not to whatever
    // the server's own cache happens to hold when the request lands.
    await seedScenePrice(6.5)
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(201, {
      session: SESSION,
      narration: { role: 'assistant', content: 'x', kind: 'scene_narration' },
      character_message: { role: 'assistant', content: 'y', kind: 'chat' },
      suggested_actions: [],
    }))

    await openStoryScene('char-1')

    const init = mockedAuthedFetch.mock.calls[0][1]
    expect(JSON.parse(String(init?.body))).toEqual({ quoted_price_cr: 6.5 })
    expect(
      (init?.headers as Record<string, string>)['Content-Type'],
    ).toBe('application/json')
  })

  it('keeps the structured code of every documented refusal', async () => {
    const cases: Array<[number, string]> = [
      [409, 'scene_in_progress'],
      [409, 'no_material'],
      [409, 'conversation_busy'],
      [502, 'scene_open_failed'],
      // The quota guard's own two, which the route deliberately gave
      // different statuses: 429 "out for today" vs 503 "could not check".
      [429, 'story_scene_daily_limit_reached'],
      [503, 'story_scene_quota_unavailable'],
    ]

    for (const [status, code] of cases) {
      mockedAuthedFetch.mockResolvedValueOnce(refusal(status, code))

      const error = await openStoryScene('char-1').catch(err => err)

      expect(error).toBeInstanceOf(StorySceneError)
      expect(error).toMatchObject({ code, statusCode: status })
    }
  })

  it('types the billing refusals behind this button', async () => {
    // The money errors have exactly one shape across the SPA, so the panel
    // can answer them with the shared top-up card and the price refresh
    // rather than with a scene error line the player cannot act on.
    mockedAuthedFetch.mockResolvedValueOnce(
      refusal(402, 'insufficient_credits'),
    )
    await expect(openStoryScene('char-1')).rejects
      .toBeInstanceOf(InsufficientCreditsError)

    mockedAuthedFetch.mockResolvedValueOnce(refusal(409, 'price_changed'))
    await expect(openStoryScene('char-1')).rejects
      .toBeInstanceOf(PriceChangedError)
  })

  it('falls back to a plain error when the body carries no code', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(500, {
      detail: 'internal server error',
    }))

    const error = await openStoryScene('char-1').catch(err => err)

    expect(error).not.toBeInstanceOf(StorySceneError)
    expect(error.message).toBe('internal server error')
  })
})

describe('getStoryScene', () => {
  it('reads a running scene', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, SESSION))

    const session = await getStoryScene('char-1')

    expect(session?.status).toBe('open')
    expect(mockedAuthedFetch.mock.calls[0][0])
      .toBe('/api/v1/characters/char-1/story-scene')
  })

  it('accepts a bare null as "no scene"', async () => {
    // The route answers `200 null`, not `204` and not `{}`. A client that
    // assumed an object would throw on the most common state there is.
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, null))

    await expect(getStoryScene('char-1')).resolves.toBeNull()
  })
})

describe('endStoryScene', () => {
  it('names the session it means to close', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      session: { ...SESSION, status: 'closed', closed_reason: 'manual' },
      closing_narration: {
        role: 'assistant',
        content: 'The roof empties out.',
        attachments: [],
        turn_record_id: null,
        kind: 'scene_narration',
      },
    }))

    const response = await endStoryScene('char-1', 'scene-1')

    const [url, init] = mockedAuthedFetch.mock.calls[0]
    expect(url).toBe('/api/v1/characters/char-1/story-scene/end')
    expect(JSON.parse(String(init?.body))).toEqual({ session_id: 'scene-1' })
    expect(response.session.closed_reason).toBe('manual')
    expect(response.closing_narration?.kind).toBe('scene_narration')
  })

  it('accepts a close with no send-off written', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      session: { ...SESSION, status: 'closed', closed_reason: 'resolved' },
      closing_narration: null,
    }))

    await expect(endStoryScene('char-1', 'scene-1')).resolves
      .toMatchObject({ closing_narration: null })
  })

  it('carries the interim 501 through as its own code', async () => {
    // SC1-D has not landed yet; until it does the button must fail as
    // "not yet", never as "something broke".
    mockedAuthedFetch.mockResolvedValueOnce(
      refusal(501, 'scene_end_not_implemented'),
    )

    const error = await endStoryScene('char-1', 'scene-1').catch(err => err)

    expect(error).toMatchObject({
      code: 'scene_end_not_implemented',
      statusCode: 501,
    })
  })
})

describe('storySceneErrorKey', () => {
  const CATALOGS = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP }

  it('maps every contract code to its own catalog key', () => {
    const expected: Array<[string, string]> = [
      ['scene_in_progress', 'inProgress'],
      ['no_material', 'noMaterial'],
      ['conversation_busy', 'conversationBusy'],
      ['scene_open_failed', 'openFailed'],
      ['scene_end_not_implemented', 'endNotAvailable'],
      // The two the backend split apart on purpose: "that scene already
      // ended" is a different sentence from "you are not in a scene", and
      // collapsing both into `endGeneric` would have told the player to try
      // again later at the exact moment there was nothing left to try.
      ['no_open_scene', 'endAlreadyEnded'],
      ['scene_session_mismatch', 'endSceneChanged'],
      // SC3-B's two quota refusals. Unmapped they landed on `generic`
      // ("please try again later"), which is wrong twice over: retrying is
      // futile until tomorrow, and the reason — an allowance, not a
      // malfunction — was hidden from the one player it is about.
      ['story_scene_daily_limit_reached', 'dailyLimitReached'],
      ['story_scene_quota_unavailable', 'quotaUnavailable'],
    ]

    for (const [code, suffix] of expected) {
      const error = new StorySceneError({ code, statusCode: 409 })
      expect(storySceneErrorKey(error)).toBe(`chat.storyScene.errors.${suffix}`)
    }
  })

  it('falls back per entry point rather than to one vague line', () => {
    const unknown = new StorySceneError({ code: 'who_knows', statusCode: 500 })

    expect(storySceneErrorKey(unknown)).toBe('chat.storyScene.errors.generic')
    expect(storySceneErrorKey(unknown, 'endGeneric'))
      .toBe('chat.storyScene.errors.endGeneric')
    expect(storySceneErrorKey(new Error('offline'), 'endGeneric'))
      .toBe('chat.storyScene.errors.endGeneric')
  })

  it('resolves in all three languages, with no key left unwritten', () => {
    const keys = [
      'inProgress', 'noMaterial', 'conversationBusy',
      'openFailed', 'endNotAvailable', 'endAlreadyEnded', 'endSceneChanged',
      'dailyLimitReached', 'quotaUnavailable',
      'generic', 'endGeneric',
    ] as const

    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const errors = catalog.chat.storyScene.errors as Record<string, string>
      for (const key of keys) {
        expect(errors[key], `${locale}.${key}`).toBeTruthy()
      }
    }
  })

  it('names the "out for today" key so the panel can react to it', () => {
    // The state machine swallows the error object, so this key is the only
    // handle the panel has on "the count on screen is now stale".
    const error = new StorySceneError({
      code: 'story_scene_daily_limit_reached',
      statusCode: 429,
    })

    expect(storySceneErrorKey(error)).toBe(STORY_SCENE_DAILY_LIMIT_KEY)
    expect(STORY_SCENE_DAILY_LIMIT_KEY)
      .toBe('chat.storyScene.errors.dailyLimitReached')
  })

  it('keeps the two quota refusals apart in every language', () => {
    // One says "come back tomorrow", the other says "try again in a
    // moment". Sharing a sentence would send the player to wait a day
    // over a transient read failure, or to keep pressing a button that
    // cannot work until the window clears.
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const errors = catalog.chat.storyScene.errors
      expect(errors.dailyLimitReached, locale)
        .not.toBe(errors.quotaUnavailable)
      expect(errors.dailyLimitReached, locale).not.toBe(errors.generic)
      expect(errors.quotaUnavailable, locale).not.toBe(errors.generic)
    }
  })

  it('says nothing was spent on either quota refusal', () => {
    // The button carries a price (SC3-C). A refusal that leaves that
    // unsaid reads as "you paid for a scene you did not get".
    const spentPromise: Record<string, readonly string[]> = {
      'zh-TW': ['沒有扣點'],
      'en-US': ['Nothing was spent', 'nothing was spent'],
      'ja-JP': ['消費されていません'],
    }

    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const errors = catalog.chat.storyScene.errors
      for (const copy of [errors.dailyLimitReached, errors.quotaUnavailable]) {
        expect(
          spentPromise[locale].some(phrase => copy.includes(phrase)),
          `${locale}: ${copy}`,
        ).toBe(true)
      }
    }
  })

  it('does not treat a quota refusal as a stale view of the scene', () => {
    // Re-reading the session answers nothing here — the server is not
    // disagreeing about which scene is running, it is counting.
    for (const code of [
      'story_scene_daily_limit_reached', 'story_scene_quota_unavailable',
    ]) {
      expect(
        isStaleSceneState(new StorySceneError({ code, statusCode: 429 })),
        code,
      ).toBe(false)
    }
  })

  it('groups the three refusals that mean "your view is out of date"', () => {
    // M7: each of these says the client asked about a scene the server does
    // not agree is running, and the only useful answer to all three is to go
    // and re-read the state rather than to explain harder.
    for (const code of [
      'scene_in_progress', 'no_open_scene', 'scene_session_mismatch',
    ]) {
      expect(
        isStaleSceneState(new StorySceneError({ code, statusCode: 409 })),
        code,
      ).toBe(true)
    }

    // A refusal about the material or the transport says nothing about which
    // scene is running: re-reading would buy a request per failure and change
    // nothing.
    for (const error of [
      new StorySceneError({ code: 'no_material', statusCode: 409 }),
      new StorySceneError({ code: 'scene_end_not_implemented', statusCode: 501 }),
      new Error('offline'),
    ]) {
      expect(isStaleSceneState(error)).toBe(false)
    }
  })
})

describe('message kind compatibility', () => {
  it('treats a message with no kind as ordinary chat', () => {
    // Every message stored before SC1-A has no `kind`. Reading narration as
    // "not chat" would reformat whole back catalogues of conversation.
    expect(isSceneNarration({ kind: undefined })).toBe(false)
    expect(isSceneNarration({ kind: null })).toBe(false)
    expect(isSceneNarration({ kind: 'chat' })).toBe(false)
    expect(isSceneNarration({ kind: 'scene_narration' })).toBe(true)
    expect(isSceneNarration(undefined)).toBe(false)
  })

  it('reads a bare tool artifact as chat, not as narration', () => {
    // L9: the backend emits `tool_only` verbatim for an assistant row whose
    // whole payload is a picture. It is a context-gating marker on the
    // server side and has never been a scene frame here — which is exactly
    // what the positive test on `scene_narration` preserves now that the
    // value is spellable in the union.
    expect(isSceneNarration({ kind: 'tool_only' })).toBe(false)
    expect(sceneHeadingIndex(
      [{ kind: 'tool_only' as const }],
      { hasSession: true },
    )).toBeNull()
  })

  it('puts the heading on the narration that opened the scene', () => {
    const thread = [
      { kind: 'chat' as const },
      { kind: 'scene_narration' as const },
      { kind: 'chat' as const },
    ]

    expect(sceneHeadingIndex(thread, { hasSession: true })).toBe(1)
  })

  it('leaves the heading in place when a send-off is appended below it', () => {
    const thread = [
      { kind: 'scene_narration' as const },
      { kind: 'chat' as const },
      { kind: 'scene_narration' as const },
    ]

    expect(sceneHeadingIndex(thread, { hasSession: true, closingIndex: 2 }))
      .toBe(0)
  })

  it('titles nothing once the scene metadata is gone', () => {
    const thread = [{ kind: 'scene_narration' as const }]

    expect(sceneHeadingIndex(thread, { hasSession: false })).toBeNull()
    expect(sceneHeadingIndex([{ kind: 'chat' as const }], { hasSession: true }))
      .toBeNull()
  })
})
