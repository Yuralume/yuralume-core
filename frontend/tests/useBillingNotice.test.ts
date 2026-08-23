/**
 * FX2 — a refusal is answered where the player is standing.
 *
 * The four branching-drama entry points (create, start, talk, advance) all
 * funnelled a `402 insufficient_credits` and a `409 price_changed` into
 * `emit('error')`, which navigated the player out of the VN, dropped the line
 * they had just typed, and printed the gateway's English sentence. Both
 * refusals charged nothing and ran nothing, so what is pinned here is that
 * `absorb` *takes them off the caller's hands* (returns `true`, so the caller
 * returns before its own error path) — and that an ordinary fault is still
 * handed back.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setDeploymentMode } from '@/composables/deploymentMode'

vi.mock('@/utils/api/cloudPricing', () => ({ fetchCloudPricing: vi.fn() }))

const { fetchCloudPricing } = await import('@/utils/api/cloudPricing')
const { useActionPricing } = await import('@/composables/useActionPricing')
const { useBillingNotice } = await import('@/composables/useBillingNotice')
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
          action_key: 'branching_drama_advance',
          unit: 'per_iteration',
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

describe('an empty wallet is answered in place', () => {
  it('absorbs the 402 and raises the top-up card', async () => {
    const notice = useBillingNotice()

    const handled = await notice.absorb(new InsufficientCreditsError())

    expect(handled).toBe(true)
    expect(notice.outOfCredits.value).toBe(true)
    expect(notice.priceChanged.value).toBe(false)
  })

  it('does not spend a request re-pulling prices — nothing moved', async () => {
    const notice = useBillingNotice()

    await notice.absorb(new InsufficientCreditsError())

    expect(mockedPricing).not.toHaveBeenCalled()
  })
})

describe('a moved price is answered in place, at the new number', () => {
  it('absorbs the 409 and re-pulls the published list', async () => {
    mockedPricing.mockResolvedValueOnce(snapshot(6))
    await useActionPricing().ensureLoaded()
    const notice = useBillingNotice()

    mockedPricing.mockResolvedValueOnce(snapshot(9))
    const handled = await notice.absorb(
      new PriceChangedError({ currentPriceCr: 9 }),
    )

    expect(handled).toBe(true)
    expect(notice.priceChanged.value).toBe(true)
    expect(notice.outOfCredits.value).toBe(false)
    // Without this the chip beside the button keeps advertising 6 and the
    // "press it again" retry quotes 6 again — a loop the player cannot exit.
    expect(useActionPricing().priceOf('branching_drama_advance')?.price_cr)
      .toBe(9)
  })

  it('replaces a standing top-up card rather than stacking on it', async () => {
    mockedPricing.mockResolvedValue(snapshot(6))
    const notice = useBillingNotice()
    await notice.absorb(new InsufficientCreditsError())

    await notice.absorb(new PriceChangedError({ currentPriceCr: 9 }))

    expect(notice.outOfCredits.value).toBe(false)
    expect(notice.priceChanged.value).toBe(true)
  })
})

describe('an ordinary fault stays the caller’s problem', () => {
  it('hands back a crash untouched', async () => {
    const notice = useBillingNotice()

    const handled = await notice.absorb(new Error('gpu on fire'))

    expect(handled).toBe(false)
    expect(notice.outOfCredits.value).toBe(false)
    expect(notice.priceChanged.value).toBe(false)
  })

  it('keeps one surface’s refusal off another’s screen', async () => {
    // Module-scope state would leak the drama page's 402 onto the VN player.
    const page = useBillingNotice()
    const player = useBillingNotice()

    await page.absorb(new InsufficientCreditsError())

    expect(player.outOfCredits.value).toBe(false)
  })

  it('clears on the next press', async () => {
    const notice = useBillingNotice()
    await notice.absorb(new InsufficientCreditsError())

    notice.clear()

    expect(notice.outOfCredits.value).toBe(false)
    expect(notice.priceChanged.value).toBe(false)
  })
})
