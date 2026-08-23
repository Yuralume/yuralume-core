/**
 * The shared "is this an answer or a fault?" rule behind every priced button.
 *
 * Four surfaces consume it (fusion create, the three fusion iterate buttons,
 * the picture batch, the character draft). Each of them used to carry its own
 * copy, which is exactly how the newly-priced `/images/candidates` shipped
 * rendering a moved price as "生成失敗". What is pinned here is the part with
 * actual logic: which failures are refusals, which are not, and that a moved
 * price re-pulls the price list so the retry is not guaranteed to fail again.
 */

import { readFileSync } from 'node:fs'

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setDeploymentMode } from '@/composables/deploymentMode'

vi.mock('@/utils/api/cloudPricing', () => ({ fetchCloudPricing: vi.fn() }))

const { fetchCloudPricing } = await import('@/utils/api/cloudPricing')
const { useActionPricing } = await import('@/composables/useActionPricing')
const { billingRefusalKind, isRetryableConflict, refreshQuotedPrices }
  = await import('@/utils/api/billingRefusal')
const { InsufficientCreditsError }
  = await import('@/utils/api/insufficientCredits')
const { PriceChangedError } = await import('@/utils/api/priceChanged')

const mockedPricing = vi.mocked(fetchCloudPricing)

function snapshot(priceCr: number) {
  return {
    kind: 'ok' as const,
    snapshot: {
      stale: false,
      tiers: [{
        tier_name: 'standard',
        billing_shape: 'action_fixed',
        actions: [{
          action_key: 'image_portrait',
          unit: 'per_portrait',
          price_cr: priceCr,
          overage: false,
        }],
      }],
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useActionPricing().reset()
  setDeploymentMode('cloud')
})

describe('billingRefusalKind', () => {
  it('names the empty wallet', () => {
    expect(billingRefusalKind(new InsufficientCreditsError())).toBe(
      'insufficient_credits',
    )
  })

  it('names the moved price', () => {
    expect(billingRefusalKind(new PriceChangedError({ currentPriceCr: 5 })))
      .toBe('price_changed')
  })

  it('leaves an ordinary failure alone', () => {
    // A generator crash, a dropped connection, a 500 — these are faults, and
    // dressing one up as a top-up card would send the player to pay for a
    // problem money cannot fix.
    expect(billingRefusalKind(new Error('gpu on fire'))).toBeNull()
    expect(billingRefusalKind(null)).toBeNull()
    expect(billingRefusalKind({ response: { status: 402 } })).toBeNull()
  })
})

describe('isRetryableConflict', () => {
  function statusError(statusCode: number): Error {
    return Object.assign(new Error(`status ${statusCode}`), { statusCode })
  }

  it('retries the transient 409 — "someone is already generating this"', () => {
    // `BranchingGenerationInProgress` / `SceneRegenerationInProgress`: a plain
    // string detail, and waiting is exactly what clears it.
    expect(isRetryableConflict(statusError(409))).toBe(true)
  })

  it('never retries a moved price, which shares the 409', () => {
    // The bug this exists to stop: `price_changed` also carries `statusCode
    // 409`, so a bare status check sat on it for the full 60s retry window —
    // a minute of fake "still generating…" and then the refusal thrown at the
    // player as a fatal error that booted them out of the VN.
    expect(isRetryableConflict(new PriceChangedError({ currentPriceCr: 8 })))
      .toBe(false)
  })

  it('never retries an empty wallet', () => {
    expect(isRetryableConflict(new InsufficientCreditsError())).toBe(false)
  })

  it('leaves every other failure to the caller', () => {
    expect(isRetryableConflict(statusError(500))).toBe(false)
    expect(isRetryableConflict(statusError(404))).toBe(false)
    expect(isRetryableConflict(new Error('offline'))).toBe(false)
    expect(isRetryableConflict(null)).toBe(false)
  })
})

describe('refreshQuotedPrices', () => {
  it('re-pulls the published list rather than reusing the refused one', async () => {
    mockedPricing.mockResolvedValueOnce(snapshot(12))
    await useActionPricing().ensureLoaded()
    expect(useActionPricing().priceOf('image_portrait')?.price_cr).toBe(12)

    // The back office moved the price; the server answered 409 for the 12 this
    // screen quoted. Without the refresh the hint would keep showing 12 and
    // the "send it again" retry would quote 12 again — a loop.
    mockedPricing.mockResolvedValueOnce(snapshot(15))
    await refreshQuotedPrices()

    expect(mockedPricing).toHaveBeenCalledTimes(2)
    expect(mockedPricing.mock.calls[1]?.[0]).toMatchObject({ refresh: true })
    expect(useActionPricing().priceOf('image_portrait')?.price_cr).toBe(15)
  })

  it('keeps the previous list when the refresh cannot land', async () => {
    // Fail-soft: an unimproved retry is worse than nothing to quote at all.
    mockedPricing.mockResolvedValueOnce(snapshot(12))
    await useActionPricing().ensureLoaded()

    mockedPricing.mockResolvedValueOnce({ kind: 'degraded' as const })
    await refreshQuotedPrices()

    expect(useActionPricing().priceOf('image_portrait')?.price_cr).toBe(12)
  })
})

// ----------------------------------------------------------------------
// The refusal's *surface*, not just its classification
// ----------------------------------------------------------------------

/**
 * Naming the refusal correctly is only half the job — every surface then has
 * to answer it the same way. `InsufficientCreditsNotice` is that answer: it
 * states that nothing ran and nothing was charged, and it carries the top-up
 * link, which is the only thing the player can actually do next. A surface
 * that instead prints the refusal into its red error line tells the same
 * player their generator is broken and offers them nowhere to go — which is
 * exactly what the character-draft button did while the picture batch, the
 * fusion viewer and the drama page all showed the card.
 *
 * Read from source because the harness renders SSR only: there is no way to
 * drive a component into its 402 state from here, and "does this file even
 * know about the shared card" is the part that actually regressed.
 */
const PRICED_SURFACES = [
  'components/CharacterCreateModal.vue',
  'components/CharacterImagesPanel.vue',
  'components/ChatPanel.vue',
  'components/fusion-story/FusionStoryViewer.vue',
  'pages/FusionStoryPage.vue',
  'pages/BranchingDramaPage.vue',
  // FX2: start / talk / advance answer a 402 in place too, instead of
  // emitting `error` and booting the player out of the playthrough.
  'components/branching-drama/BranchingDramaPlayer.vue',
]

function source(file: string): string {
  return readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
}

describe('insufficient-credits surface parity', () => {
  it.each(PRICED_SURFACES)('%s answers a 402 with the shared card', (file) => {
    const src = source(file)

    expect(src).toContain(
      "from '@/components/InsufficientCreditsNotice.vue'",
    )
    expect(src).toContain('<InsufficientCreditsNotice')
  })

  it('retries the drama advance on the shape, never on the bare status', () => {
    // The FX2 regression in concrete form: `isRetryable: (e) => status === 409`
    // swallowed `price_changed` into a 60-second "still generating…".
    const src = source('components/branching-drama/BranchingDramaPlayer.vue')

    expect(src).toContain('isRetryable: isRetryableConflict')
    expect(src).not.toContain('apiStatusErrorStatus')
  })

  it('does not leave the draft refusal in a plain error line', () => {
    // The regression in concrete form: the draft button used to assign
    // `credits.insufficient.body` straight into `draftError`, which renders as
    // `.error-msg` — the same red box a crashed generator gets.
    expect(source('components/CharacterCreateModal.vue'))
      .not.toContain("t('credits.insufficient.body')")
  })
})
