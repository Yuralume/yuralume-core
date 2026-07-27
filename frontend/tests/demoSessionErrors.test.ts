import { describe, expect, it } from 'vitest'

import { DemoSessionLoginError } from '@/utils/api/auth'
import { demoSessionErrorCopy } from '@/utils/demoSessionErrors'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

const CATALOGS = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP }

function lookup(catalog: unknown, key: string): unknown {
  return key.split('.').reduce<unknown>(
    (node, part) => (node as Record<string, unknown> | undefined)?.[part],
    catalog,
  )
}

describe('demoSessionErrorCopy', () => {
  it('shows a capacity path for demo_busy', () => {
    const copy = demoSessionErrorCopy(new DemoSessionLoginError({
      code: 'demo_busy',
      message: 'demo slots are full',
      retryable: true,
      statusCode: 503,
    }))

    expect(copy.titleKey).toBe('demo.errors.busyTitle')
    expect(copy.messageKey).toBe('demo.errors.busyBody')
    expect(copy.actions.map((action) => action.labelKey)).toEqual([
      'demo.actions.tier0',
      'demo.actions.waitlist',
      'demo.actions.discord',
    ])
  })

  it('shows a daily source limit path for demo_rate_limited', () => {
    const copy = demoSessionErrorCopy(new DemoSessionLoginError({
      code: 'demo_rate_limited',
      message: 'rate limited',
      retryable: true,
      statusCode: 429,
    }))

    expect(copy.titleKey).toBe('demo.errors.rateLimitedTitle')
    expect(copy.messageKey).toBe('demo.errors.rateLimitedBody')
    expect(copy.actions.map((action) => action.labelKey)).toEqual([
      'demo.actions.tier0',
      'demo.actions.discord',
      'demo.actions.selfHost',
    ])
  })

  it('keeps conversion paths for generic demo startup failures', () => {
    const copy = demoSessionErrorCopy(new Error('upstream unavailable'))

    expect(copy.titleKey).toBe('demo.errors.unavailableTitle')
    expect(copy.actions.map((action) => action.href)).toEqual([
      '/#demo-showcase',
      'https://discord.gg/tF8zw7S6',
      '/#tiers',
    ])
  })

  it('resolves every emitted key in all three catalogs', () => {
    // Regression guard for the copy that used to be hard-coded English here:
    // a classification returning a key the catalog lacks renders blank, which
    // is worse than the untranslated string it replaced.
    const copies = [
      demoSessionErrorCopy(new DemoSessionLoginError({
        code: 'demo_busy', message: '', retryable: true, statusCode: 503,
      })),
      demoSessionErrorCopy(new DemoSessionLoginError({
        code: 'demo_rate_limited', message: '', retryable: true, statusCode: 429,
      })),
      demoSessionErrorCopy(new Error('boom')),
    ]

    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      for (const copy of copies) {
        const keys = [
          copy.titleKey,
          copy.messageKey,
          ...copy.actions.map((action) => action.labelKey),
        ]
        for (const key of keys) {
          expect(typeof lookup(catalog, key), `${key} missing in ${locale}`)
            .toBe('string')
        }
      }
    }
  })
})
