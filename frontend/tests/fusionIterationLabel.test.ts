/**
 * FX6 — the player-facing label for a fusion story's version-history row.
 *
 * The wire value is the backend's own bookkeeping string (`polish`,
 * `beat_3_regenerate`, `restore_v2`, …), which used to render raw in the
 * version list: leaked English enum values inside an otherwise localized
 * panel, the same defect BD11 fixed for drama tones.
 *
 * Three things are pinned here, all silent when broken: every label the
 * backend can emit resolves to real copy in every shipped locale (a missing
 * key renders as the raw i18n key), the two numbered forms carry their
 * number through (`beat_0_regenerate` is the *first* section, not the
 * zeroth), and an unrecognized label falls back to the raw value instead of
 * blanking the cell.
 */
import { describe, expect, it } from 'vitest'

import { fusionIterationLabel } from '@/utils/fusionIterationLabel'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

/**
 * Minimal `t` — resolves the dotted key against a catalogue and interpolates
 * `{named}` placeholders the way vue-i18n would, so a message that forgets
 * its parameter shows up as a leftover brace rather than passing silently.
 */
function makeT(catalog: Record<string, unknown>) {
  return (key: string, named?: Record<string, unknown>): string => {
    const value = key
      .split('.')
      .reduce<any>((node, part) => node?.[part], catalog)
    if (typeof value !== 'string') return key
    if (!named) return value
    return value.replace(
      /\{(\w+)\}/g,
      (whole, name: string) => (name in named ? String(named[name]) : whole),
    )
  }
}

const LOCALES = [
  ['zh-TW', zhTW],
  ['en-US', enUS],
  ['ja-JP', jaJP],
] as const

/** Exactly what `FusionStoryService` / `snapshot_version` can write. */
const EMITTED_LABELS = [
  'outline_regenerate',
  'polish',
  'iterate',
  'beat_0_regenerate',
  'beat_11_regenerate',
  'restore_v1',
  'restore_v42',
]

describe('fusionIterationLabel', () => {
  it.each(LOCALES)(
    'never leaves a backend label untranslated in %s',
    (_tag, catalog) => {
      const t = makeT(catalog as unknown as Record<string, unknown>)
      for (const label of EMITTED_LABELS) {
        const rendered = fusionIterationLabel(label, t)
        expect(rendered).toBeTruthy()
        // Neither the raw enum value nor an unresolved i18n key.
        expect(rendered).not.toBe(label)
        expect(rendered).not.toContain('fusionStory.viewer')
        // No placeholder left behind by a message missing its parameter.
        expect(rendered).not.toMatch(/\{\w+\}/)
      }
    },
  )

  it('numbers beats from one, because the wire index is zero-based', () => {
    const t = makeT(zhTW as unknown as Record<string, unknown>)

    expect(fusionIterationLabel('beat_0_regenerate', t)).toContain('1')
    expect(fusionIterationLabel('beat_4_regenerate', t)).toContain('5')
    // Multi-digit indices must not be truncated by the pattern.
    expect(fusionIterationLabel('beat_12_regenerate', t)).toContain('13')
  })

  it('carries the restored version number through unchanged', () => {
    // Unlike the beat index this one is already the number the player sees
    // next to the row ("v7") — it names the version restored *to* — so it
    // must NOT be shifted.
    const t = makeT(zhTW as unknown as Record<string, unknown>)

    expect(fusionIterationLabel('restore_v7', t)).toContain('7')
    expect(fusionIterationLabel('restore_v7', t)).not.toContain('8')
  })

  it('returns an empty string for null/undefined rather than a key', () => {
    const t = makeT(zhTW as unknown as Record<string, unknown>)

    expect(fusionIterationLabel(null, t)).toBe('')
    expect(fusionIterationLabel(undefined, t)).toBe('')
    expect(fusionIterationLabel('', t)).toBe('')
  })

  it('falls back to the raw value for a label outside the known set', () => {
    // A future iterate operation should still show something in the history
    // list instead of a blank cell or a thrown render.
    const t = makeT(zhTW as unknown as Record<string, unknown>)

    expect(fusionIterationLabel('critic_pass', t)).toBe('critic_pass')
    // Near-misses must not be parsed as the numbered forms.
    expect(fusionIterationLabel('beat_x_regenerate', t)).toBe(
      'beat_x_regenerate',
    )
    expect(fusionIterationLabel('restore_vX', t)).toBe('restore_vX')
  })

  it('keeps the label catalogue identical across all three locales', () => {
    const keysOf = (value: unknown): string[] =>
      Object.keys(value as Record<string, unknown>).sort()

    const zhKeys = keysOf(zhTW.fusionStory.viewer.versionsPanel.iterationLabels)
    expect(keysOf(enUS.fusionStory.viewer.versionsPanel.iterationLabels))
      .toEqual(zhKeys)
    expect(keysOf(jaJP.fusionStory.viewer.versionsPanel.iterationLabels))
      .toEqual(zhKeys)
  })
})
