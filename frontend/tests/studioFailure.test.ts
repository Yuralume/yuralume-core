import { describe, expect, it } from 'vitest'

import {
  isGenericStudioFailure,
  isInsufficientCreditsFailure,
} from '@/utils/studioFailure'

describe('studio job failure routing (U4b)', () => {
  it('recognises a failed job the backend blamed on credits', () => {
    expect(isInsufficientCreditsFailure({
      status: 'failed',
      error_code: 'insufficient_credits',
    })).toBe(true)
  })

  it('leaves an ordinary crash (no code) on the generic path', () => {
    // Background crashes record no code at all; the existing failure copy
    // must keep handling them.
    expect(isInsufficientCreditsFailure({
      status: 'failed',
      error_code: null,
    })).toBe(false)
    expect(isGenericStudioFailure({ status: 'failed', error_code: null }))
      .toBe(true)
  })

  it('treats an unknown future code as a generic failure, not a blank card', () => {
    // error_code is an open set forwarded verbatim: a newer backend can ship
    // a code this build has never seen, and it must degrade quietly.
    const job = { status: 'failed', error_code: 'model_quarantined' }

    expect(isInsufficientCreditsFailure(job)).toBe(false)
    expect(isGenericStudioFailure(job)).toBe(true)
  })

  it('ignores a stale code on a job that is running again', () => {
    // Re-runs clear the code server-side, but the poll that races the reset
    // must never resurrect the top-up prompt over a live progress bar.
    expect(isInsufficientCreditsFailure({
      status: 'writing',
      error_code: 'insufficient_credits',
    })).toBe(false)
    expect(isGenericStudioFailure({
      status: 'writing',
      error_code: 'insufficient_credits',
    })).toBe(false)
  })

  it('is safe on a missing job', () => {
    expect(isInsufficientCreditsFailure(null)).toBe(false)
    expect(isInsufficientCreditsFailure(undefined)).toBe(false)
    expect(isGenericStudioFailure(null)).toBe(false)
  })

  it('handles a DTO from a backend that predates the field', () => {
    expect(isInsufficientCreditsFailure({ status: 'failed' })).toBe(false)
    expect(isGenericStudioFailure({ status: 'failed' })).toBe(true)
  })
})
