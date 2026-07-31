import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchCloudPricing } from '@/utils/api/cloudPricing'
import type { TierPricing } from '@/utils/api/cloudPricing'
import {
  ACTION_CHAT,
  ACTION_IMAGE_PORTRAIT,
  resolveActionPrice,
  useActionPricing,
} from '@/composables/useActionPricing'

vi.mock('@/utils/api/cloudPricing', () => ({
  fetchCloudPricing: vi.fn(),
}))

const mockedFetch = vi.mocked(fetchCloudPricing)

function tier(
  name: string,
  shape: string,
  actions: Array<[string, number, string?]>,
): TierPricing {
  return {
    tier_name: name,
    billing_shape: shape,
    actions: actions.map(([action_key, price_cr, unit]) => ({
      action_key,
      unit: unit ?? 'per_message',
      price_cr,
      overage: action_key.endsWith('_overage'),
    })),
  }
}

function ok(tiers: TierPricing[], stale = false) {
  return { kind: 'ok' as const, snapshot: { tiers, stale } }
}

beforeEach(() => {
  vi.clearAllMocks()
  useActionPricing().reset()
})

describe('resolveActionPrice', () => {
  it('quotes the price when every fixed-price tier agrees', () => {
    const tiers = [
      tier('standard', 'action_fixed', [['chat', 3]]),
      tier('pro', 'action_fixed', [['chat', 3]]),
    ]

    expect(resolveActionPrice(tiers, ACTION_CHAT)?.price_cr).toBe(3)
  })

  it('quotes nothing when the tiers disagree — we cannot tell which is yours', () => {
    // Showing one tier's number to a player on another tier is a fake
    // number, which the plan bans outright. Silence is the honest answer.
    const tiers = [
      tier('standard', 'action_fixed', [['chat', 3]]),
      tier('pro', 'action_fixed', [['chat', 2]]),
    ]

    expect(resolveActionPrice(tiers, ACTION_CHAT)).toBeNull()
  })

  it('quotes nothing when a fixed-price tier has no published price', () => {
    const tiers = [
      tier('standard', 'action_fixed', [['chat', 3]]),
      tier('pro', 'action_fixed', [['image_portrait', 9]]),
    ]

    expect(resolveActionPrice(tiers, ACTION_CHAT)).toBeNull()
  })

  it('quotes nothing while every tier still bills by usage', () => {
    // Under token_floating the fixed price is not what gets charged, so
    // printing it would be a lie even though the number exists.
    const tiers = [tier('standard', 'token_floating', [['chat', 3]])]

    expect(resolveActionPrice(tiers, ACTION_CHAT)).toBeNull()
  })

  it('ignores usage-billed tiers when a fixed-price tier exists', () => {
    const tiers = [
      tier('legacy', 'token_floating', [['chat', 99]]),
      tier('standard', 'action_fixed', [['chat', 3]]),
    ]

    expect(resolveActionPrice(tiers, ACTION_CHAT)?.price_cr).toBe(3)
  })

  it('carries the unit through so the caller can say "per picture"', () => {
    const tiers = [tier('standard', 'action_fixed', [['image_portrait', 12, 'per_portrait']])]

    expect(resolveActionPrice(tiers, ACTION_IMAGE_PORTRAIT)?.unit).toBe('per_portrait')
  })
})

describe('useActionPricing state machine', () => {
  it('starts with nothing to quote', () => {
    expect(useActionPricing().priceOf(ACTION_CHAT)).toBeNull()
  })

  it('loads once — a second ensureLoaded does not re-request', async () => {
    mockedFetch.mockResolvedValueOnce(ok([tier('standard', 'action_fixed', [['chat', 3]])]))
    const pricing = useActionPricing()

    await pricing.ensureLoaded()
    await pricing.ensureLoaded()

    expect(mockedFetch).toHaveBeenCalledTimes(1)
    expect(pricing.priceOf(ACTION_CHAT)?.price_cr).toBe(3)
  })

  it('stops asking for good once the deployment says there is no price list', async () => {
    mockedFetch.mockResolvedValueOnce({ kind: 'unsupported' })
    const pricing = useActionPricing()

    await pricing.ensureLoaded()
    await pricing.refresh()

    expect(mockedFetch).toHaveBeenCalledTimes(1)
    expect(pricing.priceOf(ACTION_CHAT)).toBeNull()
  })

  it('keeps the last-known-good list when a refresh fails', async () => {
    mockedFetch.mockResolvedValueOnce(ok([tier('standard', 'action_fixed', [['chat', 3]])]))
    const pricing = useActionPricing()
    await pricing.ensureLoaded()

    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })
    await pricing.refresh()

    expect(pricing.priceOf(ACTION_CHAT)?.price_cr).toBe(3)
  })

  it('collapses concurrent callers onto one request', async () => {
    mockedFetch.mockResolvedValueOnce(ok([tier('standard', 'action_fixed', [['chat', 3]])]))
    const pricing = useActionPricing()

    await Promise.all([pricing.ensureLoaded(), pricing.ensureLoaded()])

    expect(mockedFetch).toHaveBeenCalledTimes(1)
  })
})

describe('affordability pre-check', () => {
  // The balance singleton is the other half of the pre-check; it is mocked
  // through its own transport so this file stays about the pricing rules.
  it('never blocks an action whose price is unknown', async () => {
    const { shortfallFor } = useActionPricing()

    expect(shortfallFor(ACTION_CHAT, { total: 0, known: true, stale: false })).toBeNull()
  })

  it('blocks when the balance cannot cover the published price', async () => {
    mockedFetch.mockResolvedValueOnce(ok([tier('standard', 'action_fixed', [['chat', 3]])]))
    const pricing = useActionPricing()
    await pricing.ensureLoaded()

    expect(pricing.shortfallFor(ACTION_CHAT, { total: 2, known: true, stale: false }))
      .toBe(3)
  })

  it('lets the action through when the balance covers the price exactly', async () => {
    mockedFetch.mockResolvedValueOnce(ok([tier('standard', 'action_fixed', [['chat', 3]])]))
    const pricing = useActionPricing()
    await pricing.ensureLoaded()

    expect(pricing.shortfallFor(ACTION_CHAT, { total: 3, known: true, stale: false }))
      .toBeNull()
  })

  it('does not block on an unknown balance', async () => {
    mockedFetch.mockResolvedValueOnce(ok([tier('standard', 'action_fixed', [['chat', 3]])]))
    const pricing = useActionPricing()
    await pricing.ensureLoaded()

    expect(pricing.shortfallFor(ACTION_CHAT, { total: 0, known: false, stale: false }))
      .toBeNull()
  })

  it('does not block on a stale balance — the server gets the last word', async () => {
    // A stale number is exactly the case where refusing locally would
    // strand a player who has already topped up.
    mockedFetch.mockResolvedValueOnce(ok([tier('standard', 'action_fixed', [['chat', 3]])]))
    const pricing = useActionPricing()
    await pricing.ensureLoaded()

    expect(pricing.shortfallFor(ACTION_CHAT, { total: 0, known: true, stale: true }))
      .toBeNull()
  })
})
