import { describe, expect, it } from 'vitest'

import {
  ARC_BEAT_COUNT_MAX,
  ARC_BEAT_COUNT_MIN,
  ARC_DURATION_MAX_DAYS,
  ARC_DURATION_MIN_DAYS,
  validateNewArcDraft,
} from '@/utils/storyArcValidation'

describe('validateNewArcDraft', () => {
  it('accepts the relaxed 3-day floor', () => {
    expect(validateNewArcDraft({ duration_days: 3, beat_count: 3 })).toBeNull()
  })

  it('rejects duration below the floor', () => {
    expect(validateNewArcDraft({ duration_days: 2, beat_count: 3 }))
      .toBe('durationOutOfRange')
  })

  it('rejects duration shorter than beat_count', () => {
    expect(validateNewArcDraft({ duration_days: 3, beat_count: 5 }))
      .toBe('durationShorterThanBeatCount')
  })

  it('accepts the 7 and 90 day boundaries', () => {
    expect(validateNewArcDraft({ duration_days: 7, beat_count: 5 })).toBeNull()
    expect(validateNewArcDraft({ duration_days: 90, beat_count: 7 })).toBeNull()
  })

  it('rejects duration above the ceiling', () => {
    expect(validateNewArcDraft({ duration_days: 91, beat_count: 3 }))
      .toBe('durationOutOfRange')
  })

  it('rejects beat_count outside its own bounds', () => {
    expect(validateNewArcDraft({ duration_days: 10, beat_count: 2 }))
      .toBe('beatCountOutOfRange')
    expect(validateNewArcDraft({ duration_days: 10, beat_count: 8 }))
      .toBe('beatCountOutOfRange')
  })

  it('rejects non-finite input defensively', () => {
    expect(validateNewArcDraft({ duration_days: NaN, beat_count: 3 }))
      .toBe('durationOutOfRange')
  })

  it('exposes the bounds constants matching backend DTO', () => {
    expect(ARC_DURATION_MIN_DAYS).toBe(3)
    expect(ARC_DURATION_MAX_DAYS).toBe(90)
    expect(ARC_BEAT_COUNT_MIN).toBe(3)
    expect(ARC_BEAT_COUNT_MAX).toBe(7)
  })
})
