import { describe, expect, it } from 'vitest'

import {
  getExplicitStageLayout,
  resolveStageLayout,
  setExplicitStageLayout,
  stageLayoutKey,
} from '@/utils/stageLayout'

function fakeStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  } satisfies Pick<Storage, 'getItem' | 'setItem'>
}

describe('stageLayoutKey', () => {
  it('namespaces the key per character id', () => {
    expect(stageLayoutKey('char-1')).toBe('kokoro.stageLayout.char-1')
  })
})

describe('getExplicitStageLayout', () => {
  it('returns null when there is no storage', () => {
    expect(getExplicitStageLayout(null, 'char-1')).toBe(null)
  })

  it('returns null when there is no characterId', () => {
    expect(getExplicitStageLayout(fakeStorage(), null)).toBe(null)
    expect(getExplicitStageLayout(fakeStorage(), undefined)).toBe(null)
  })

  it('returns null when no preference has been set yet', () => {
    expect(getExplicitStageLayout(fakeStorage(), 'char-1')).toBe(null)
  })

  it('returns the stored preference once set', () => {
    const storage = fakeStorage()
    expect(setExplicitStageLayout(storage, 'char-1', 'chat-centric')).toBe(true)
    expect(getExplicitStageLayout(storage, 'char-1')).toBe('chat-centric')
  })

  it('is per-character: setting one character does not affect another', () => {
    const storage = fakeStorage()
    setExplicitStageLayout(storage, 'char-1', 'chat-centric')
    expect(getExplicitStageLayout(storage, 'char-2')).toBe(null)
  })

  it('fails soft (returns null) when storage throws', () => {
    const throwingStorage = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    } satisfies Pick<Storage, 'getItem' | 'setItem'>

    expect(getExplicitStageLayout(throwingStorage, 'char-1')).toBe(null)
  })
})

describe('setExplicitStageLayout', () => {
  it('returns true on a normal write', () => {
    expect(setExplicitStageLayout(fakeStorage(), 'char-1', 'stage-centric')).toBe(true)
  })

  it('returns false without storage or characterId', () => {
    expect(setExplicitStageLayout(null, 'char-1', 'stage-centric')).toBe(false)
    expect(setExplicitStageLayout(fakeStorage(), null, 'stage-centric')).toBe(false)
  })

  it('returns false (does not throw) when storage throws', () => {
    const throwingStorage = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    } satisfies Pick<Storage, 'getItem' | 'setItem'>

    expect(setExplicitStageLayout(throwingStorage, 'char-1', 'chat-centric')).toBe(false)
  })
})

describe('resolveStageLayout', () => {
  it('returns the explicit preference when set, regardless of hasImages', () => {
    expect(resolveStageLayout({ explicit: 'chat-centric', hasImages: true })).toBe('chat-centric')
    expect(resolveStageLayout({ explicit: 'stage-centric', hasImages: false })).toBe('stage-centric')
  })

  it('auto rule: no explicit preference and no images -> chat-centric', () => {
    expect(resolveStageLayout({ explicit: null, hasImages: false })).toBe('chat-centric')
  })

  it('auto rule: no explicit preference and has images -> stage-centric', () => {
    expect(resolveStageLayout({ explicit: null, hasImages: true })).toBe('stage-centric')
  })
})
