import { describe, expect, it } from 'vitest'

import {
  PRICE_BOOK_STORAGE_KEY,
  computeCustomCost,
  hasPrice,
  loadPriceBook,
  priceBookKey,
  savePriceBook,
} from '@/utils/usagePricing'

function fakeStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  } satisfies Pick<Storage, 'getItem' | 'setItem'>
}

describe('usagePricing', () => {
  it('builds a capability-scoped key that survives ids with slashes', () => {
    expect(priceBookKey('llm', 'openrouter', 'openai/gpt-5.5')).toBe(
      'llm␟openrouter␟openai/gpt-5.5',
    )
    // Same model id under a different capability is a different row.
    expect(priceBookKey('image', 'openai', 'gpt-image-2')).not.toBe(
      priceBookKey('llm', 'openai', 'gpt-image-2'),
    )
  })

  it('computes cost from per-1M prices', () => {
    // 2M input @ $2.50/1M + 0.5M output @ $10/1M = 5 + 5 = 10
    const cost = computeCustomCost(2_000_000, 500_000, {
      inputPerMillion: '2.50',
      outputPerMillion: '10',
    })
    expect(cost).toBeCloseTo(10, 8)
  })

  it('treats blank / non-positive prices as zero contribution', () => {
    expect(computeCustomCost(1_000_000, 1_000_000, undefined)).toBe(0)
    expect(
      computeCustomCost(1_000_000, 1_000_000, { inputPerMillion: '', outputPerMillion: '-3' }),
    ).toBe(0)
    // Only output priced → input contributes nothing.
    expect(
      computeCustomCost(1_000_000, 1_000_000, { inputPerMillion: '', outputPerMillion: '4' }),
    ).toBeCloseTo(4, 8)
  })

  it('reports whether an entry carries any positive price', () => {
    expect(hasPrice(undefined)).toBe(false)
    expect(hasPrice({ inputPerMillion: '0', outputPerMillion: '' })).toBe(false)
    expect(hasPrice({ inputPerMillion: '1', outputPerMillion: '' })).toBe(true)
  })

  it('round-trips through storage and ignores corrupt payloads', () => {
    const storage = fakeStorage()
    const book = { [priceBookKey('llm', 'p', 'm')]: { inputPerMillion: '2', outputPerMillion: '8' } }
    expect(savePriceBook(storage, book)).toBe(true)
    expect(loadPriceBook(storage)).toEqual(book)

    expect(loadPriceBook(fakeStorage({ [PRICE_BOOK_STORAGE_KEY]: '{not json' }))).toEqual({})
    expect(
      loadPriceBook(fakeStorage({ [PRICE_BOOK_STORAGE_KEY]: '{"k":{"inputPerMillion":1}}' })),
    ).toEqual({})
  })

  it('fails soft when storage is unavailable', () => {
    expect(loadPriceBook(null)).toEqual({})
    expect(savePriceBook(null, {})).toBe(false)
  })
})
