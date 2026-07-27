import { describe, expect, it } from 'vitest'

import { CLOUD_COPY_SUFFIX, resolveCopyKey } from '@/utils/playerCopy'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

const CATALOGS = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP }

function flatten(node: unknown, prefix = ''): [string, string][] {
  if (typeof node === 'string') return [[prefix, node]]
  if (!node || typeof node !== 'object') return []
  return Object.entries(node as Record<string, unknown>).flatMap(
    ([key, child]) => flatten(child, prefix ? `${prefix}.${key}` : key),
  )
}

describe('resolveCopyKey', () => {
  const has = (candidate: string) => candidate.endsWith(CLOUD_COPY_SUFFIX)

  it('keeps the original key on self-host even when a variant exists', () => {
    // The hard promise of plan U1: adding a *Cloud sibling can never change
    // a single self-host string.
    expect(resolveCopyKey('albumPanel.hint', false, has))
      .toBe('albumPanel.hint')
  })

  it('prefers the cloud sibling when hosted', () => {
    expect(resolveCopyKey('albumPanel.hint', true, has))
      .toBe('albumPanel.hintCloud')
  })

  it('falls back to the original when no cloud sibling was written', () => {
    // Half-migrated surfaces must render the operator wording rather than a
    // missing-key blank.
    expect(resolveCopyKey('albumPanel.title', true, () => false))
      .toBe('albumPanel.title')
  })

  it('never chains suffixes', () => {
    expect(resolveCopyKey('albumPanel.hintCloud', true, has))
      .toBe('albumPanel.hintCloud')
  })
})

describe('cloud copy variants', () => {
  it('exists in all three catalogs for every variant zh-TW defines', () => {
    const cloudKeys = flatten(zhTW)
      .map(([key]) => key)
      .filter((key) => key.endsWith(CLOUD_COPY_SUFFIX))

    expect(cloudKeys.length).toBeGreaterThan(20)

    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const present = new Set(flatten(catalog).map(([key]) => key))
      for (const key of cloudKeys) {
        expect(present.has(key), `${key} missing in ${locale}`).toBe(true)
      }
    }
  })

  it('always shadows a real key, never a typo', () => {
    // A *Cloud key whose base does not exist would silently never render.
    // The exceptions are the two places where the self-host copy is a
    // prefix/suffix pair wrapped around a server path: the hosted version
    // collapses that into one sentence, so it is selected by an explicit
    // `v-if="cloudMode"` in the template rather than by `pt()`.
    const collapsedPairs = new Set([
      'story.arcTemplatePicker.emptyCloud',
      'story.arcTemplateIntake.review.hintCloud',
    ])
    const all = new Set(flatten(zhTW).map(([key]) => key))
    for (const key of all) {
      if (!key.endsWith(CLOUD_COPY_SUFFIX)) continue
      if (collapsedPairs.has(key)) continue
      const base = key.slice(0, -CLOUD_COPY_SUFFIX.length)
      expect(all.has(base), `${key} shadows nothing`).toBe(true)
    }
  })

  it('drops the jargon the hosted variants exist to remove', () => {
    const banned = [
      'ComfyUI', 'LoRA', 'LLM', 'YAML', 'yaml', 'beat', 'arc ', 'Arc Template',
      'VAPID', 'API key', 'self-host', 'SillyTavern', 'lorebook', 'runtime',
      'premise（', 'prompt', 'metadata', '資料庫', 'memorialized',
    ]
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      for (const [key, value] of flatten(catalog)) {
        if (!key.endsWith(CLOUD_COPY_SUFFIX)) continue
        for (const word of banned) {
          expect(
            value.includes(word),
            `${locale} ${key} still says "${word}": ${value}`,
          ).toBe(false)
        }
      }
    }
  })
})
