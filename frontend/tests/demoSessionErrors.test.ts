import { describe, expect, it } from 'vitest'

import { DemoSessionLoginError } from '@/utils/api/auth'
import { demoSessionErrorCopy } from '@/utils/demoSessionErrors'

describe('demoSessionErrorCopy', () => {
  it('shows a capacity path for demo_busy', () => {
    const copy = demoSessionErrorCopy(new DemoSessionLoginError({
      code: 'demo_busy',
      message: 'demo slots are full',
      retryable: true,
      statusCode: 503,
    }))

    expect(copy.title).toBe('Demo is full')
    expect(copy.message).toContain('Tier 0')
    expect(copy.actions.map((action) => action.label)).toEqual([
      'Try Tier 0',
      'Join waitlist',
      'Join Discord',
    ])
  })

  it('shows a daily source limit path for demo_rate_limited', () => {
    const copy = demoSessionErrorCopy(new DemoSessionLoginError({
      code: 'demo_rate_limited',
      message: 'rate limited',
      retryable: true,
      statusCode: 429,
    }))

    expect(copy.title).toBe('Demo limit reached')
    expect(copy.message).toContain('demo start limit')
    expect(copy.actions.map((action) => action.label)).toEqual([
      'Try Tier 0',
      'Join Discord',
      'Self-host path',
    ])
  })

  it('keeps conversion paths for generic demo startup failures', () => {
    const copy = demoSessionErrorCopy(new Error('upstream unavailable'))

    expect(copy.title).toBe('Demo unavailable')
    expect(copy.actions.map((action) => action.href)).toEqual([
      '/#demo-showcase',
      'https://discord.gg/tF8zw7S6',
      '/#tiers',
    ])
  })
})
