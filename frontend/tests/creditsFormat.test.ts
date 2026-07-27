import { describe, expect, it } from 'vitest'

import {
  formatCreditAmount,
  portalCreditsUrl,
} from '@/utils/creditsFormat'

describe('formatCreditAmount', () => {
  it('groups thousands', () => {
    expect(formatCreditAmount(12340)).toBe('12,340')
    expect(formatCreditAmount(999)).toBe('999')
    expect(formatCreditAmount(1234567)).toBe('1,234,567')
  })

  it('drops the fraction for whole balances but keeps it otherwise', () => {
    // The Portal ledger always shows 2dp because a single row can be a
    // fraction; a badge shows a balance, where ".00" is just noise.
    expect(formatCreditAmount(1200)).toBe('1,200')
    expect(formatCreditAmount(1200.5)).toBe('1,200.50')
    expect(formatCreditAmount(0.125)).toBe('0.13')
  })

  it('never renders a negative zero', () => {
    expect(formatCreditAmount(-0.001)).toBe('0')
    expect(formatCreditAmount(0)).toBe('0')
  })

  it('degrades to zero for non-finite input', () => {
    expect(formatCreditAmount(Number.NaN)).toBe('0')
  })
})

describe('portalCreditsUrl', () => {
  it('builds the ledger deep link, tolerating a trailing slash', () => {
    expect(portalCreditsUrl('https://app.yuralume.com'))
      .toBe('https://app.yuralume.com/credits')
    expect(portalCreditsUrl('https://app.yuralume.com/'))
      .toBe('https://app.yuralume.com/credits')
  })

  it('returns null without a portal so callers render no link at all', () => {
    expect(portalCreditsUrl(null)).toBeNull()
    expect(portalCreditsUrl('   ')).toBeNull()
  })
})
