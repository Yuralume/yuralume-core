/**
 * The frontend side of the Cloud/Core cross-repo contract for official
 * character cards (OC6g, OFFICIAL_CARD_CLOUD_CATALOG_PLAN §3.5).
 *
 * Three things get pinned here, none of which are visible from reading
 * `CharacterCardFace.vue` alone:
 *
 *   1. `withProtectedCharacterCardImageToken` only ever touches a URL that
 *      starts with `/api/v1/character-cards/` — an official card's
 *      `image_url` is an *absolute* Cloud URL (the list route only serves
 *      local packs), so it must come back byte-for-byte untouched even when
 *      a token is on hand. Getting this wrong either 404s a real image (a
 *      foreign host doesn't recognise this deployment's token as a query
 *      param) or leaks the token into a request log on another host.
 *   2. `official_cards_unavailable` survives the round trip as a real
 *      boolean, and a response that omits `cards` entirely does not crash
 *      the panel.
 *   3. A `cloud:` pack id survives `encodeURIComponent` on the preview and
 *      install routes — the id is opaque to the frontend, and this is the
 *      one place that could accidentally re-encode or truncate the colon.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CharacterCardPackSummary } from '@/utils/api/characters'

const authHolder = vi.hoisted(() => ({ token: null as string | null }))

vi.mock('axios', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/composables/useAuth', () => ({
  getStoredToken: () => authHolder.token,
}))
// `characters.ts` pulls in `useActionPricing` for the R9 quote-binding on the
// image/draft endpoints — unrelated to this file, but its import chain
// reaches `authedFetch` -> the router -> `createWebHistory()`, which needs a
// `window` this repo's node test environment does not provide. Same mock
// `characterQuotedPrice.test.ts` uses to sidestep it.
vi.mock('@/utils/api/cloudPricing', () => ({ fetchCloudPricing: vi.fn() }))

const axios = (await import('axios')).default
const {
  installCharacterCard,
  listCharacterCards,
  previewCharacterCardPack,
} = await import('@/utils/api/characters')

const mockedGet = vi.mocked(axios.get)
const mockedPost = vi.mocked(axios.post)

beforeEach(() => {
  vi.clearAllMocks()
  authHolder.token = null
})

function baseCard(overrides: Partial<CharacterCardPackSummary>): CharacterCardPackSummary {
  return {
    pack_id: 'local-mio',
    title: 'Mio',
    author: '',
    description: '',
    tags: [],
    note: '',
    name: 'Mio',
    summary: '',
    personality: [],
    interests: [],
    speaking_style: 'natural',
    boundaries: [],
    aspirations: [],
    appearance: '',
    gender_identity: '',
    third_person_pronoun: '',
    visual_gender_presentation: '',
    visual_subject_type: 'human',
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
    world_frame: 'modern',
    world_awareness_enabled: false,
    world_topics: [],
    subscribed_categories: [],
    excluded_topics: [],
    proactive_enabled: true,
    proactive_daily_limit: 3,
    proactive_cooldown_minutes: 30,
    accepts_web_proactive: true,
    feed_daily_limit: 3,
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
    source: 'local',
    localized: false,
    ...overrides,
  }
}

describe('listCharacterCards — image access token', () => {
  it('leaves an absolute Cloud image URL untouched even when a token is on hand', async () => {
    authHolder.token = 'secret-token'
    const cloudCard = baseCard({
      pack_id: 'cloud:abc123',
      source: 'cloud',
      image_urls: ['https://app.yuralume.com/api/user/v1/public/official-cards/abc123/images/0'],
    })
    mockedGet.mockResolvedValueOnce({
      data: { cards: [cloudCard], official_cards_unavailable: false },
    })

    const { cards } = await listCharacterCards()

    expect(cards[0].image_urls).toEqual([
      'https://app.yuralume.com/api/user/v1/public/official-cards/abc123/images/0',
    ])
  })

  it('still stamps the access token onto a local, relative character-cards image URL', async () => {
    // Regression pin for the mechanism the ticket claimed was already
    // correct: this is the behaviour a cloud card must NOT get, so the two
    // tests together are the actual proof the branch is right.
    authHolder.token = 'secret-token'
    const localCard = baseCard({
      pack_id: 'mio',
      source: 'local',
      image_urls: ['/api/v1/character-cards/mio/images/0'],
    })
    mockedGet.mockResolvedValueOnce({
      data: { cards: [localCard], official_cards_unavailable: false },
    })

    const { cards } = await listCharacterCards()

    expect(cards[0].image_urls).toEqual([
      '/api/v1/character-cards/mio/images/0?access_token=secret-token',
    ])
  })

  it('does not append a token param twice when the URL already carries one', async () => {
    authHolder.token = 'secret-token'
    const localCard = baseCard({
      pack_id: 'mio',
      image_urls: ['/api/v1/character-cards/mio/images/0?access_token=already-there'],
    })
    mockedGet.mockResolvedValueOnce({
      data: { cards: [localCard], official_cards_unavailable: false },
    })

    const { cards } = await listCharacterCards()

    expect(cards[0].image_urls).toEqual([
      '/api/v1/character-cards/mio/images/0?access_token=already-there',
    ])
  })

  it('leaves every URL alone when there is no stored token', async () => {
    authHolder.token = null
    const localCard = baseCard({ image_urls: ['/api/v1/character-cards/mio/images/0'] })
    mockedGet.mockResolvedValueOnce({
      data: { cards: [localCard], official_cards_unavailable: false },
    })

    const { cards } = await listCharacterCards()

    expect(cards[0].image_urls).toEqual(['/api/v1/character-cards/mio/images/0'])
  })
})

describe('listCharacterCards — official_cards_unavailable', () => {
  it('carries the flag through as a real boolean', async () => {
    mockedGet.mockResolvedValueOnce({
      data: { cards: [], official_cards_unavailable: true },
    })

    const catalog = await listCharacterCards()

    expect(catalog.official_cards_unavailable).toBe(true)
    expect(catalog.cards).toEqual([])
  })

  it('defaults to an empty shelf and a false flag if the response omits them', async () => {
    mockedGet.mockResolvedValueOnce({ data: {} })

    const catalog = await listCharacterCards()

    expect(catalog.cards).toEqual([])
    expect(catalog.official_cards_unavailable).toBe(false)
  })
})

describe('preview / install — cloud: pack id round trip', () => {
  it('percent-encodes the colon in a cloud pack id on the preview route', async () => {
    mockedPost.mockResolvedValueOnce({ data: baseCard({ pack_id: 'cloud:abc123', source: 'cloud' }) })

    await previewCharacterCardPack('cloud:abc123')

    expect(mockedPost).toHaveBeenCalledWith(
      '/api/v1/character-cards/cloud%3Aabc123/preview',
      null,
      { params: { translate: undefined } },
    )
  })

  it('percent-encodes the colon in a cloud pack id on the install route', async () => {
    mockedPost.mockResolvedValueOnce({
      data: {
        character: { id: 'char-1' },
        landed_arc_template_ids: [],
        landed_arc_series_ids: [],
      },
    })

    await installCharacterCard('cloud:abc123')

    expect(mockedPost).toHaveBeenCalledWith(
      '/api/v1/character-cards/cloud%3Aabc123/install',
      null,
      { params: { translate: undefined } },
    )
  })

  it('still enriches a cloud preview with the same absolute-URL image handling', async () => {
    authHolder.token = 'secret-token'
    mockedPost.mockResolvedValueOnce({
      data: baseCard({
        pack_id: 'cloud:abc123',
        source: 'cloud',
        localized: true,
        image_urls: ['https://app.yuralume.com/api/user/v1/public/official-cards/abc123/images/0'],
      }),
    })

    const preview = await previewCharacterCardPack('cloud:abc123')

    expect(preview.image_urls).toEqual([
      'https://app.yuralume.com/api/user/v1/public/official-cards/abc123/images/0',
    ])
    expect(preview.source).toBe('cloud')
    expect(preview.localized).toBe(true)
  })
})
