import { describe, expect, it } from 'vitest'

import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import {
  LocaleAlreadyConfirmedError,
  LocaleChangeInProgressError,
  LocaleChangeLimitError,
} from '@/utils/api/playerLocale'
import {
  localeChangePayload,
  localeChangeSubject,
  localeConfirmFailureCopy,
  shouldBlockForLocaleConfirmation,
} from '@/utils/localeLifecycle'

const CLOUD_NEEDS_CONFIRMATION = {
  cloudMode: true,
  authenticated: true,
  needsConfirmation: true,
  publicRoute: false,
}

describe('shouldBlockForLocaleConfirmation', () => {
  it('blocks a hosted player who has not acknowledged their locale', () => {
    expect(shouldBlockForLocaleConfirmation(CLOUD_NEEDS_CONFIRMATION)).toBe(true)
  })

  it('never blocks self-host', () => {
    expect(shouldBlockForLocaleConfirmation({
      ...CLOUD_NEEDS_CONFIRMATION,
      cloudMode: false,
    })).toBe(false)
  })

  it('does not block a hosted player who already confirmed', () => {
    expect(shouldBlockForLocaleConfirmation({
      ...CLOUD_NEEDS_CONFIRMATION,
      needsConfirmation: false,
    })).toBe(false)
  })

  it('stays out of the login / setup / callback flow', () => {
    // Blocking a public route would deadlock the very flow that produces
    // the identity the gate reads.
    expect(shouldBlockForLocaleConfirmation({
      ...CLOUD_NEEDS_CONFIRMATION,
      publicRoute: true,
    })).toBe(false)
  })

  it('waits until an identity is resolved', () => {
    expect(shouldBlockForLocaleConfirmation({
      ...CLOUD_NEEDS_CONFIRMATION,
      authenticated: false,
    })).toBe(false)
  })
})

describe('localeChangePayload', () => {
  const current = { timezoneId: 'Asia/Taipei', primaryLanguage: 'zh-TW' }

  it('sends only the field that actually moved', () => {
    expect(localeChangePayload(current, {
      timezoneId: 'Asia/Tokyo',
      primaryLanguage: 'zh-TW',
    })).toEqual({ timezone_id: 'Asia/Tokyo' })

    expect(localeChangePayload(current, {
      timezoneId: 'Asia/Taipei',
      primaryLanguage: 'ja-JP',
    })).toEqual({ primary_language: 'ja-JP' })
  })

  it('sends both when both moved', () => {
    expect(localeChangePayload(current, {
      timezoneId: 'Asia/Tokyo',
      primaryLanguage: 'ja-JP',
    })).toEqual({ timezone_id: 'Asia/Tokyo', primary_language: 'ja-JP' })
  })

  it('returns null for a no-op so a double submit cannot spend a change', () => {
    expect(localeChangePayload(current, { ...current })).toBeNull()
    expect(localeChangePayload(current, {
      timezoneId: '  Asia/Taipei  ',
      primaryLanguage: 'zh-TW',
    })).toBeNull()
  })

  it('ignores an empty draft field rather than clearing the stored value', () => {
    expect(localeChangePayload(current, {
      timezoneId: '',
      primaryLanguage: '',
    })).toBeNull()
  })
})

describe('localeChangeSubject', () => {
  const current = { timezoneId: 'Asia/Taipei', primaryLanguage: 'zh-TW' }

  it('names what the warning must talk about', () => {
    expect(localeChangeSubject(current, {
      timezoneId: 'Asia/Tokyo',
      primaryLanguage: 'zh-TW',
    })).toBe('timezone')
    expect(localeChangeSubject(current, {
      timezoneId: 'Asia/Taipei',
      primaryLanguage: 'en-US',
    })).toBe('language')
    expect(localeChangeSubject(current, {
      timezoneId: 'Asia/Tokyo',
      primaryLanguage: 'en-US',
    })).toBe('both')
    expect(localeChangeSubject(current, { ...current })).toBeNull()
  })
})

