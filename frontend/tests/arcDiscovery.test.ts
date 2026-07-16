import { describe, expect, it } from 'vitest'

import {
  ARC_DISCOVERY_STUDIO_COACHMARK_KEY,
  STUDIO_EXIT_HUB_COACHMARK_KEY,
  arcDiscoveryDismissKey,
  hasArcDiscoveryBinding,
  isArcDiscoveryDismissed,
  isExitHubCoachmarkDismissed,
  isStudioCoachmarkDismissed,
  rememberArcDiscoveryDismissed,
  rememberExitHubCoachmarkDismissed,
  rememberStudioCoachmarkDismissed,
  shouldShowArcDiscovery,
} from '@/utils/arcDiscovery'

function character(overrides: {
  id?: string | null
  arc_template_id?: string | null
  arc_series_id?: string | null
} = {}) {
  return {
    id: 'char-1',
    arc_template_id: null,
    arc_series_id: null,
    ...overrides,
  }
}

function fakeStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  } satisfies Pick<Storage, 'getItem' | 'setItem'>
}

describe('shouldShowArcDiscovery', () => {
  it('shows for a selected character with no active arc, no binding, and no dismissal', () => {
    expect(shouldShowArcDiscovery({
      character: character(),
      hasActiveArc: false,
      dismissed: false,
    })).toBe(true)
  })

  it('hides when there is no selected character id', () => {
    expect(shouldShowArcDiscovery({
      character: character({ id: null }),
      hasActiveArc: false,
      dismissed: false,
    })).toBe(false)
  })

  it('hides when an active story arc exists', () => {
    expect(shouldShowArcDiscovery({
      character: character(),
      hasActiveArc: true,
      dismissed: false,
    })).toBe(false)
  })

  it('hides when a template or series is already bound', () => {
    expect(hasArcDiscoveryBinding(character({ arc_template_id: 'tpl-1' }))).toBe(true)
    expect(hasArcDiscoveryBinding(character({ arc_series_id: 'series-1' }))).toBe(true)
    expect(shouldShowArcDiscovery({
      character: character({ arc_template_id: 'tpl-1' }),
      hasActiveArc: false,
      dismissed: false,
    })).toBe(false)
    expect(shouldShowArcDiscovery({
      character: character({ arc_series_id: 'series-1' }),
      hasActiveArc: false,
      dismissed: false,
    })).toBe(false)
  })

  it('hides after the character-specific guide has been dismissed', () => {
    expect(shouldShowArcDiscovery({
      character: character(),
      hasActiveArc: false,
      dismissed: true,
    })).toBe(false)
  })
})

describe('arc discovery dismissal storage', () => {
  it('stores dismissal per character', () => {
    const storage = fakeStorage()

    expect(isArcDiscoveryDismissed(storage, 'char-1')).toBe(false)
    expect(rememberArcDiscoveryDismissed(storage, 'char-1')).toBe(true)
    expect(isArcDiscoveryDismissed(storage, 'char-1')).toBe(true)
    expect(isArcDiscoveryDismissed(storage, 'char-2')).toBe(false)
    expect(arcDiscoveryDismissKey('char-1')).toBe('yuralume.arcDiscovery.dismissed.char-1')
  })

  it('fails soft when localStorage is unavailable or throws', () => {
    const throwingStorage = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    } satisfies Pick<Storage, 'getItem' | 'setItem'>

    expect(isArcDiscoveryDismissed(null, 'char-1')).toBe(false)
    expect(rememberArcDiscoveryDismissed(null, 'char-1')).toBe(false)
    expect(isArcDiscoveryDismissed(throwingStorage, 'char-1')).toBe(false)
    expect(rememberArcDiscoveryDismissed(throwingStorage, 'char-1')).toBe(false)
  })
})

describe('studio coachmark dismissal storage', () => {
  it('stores the user-wide Creator Studio coachmark dismissal', () => {
    const storage = fakeStorage()

    expect(isStudioCoachmarkDismissed(storage)).toBe(false)
    expect(rememberStudioCoachmarkDismissed(storage)).toBe(true)
    expect(isStudioCoachmarkDismissed(storage)).toBe(true)
    expect(storage.getItem(ARC_DISCOVERY_STUDIO_COACHMARK_KEY)).toBe('1')
  })

  it('fails soft when the studio coachmark storage is unavailable or throws', () => {
    const throwingStorage = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    } satisfies Pick<Storage, 'getItem' | 'setItem'>

    expect(isStudioCoachmarkDismissed(null)).toBe(false)
    expect(rememberStudioCoachmarkDismissed(null)).toBe(false)
    expect(isStudioCoachmarkDismissed(throwingStorage)).toBe(false)
    expect(rememberStudioCoachmarkDismissed(throwingStorage)).toBe(false)
  })
})

describe('exit hub coachmark dismissal storage', () => {
  it('flips the predicate once dismissed (so the hub coachmark hides)', () => {
    const storage = fakeStorage()

    expect(isExitHubCoachmarkDismissed(storage)).toBe(false)
    expect(rememberExitHubCoachmarkDismissed(storage)).toBe(true)
    expect(isExitHubCoachmarkDismissed(storage)).toBe(true)
    expect(storage.getItem(STUDIO_EXIT_HUB_COACHMARK_KEY)).toBe('1')
  })

  it('uses a distinct key from the studio launcher coachmark', () => {
    expect(STUDIO_EXIT_HUB_COACHMARK_KEY).not.toBe(
      ARC_DISCOVERY_STUDIO_COACHMARK_KEY,
    )
  })

  it('fails soft when storage is unavailable or throws', () => {
    const throwingStorage = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    } satisfies Pick<Storage, 'getItem' | 'setItem'>

    expect(isExitHubCoachmarkDismissed(null)).toBe(false)
    expect(rememberExitHubCoachmarkDismissed(null)).toBe(false)
    expect(isExitHubCoachmarkDismissed(throwingStorage)).toBe(false)
    expect(rememberExitHubCoachmarkDismissed(throwingStorage)).toBe(false)
  })
})
