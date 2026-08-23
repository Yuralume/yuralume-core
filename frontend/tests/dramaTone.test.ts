/**
 * BD11 — the player-facing label for a drama's tone branch.
 *
 * The wire value is the backend's closed English enum (`dark` / `sunny` /
 * `neutral`), which used to render raw in the player's tone tag and in the
 * exported transcript — unexplained English inside an otherwise-localized
 * UI. Two things worth pinning here, both silent when broken: every tone in
 * `DRAMA_TONES` has a label in every shipped locale (a missing key renders
 * as the raw i18n key), and `dramaToneLabel` falls back to the raw value
 * instead of throwing when handed something outside the known set.
 */
import { describe, expect, it } from 'vitest'

import { dramaToneLabel } from '@/utils/dramaTone'
import { DRAMA_TONES } from '@/types/branchingDrama'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

function makeT(catalog: Record<string, any>): (key: string) => string {
  return (key: string) => {
    const value = key.split('.').reduce((node, part) => node?.[part], catalog)
    return typeof value === 'string' ? value : key
  }
}

describe('tone vocabulary', () => {
  it('is the closed three-way branch', () => {
    expect(DRAMA_TONES).toEqual(['dark', 'sunny', 'neutral'])
  })

  it.each([
    ['zh-TW', zhTW],
    ['en-US', enUS],
    ['ja-JP', jaJP],
  ])('has a translated label for every tone in %s', (_tag, catalog) => {
    const tone = (catalog as Record<string, any>).branchingDrama.tone
    for (const value of DRAMA_TONES) {
      expect(tone[value]).toBeTruthy()
      // Never the raw English enum value left untranslated.
      expect(tone[value]).not.toBe(value)
    }
  })
})

describe('dramaToneLabel', () => {
  it('translates a known tone through the given t()', () => {
    const t = makeT(zhTW as unknown as Record<string, any>)
    expect(dramaToneLabel('dark', t)).toBe(zhTW.branchingDrama.tone.dark)
    expect(dramaToneLabel('sunny', t)).toBe(zhTW.branchingDrama.tone.sunny)
    expect(dramaToneLabel('neutral', t)).toBe(zhTW.branchingDrama.tone.neutral)
  })

  it('returns an empty string for null/undefined rather than a dash or the key', () => {
    const t = makeT(zhTW as unknown as Record<string, any>)
    expect(dramaToneLabel(null, t)).toBe('')
    expect(dramaToneLabel(undefined, t)).toBe('')
  })

  it('falls back to the raw value for a tone outside the known set', () => {
    // A future fourth tone should still show something instead of going
    // blank mid-playthrough or throwing.
    const t = makeT(zhTW as unknown as Record<string, any>)
    expect(dramaToneLabel('mysterious', t)).toBe('mysterious')
  })
})