describe('localeConfirmFailureCopy', () => {
  it('sends an already-confirmed player to the settings fields, with the budget', () => {
    const copy = localeConfirmFailureCopy(new LocaleAlreadyConfirmedError({
      limit: 2,
      windowDays: 30,
    }))

    expect(copy?.key).toBe('playerLocale.confirm.alreadyConfirmed')
    expect(copy?.params).toEqual({ limit: 2, days: 30 })
    // The flag that keeps this gate up is stale — re-read the identity so a
    // confirmed player is not held behind a screen that can never succeed.
    expect(copy?.staleGate).toBe(true)
  })

  it('asks for a retry — not a different screen — while a write is in flight', () => {
    const copy = localeConfirmFailureCopy(new LocaleChangeInProgressError())

    expect(copy?.key).toBe('playerLocale.change.inProgress')
    expect(copy?.staleGate).toBe(false)
  })

  it('leaves every other failure on the generic path', () => {
    expect(localeConfirmFailureCopy(new Error('network down'))).toBeNull()
    // The guardrail cannot be hit by the free endpoint; if it somehow
    // arrives, it is not this gate's story to tell.
    expect(localeConfirmFailureCopy(new LocaleChangeLimitError({
      retryAt: null, limit: 2, windowDays: 30,
    }))).toBeNull()
    expect(localeConfirmFailureCopy(null)).toBeNull()
  })
})

describe('locale refusal copy', () => {
  // Two opposite instructions: "stop pressing this, it moved" versus
  // "press it again shortly". Sharing wording would strand a player.
  it('names the settings-side budget in all three locales', () => {
    for (const catalog of [zhTW, enUS, jaJP]) {
      expect(catalog.playerLocale.confirm.alreadyConfirmed).toContain('{limit}')
      expect(catalog.playerLocale.confirm.alreadyConfirmed).toContain('{days}')
    }
  })

  it('offers a retry rather than a failure while a change is in flight', () => {
    for (const catalog of [zhTW, enUS, jaJP]) {
      expect(catalog.playerLocale.change.inProgress).not.toBe(
        catalog.playerLocale.change.failed,
      )
      expect(catalog.playerLocale.change.inProgress).not.toBe(
        catalog.playerLocale.confirm.alreadyConfirmed,
      )
    }
    expect(zhTW.playerLocale.change.inProgress).toContain('稍後再試')
    expect(enUS.playerLocale.change.inProgress).toContain('try again')
    expect(jaJP.playerLocale.change.inProgress).toContain('もう一度')
  })
})

describe('irreversibility warning copy', () => {
  // The plan (§4.2) makes this wording a requirement, not a suggestion: a
  // player must know that nothing is rewritten and that today's schedule is
  // replanned once, in whichever language they read.
  it('names the setting being moved in all three locales', () => {
    for (const catalog of [zhTW, enUS, jaJP]) {
      expect(catalog.playerLocale.change.confirmBody).toContain('{subject}')
      expect(catalog.playerLocale.change.confirmTitle).toContain('{subject}')
    }
  })

  it('promises that past memories stay as they are', () => {
    expect(zhTW.playerLocale.change.confirmBody).toContain('不會跟著改變')
    expect(zhTW.playerLocale.change.confirmBody).toContain('重新安排')
    expect(enUS.playerLocale.change.confirmBody)
      .toContain('stay exactly as they are')
    expect(enUS.playerLocale.change.confirmBody).toContain('schedule')
    expect(jaJP.playerLocale.change.confirmBody).toContain('そのまま')
    expect(jaJP.playerLocale.change.confirmBody).toContain('組み直')
  })

  it('tells the player the change is limited, before they make one', () => {
    for (const catalog of [zhTW, enUS, jaJP]) {
      expect(catalog.playerLocale.change.limitHint).toContain('{limit}')
      expect(catalog.playerLocale.change.limitHint).toContain('{days}')
      expect(catalog.playerLocale.change.remaining).toContain('{count}')
      expect(catalog.playerLocale.change.limitReached).toContain('{time}')
    }
  })
})
