/**
 * R9 close-out — the portrait and character-draft sends carry their own quote.
 *
 * Chat closed R9 first; these were the two entry points still letting the
 * server bind the charge to its *process* price cache, which under several
 * hosted replicas (or right after a back-office edit) is not the number this
 * screen showed. Same three promises as `chatQuotedPrice.test.ts`: the quote
 * travels, nothing travels when nothing is quotable, and the caller's own
 * fields are untouched.
 *
 * The draft endpoint is multipart, so its quote rides a form field — pinned
 * here because a JSON-shaped assumption would silently drop it.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('axios', () => ({
  default: { post: vi.fn(), get: vi.fn() },
}))
vi.mock('@/utils/api/cloudPricing', () => ({ fetchCloudPricing: vi.fn() }))
vi.mock('@/composables/useAuth', () => ({ getStoredToken: () => null }))

const axios = (await import('axios')).default
const { fetchCloudPricing } = await import('@/utils/api/cloudPricing')
const { useActionPricing } = await import('@/composables/useActionPricing')
const {
  generateCharacterPortrait,
  generateCharacterDraft,
  generatePortraitCandidates,
} = await import('@/utils/api/characters')

const mockedPost = vi.mocked(axios.post)
const mockedPricing = vi.mocked(fetchCloudPricing)

beforeEach(() => {
  vi.clearAllMocks()
  useActionPricing().reset()
  mockedPost.mockResolvedValue({ data: {} } as never)
})

async function seedPrices(portraitCr: number, draftCr: number): Promise<void> {
  mockedPricing.mockResolvedValueOnce({
    kind: 'ok',
    snapshot: {
      stale: false,
      tiers: [{
        tier_name: 'standard',
        billing_shape: 'action_fixed',
        actions: [
          {
            action_key: 'image_portrait',
            unit: 'per_portrait',
            price_cr: portraitCr,
            overage: false,
          },
          {
            action_key: 'character_draft',
            unit: 'per_draft',
            price_cr: draftCr,
            overage: false,
          },
        ],
      }],
    },
  })
  await useActionPricing().ensureLoaded()
}

function sentJsonBody(): Record<string, unknown> {
  return mockedPost.mock.calls[0]?.[1] as Record<string, unknown>
}

function sentForm(): FormData {
  return mockedPost.mock.calls[0]?.[1] as FormData
}

describe('portrait generation carries the price this screen quoted', () => {
  it('attaches the quote', async () => {
    await seedPrices(12, 4)

    await generateCharacterPortrait('char-1', 'cafe')

    expect(sentJsonBody()).toMatchObject({
      positive: 'cafe',
      aspect: 'portrait',
      quoted_price_cr: 12,
    })
  })

  it('sends no quote when nothing is quotable', async () => {
    // Self-host, a token-billed tier, a price list that never loaded — the
    // server falls back to its own cache exactly as before.
    await generateCharacterPortrait('char-1', 'cafe')

    expect('quoted_price_cr' in sentJsonBody()).toBe(false)
  })
})

describe('the candidate batch — the call the button really makes', () => {
  // `generateCharacterPortrait` is API surface nothing in the SPA renders;
  // the 產生 button posts a candidate batch. A quote on the former and none on
  // the latter would leave the only path players walk unbound.
  it('attaches the quote', async () => {
    await seedPrices(12, 4)

    await generatePortraitCandidates('char-1', 'cafe', 'portrait', 4)

    expect(sentJsonBody()).toMatchObject({
      positive: 'cafe',
      aspect: 'portrait',
      count: 4,
      quoted_price_cr: 12,
    })
  })

  it('quotes the batch price, not the price times the count', async () => {
    // One press is one action. Multiplying here would bind the charge to a
    // number the hint above the button never showed, and the server would
    // answer 409 for a price that never moved.
    await seedPrices(12, 4)

    await generatePortraitCandidates('char-1', 'cafe', 'portrait', 4)

    expect(sentJsonBody().quoted_price_cr).toBe(12)
  })

  it('sends no quote when nothing is quotable', async () => {
    await generatePortraitCandidates('char-1', 'cafe')

    expect('quoted_price_cr' in sentJsonBody()).toBe(false)
  })
})

describe('character draft carries the price this screen quoted', () => {
  it('attaches the quote as a form field', async () => {
    await seedPrices(12, 4)

    await generateCharacterDraft({ prompt: 'a gentle librarian' })

    const form = sentForm()
    expect(form.get('prompt')).toBe('a gentle librarian')
    expect(form.get('quoted_price_cr')).toBe('4')
  })

  it('sends no quote when nothing is quotable', async () => {
    await generateCharacterDraft({ prompt: 'a gentle librarian' })

    expect(sentForm().has('quoted_price_cr')).toBe(false)
  })

  it('never quotes one tier’s price to another tier’s player', async () => {
    // Two fixed-price tiers that disagree: the composable resolves `null`, so
    // the request must carry nothing rather than a fabricated number.
    mockedPricing.mockResolvedValueOnce({
      kind: 'ok',
      snapshot: {
        stale: false,
        tiers: [
          {
            tier_name: 'standard',
            billing_shape: 'action_fixed',
            actions: [{
              action_key: 'character_draft', unit: 'per_draft',
              price_cr: 4, overage: false,
            }],
          },
          {
            tier_name: 'pro',
            billing_shape: 'action_fixed',
            actions: [{
              action_key: 'character_draft', unit: 'per_draft',
              price_cr: 3, overage: false,
            }],
          },
        ],
      },
    })
    await useActionPricing().ensureLoaded()

    await generateCharacterDraft({ prompt: 'x' })

    expect(sentForm().has('quoted_price_cr')).toBe(false)
  })
})

describe('billing refusals surface as typed errors', () => {
  it('turns a 409 on a draft into the price-changed error', async () => {
    const { isPriceChangedError } = await import('@/utils/api/priceChanged')
    mockedPost.mockRejectedValueOnce({
      response: {
        status: 409,
        data: { detail: { code: 'price_changed', current_price_cr: 5 } },
      },
    })

    await expect(generateCharacterDraft({ prompt: 'x' })).rejects.toSatisfy(
      isPriceChangedError,
    )
  })

  it('turns a 402 on a draft into the out-of-credits error', async () => {
    const { isInsufficientCreditsError }
      = await import('@/utils/api/insufficientCredits')
    mockedPost.mockRejectedValueOnce({
      response: {
        status: 402,
        data: { detail: { code: 'insufficient_credits', message: 'nope' } },
      },
    })

    await expect(generateCharacterDraft({ prompt: 'x' })).rejects.toSatisfy(
      isInsufficientCreditsError,
    )
  })

  it('turns a 409 on a portrait into the price-changed error', async () => {
    const { isPriceChangedError } = await import('@/utils/api/priceChanged')
    mockedPost.mockRejectedValueOnce({
      response: {
        status: 409,
        data: { detail: { code: 'price_changed', current_price_cr: 5 } },
      },
    })

    await expect(
      generateCharacterPortrait('char-1', 'cafe'),
    ).rejects.toSatisfy(isPriceChangedError)
  })

  it('turns a 409 on a candidate batch into the price-changed error', async () => {
    // Newly reachable: the batch is action-priced now, so the panel must be
    // able to tell "the price moved, resend" from "the generator broke".
    const { isPriceChangedError } = await import('@/utils/api/priceChanged')
    mockedPost.mockRejectedValueOnce({
      response: {
        status: 409,
        data: { detail: { code: 'price_changed', current_price_cr: 5 } },
      },
    })

    await expect(
      generatePortraitCandidates('char-1', 'cafe'),
    ).rejects.toSatisfy(isPriceChangedError)
  })

  it('turns a 402 on a candidate batch into the out-of-credits error', async () => {
    const { isInsufficientCreditsError }
      = await import('@/utils/api/insufficientCredits')
    mockedPost.mockRejectedValueOnce({
      response: {
        status: 402,
        data: { detail: { code: 'insufficient_credits', message: 'nope' } },
      },
    })

    await expect(
      generatePortraitCandidates('char-1', 'cafe'),
    ).rejects.toSatisfy(isInsufficientCreditsError)
  })
})
