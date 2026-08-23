/**
 * BD3 — the three drama presses carry the price this screen quoted.
 *
 * Same three promises as `characterQuotedPrice.test.ts`: the quote travels,
 * nothing travels when nothing is quotable, and the caller's own fields are
 * untouched. Advance is the one worth pinning hardest — it had no request
 * body at all before BD3, so the quote is the only reason it has one now, and
 * an unquotable deployment must still send a body the old backend accepts.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setDeploymentMode } from '@/composables/deploymentMode'

vi.mock('@/utils/authedFetch', () => ({ authedFetch: vi.fn() }))
vi.mock('@/utils/api/cloudPricing', () => ({ fetchCloudPricing: vi.fn() }))

const { authedFetch } = await import('@/utils/authedFetch')
const { fetchCloudPricing } = await import('@/utils/api/cloudPricing')
const { useActionPricing } = await import('@/composables/useActionPricing')
const {
  createBranchingDrama,
  advanceSession,
  interactSession,
  regenerateSceneImage,
  startSession,
} = await import('@/utils/api/branchingDrama')

const mockedFetch = vi.mocked(authedFetch)
const mockedPricing = vi.mocked(fetchCloudPricing)

beforeEach(() => {
  vi.clearAllMocks()
  useActionPricing().reset()
  setDeploymentMode('cloud')
  mockedFetch.mockResolvedValue(
    new Response(JSON.stringify({}), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
})

async function seedPrices(): Promise<void> {
  mockedPricing.mockResolvedValueOnce({
    kind: 'ok',
    snapshot: {
      stale: false,
      tiers: [{
        tier_name: 'standard',
        billing_shape: 'action_fixed',
        actions: [
          {
            action_key: 'branching_drama_create',
            unit: 'per_drama',
            price_cr: 40,
            overage: false,
          },
          {
            action_key: 'branching_drama_advance',
            unit: 'per_iteration',
            price_cr: 6,
            overage: false,
          },
          {
            action_key: 'branching_drama_interact',
            unit: 'per_message',
            price_cr: 2,
            overage: false,
          },
          {
            action_key: 'branching_drama_scene_regen',
            unit: 'per_image',
            price_cr: 3,
            overage: false,
          },
        ],
      }],
    },
  })
  await useActionPricing().ensureLoaded()
}

function sentBody(): Record<string, unknown> {
  const init = mockedFetch.mock.calls[0]?.[1] as RequestInit
  return JSON.parse(String(init.body)) as Record<string, unknown>
}

const CREATE_PAYLOAD = {
  character_ids: ['c-a', 'c-b'],
  prompt: '在咖啡廳發生的故事',
  total_segments: 3,
  operator_position: 'central' as const,
}

describe('create carries the price this screen quoted', () => {
  it('attaches the quote without disturbing the payload', async () => {
    await seedPrices()

    await createBranchingDrama({ ...CREATE_PAYLOAD })

    expect(sentBody()).toEqual({ ...CREATE_PAYLOAD, quoted_price_cr: 40 })
  })

  it('sends nothing when no price can be stated honestly', async () => {
    mockedPricing.mockResolvedValueOnce({ kind: 'unsupported' })
    await useActionPricing().ensureLoaded()

    await createBranchingDrama({ ...CREATE_PAYLOAD })

    expect(sentBody()).toEqual(CREATE_PAYLOAD)
    expect('quoted_price_cr' in sentBody()).toBe(false)
  })
})

describe('advance carries its own, different price', () => {
  it('attaches the advance quote — not create’s', async () => {
    await seedPrices()

    await advanceSession('drama-1', 'session-1')

    expect(sentBody()).toEqual({ quoted_price_cr: 6 })
  })

  it('still posts a body the pre-BD3 backend accepts when unquotable', async () => {
    mockedPricing.mockResolvedValueOnce({ kind: 'unsupported' })
    await useActionPricing().ensureLoaded()

    await advanceSession('drama-1', 'session-1')

    expect(sentBody()).toEqual({})
  })
})

describe('starting a playthrough quotes an advance (FX1/FX2)', () => {
  it('sends the advance price — the opening beat is one advance', async () => {
    await seedPrices()

    await startSession('drama-1')

    // Not create's 40: building the tree already happened and was paid for.
    // What this press buys is the opening beat, which is a generation like
    // any other advance — so it is quoted, and charged, as one.
    expect(sentBody()).toEqual({ quoted_price_cr: 6 })
  })

  it('still posts a body the pre-FX1 backend accepts when unquotable', async () => {
    mockedPricing.mockResolvedValueOnce({ kind: 'unsupported' })
    await useActionPricing().ensureLoaded()

    await startSession('drama-1')

    expect(sentBody()).toEqual({})
  })
})

describe('interact carries the per-exchange price', () => {
  it('keeps the player input alongside the quote', async () => {
    await seedPrices()

    await interactSession('drama-1', 'session-1', '你還好嗎？')

    expect(sentBody()).toEqual({
      player_input: '你還好嗎？',
      quoted_price_cr: 2,
    })
  })
})

describe('the scene redraw is priced on its own (BD6)', () => {
  it('quotes the redraw, not the beat it sits on', async () => {
    await seedPrices()

    await regenerateSceneImage('drama-1', 'node-1')

    expect(sentBody()).toEqual({ quoted_price_cr: 3 })
  })

  it('posts to the node’s redraw endpoint', async () => {
    await seedPrices()

    await regenerateSceneImage('drama-1', 'node-1')

    const [url, init] = mockedFetch.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(
      '/api/v1/branching-dramas/drama-1/nodes/node-1/image/regenerate',
    )
    expect(init.method).toBe('POST')
  })

  it('still posts an empty body when nothing is quotable', async () => {
    mockedPricing.mockResolvedValueOnce({ kind: 'unsupported' })
    await useActionPricing().ensureLoaded()

    await regenerateSceneImage('drama-1', 'node-1')

    expect(sentBody()).toEqual({})
  })
})
