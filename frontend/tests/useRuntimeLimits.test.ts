import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setDeploymentMode } from '@/composables/deploymentMode'

import type {
  RuntimeLimitsResult,
  RuntimeLimitsSnapshot,
} from '@/utils/api/cloudLimits'

/**
 * The hosted-limits singleton, exercised through its transport seam.
 *
 * Three promises are asserted here, in this order of importance:
 *   1. an advisory is only shown when it is *certain* (a known count at or
 *      over a known ceiling) — every unknown resolves to "say nothing";
 *   2. feature switches fail open, so a bad read never takes a feature away;
 *   3. self-host issues zero requests and reports `supported === false`, so
 *      every consumer's `v-if` collapses.
 */

// The deployment mode is a module-scope ref of its own (see
// `@/composables/deploymentMode`), which is what lets each test pick the shape
// of the deployment before the composable reads it — and, because it really is
// a ref, lets the `supported` computed re-evaluate when a late probe flips it.
vi.mock('@/utils/api/cloudLimits', () => ({
  fetchRuntimeLimits: vi.fn(),
}))

const { fetchRuntimeLimits } = await import('@/utils/api/cloudLimits')
const { notifyIdentityChanged } = await import('@/utils/identityLifecycle')
const { useRuntimeLimits } = await import('@/composables/useRuntimeLimits')

const mockedFetch = vi.mocked(fetchRuntimeLimits)

function snapshot(
  overrides: Partial<RuntimeLimitsSnapshot> = {},
): RuntimeLimitsResult {
  return {
    kind: 'ok',
    snapshot: {
      character_slots: null,
      character_daily_creates: null,
      story_scenes_daily: null,
      session_message_limit: null,
      album_generation_enabled: true,
      tts_enabled: true,
      video_generation_enabled: true,
      background: null,
      ...overrides,
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  setDeploymentMode('cloud')
  useRuntimeLimits().reset()
})

describe('useRuntimeLimits state machine', () => {
  it('exposes the counters once a snapshot arrives', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 2, limit: 3 },
      character_daily_creates: { used: 0, limit: 1 },
      story_scenes_daily: { used: 1, limit: 5 },
      session_message_limit: 80,
    }))

    await limits.ensureLoaded()

    expect(limits.supported.value).toBe(true)
    expect(limits.loaded.value).toBe(true)
    expect(limits.characterSlots.value).toEqual({ used: 2, limit: 3 })
    expect(limits.characterDailyCreates.value).toEqual({ used: 0, limit: 1 })
    expect(limits.storyScenesDaily.value).toEqual({ used: 1, limit: 5 })
    expect(mockedFetch).toHaveBeenCalledWith()
  })

  it('reports an uncapped item as null rather than a fake ceiling', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({ character_slots: null }))

    await limits.ensureLoaded()

    expect(limits.characterSlots.value).toBeNull()
  })

  it('loads once — a second ensureLoaded does not re-request', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot())

    await limits.ensureLoaded()
    await limits.ensureLoaded()

    expect(mockedFetch).toHaveBeenCalledTimes(1)
  })

  it('refresh re-reads so a just-created character moves the counter', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 1, limit: 3 },
    }))
    await limits.ensureLoaded()

    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 2, limit: 3 },
    }))
    await limits.refresh()

    expect(mockedFetch).toHaveBeenCalledTimes(2)
    expect(limits.characterSlots.value).toEqual({ used: 2, limit: 3 })
  })

  it('collapses concurrent ensureLoaded calls onto one request', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValue(snapshot())

    await Promise.all([limits.ensureLoaded(), limits.ensureLoaded()])

    expect(mockedFetch).toHaveBeenCalledTimes(1)
  })

  it('refresh never collapses onto an in-flight read', async () => {
    // A refresh is triggered by an event (a create, a scene opened) that the
    // in-flight read predates — collapsing would resolve the refresh with a
    // count from before the consumption. The newest request's answer wins
    // even when the stale read settles last.
    const limits = useRuntimeLimits()
    let settleFirst: (result: RuntimeLimitsResult) => void = () => {}
    mockedFetch.mockReturnValueOnce(new Promise((resolve) => {
      settleFirst = resolve
    }))
    const first = limits.ensureLoaded()

    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 2, limit: 3 },
    }))
    const second = limits.refresh()

    settleFirst(snapshot({ character_slots: { used: 1, limit: 3 } }))
    await Promise.all([first, second])

    expect(mockedFetch).toHaveBeenCalledTimes(2)
    expect(limits.characterSlots.value).toEqual({ used: 2, limit: 3 })
  })

  it('a degraded first read does not latch: the next ensureLoaded retries', async () => {
    // One transient blip at page open must not silence every limit hint for
    // the whole session — `loaded` is only set by an answer with content.
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })
    await limits.ensureLoaded()

    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 2, limit: 3 },
    }))
    await limits.ensureLoaded()

    expect(mockedFetch).toHaveBeenCalledTimes(2)
    expect(limits.characterSlots.value).toEqual({ used: 2, limit: 3 })
  })

  it('goes permanently quiet on 404 (self-host / no hosted profile)', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce({ kind: 'unsupported' })

    await limits.ensureLoaded()

    expect(limits.supported.value).toBe(false)
    expect(limits.characterSlots.value).toBeNull()
    // …and nothing probes again: an operator without a hosted profile will
    // not grow one mid-session.
    mockedFetch.mockClear()
    await limits.refresh()
    expect(mockedFetch).not.toHaveBeenCalled()
  })

  it('claims nothing when the read degrades', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })

    await limits.ensureLoaded()

    // Still "supported" — the deployment is hosted, the value is just
    // unknown. Nothing may be rendered from it.
    expect(limits.supported.value).toBe(true)
    expect(limits.characterSlots.value).toBeNull()
    expect(limits.characterCreationBlocked.value).toBeNull()
  })

  it('keeps the previous snapshot when a later read degrades', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 1, limit: 3 },
    }))
    await limits.ensureLoaded()

    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })
    await limits.refresh()

    expect(limits.characterSlots.value).toEqual({ used: 1, limit: 3 })
  })
})

describe('useRuntimeLimits character-creation advisory', () => {
  async function loadWith(overrides: Partial<RuntimeLimitsSnapshot>) {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot(overrides))
    await limits.ensureLoaded()
    return limits
  }

  it('says nothing before anything is loaded', () => {
    expect(useRuntimeLimits().characterCreationBlocked.value).toBeNull()
  })

  it('flags a full roster', async () => {
    const limits = await loadWith({ character_slots: { used: 3, limit: 3 } })

    expect(limits.characterCreationBlocked.value).toBe('slots_full')
  })

  it('flags an exhausted daily window', async () => {
    const limits = await loadWith({
      character_slots: { used: 1, limit: 3 },
      character_daily_creates: { used: 1, limit: 1 },
    })

    expect(limits.characterCreationBlocked.value).toBe('daily_exhausted')
  })

  it('names both walls when both are up', async () => {
    // The everyday state of a tight profile (1 slot, 1 create/day). A slots-only
    // sentence promises that saying goodbye makes room, but a deletion never
    // refunds the daily ledger — the player would trade an irreversible
    // delete for a creation the server still refuses today.
    const limits = await loadWith({
      character_slots: { used: 3, limit: 3 },
      character_daily_creates: { used: 1, limit: 1 },
    })

    expect(limits.characterCreationBlocked.value).toBe('slots_and_daily')
  })

  it('stays silent below the ceiling', async () => {
    const limits = await loadWith({
      character_slots: { used: 2, limit: 3 },
      character_daily_creates: { used: 0, limit: 1 },
    })

    expect(limits.characterCreationBlocked.value).toBeNull()
  })

  it('never blocks on an unknown count, even at a known ceiling', async () => {
    // `used: null` is the backend's fail-soft: the ceiling is real, the tally
    // could not be read. Guessing "you are full" would cost the player a
    // character they are entitled to.
    const limits = await loadWith({
      character_slots: { used: null, limit: 3 },
      character_daily_creates: { used: null, limit: 1 },
    })

    expect(limits.characterCreationBlocked.value).toBeNull()
    expect(limits.characterSlots.value).toEqual({ used: null, limit: 3 })
  })

  it('flags a zero ceiling once a real count confirms it', async () => {
    const limits = await loadWith({
      character_daily_creates: { used: 0, limit: 0 },
    })

    expect(limits.characterCreationBlocked.value).toBe('daily_exhausted')
  })
})

describe('useRuntimeLimits feature switches', () => {
  it('defaults to enabled before anything is known', () => {
    const limits = useRuntimeLimits()

    expect(limits.albumGenerationEnabled.value).toBe(true)
    expect(limits.ttsEnabled.value).toBe(true)
  })

  it('stays enabled when the read degrades', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })

    await limits.ensureLoaded()

    expect(limits.albumGenerationEnabled.value).toBe(true)
    expect(limits.ttsEnabled.value).toBe(true)
  })

  it('stays enabled on self-host', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce({ kind: 'unsupported' })

    await limits.ensureLoaded()

    expect(limits.albumGenerationEnabled.value).toBe(true)
    expect(limits.ttsEnabled.value).toBe(true)
  })

  it('turns off only what a loaded snapshot explicitly denies', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({
      album_generation_enabled: false,
    }))

    await limits.ensureLoaded()

    expect(limits.albumGenerationEnabled.value).toBe(false)
    expect(limits.ttsEnabled.value).toBe(true)
  })
})

describe('useRuntimeLimits background dormancy (NF4/NF5)', () => {
  it('says nothing before anything is loaded', () => {
    expect(useRuntimeLimits().backgroundDormancyDays.value).toBeNull()
  })

  it('reports the dormancy window once a snapshot names one', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({
      background: { dormancyDays: 14 },
    }))

    await limits.ensureLoaded()

    expect(limits.backgroundDormancyDays.value).toBe(14)
  })

  it('stays null when the tier never goes dormant', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({
      background: { dormancyDays: null },
    }))

    await limits.ensureLoaded()

    expect(limits.backgroundDormancyDays.value).toBeNull()
  })

  it('stays null when the payload carried no background object at all', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({ background: null }))

    await limits.ensureLoaded()

    expect(limits.backgroundDormancyDays.value).toBeNull()
  })

  it('claims nothing when the read degrades', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })

    await limits.ensureLoaded()

    expect(limits.backgroundDormancyDays.value).toBeNull()
  })

  it('goes quiet on self-host (unsupported)', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce({ kind: 'unsupported' })

    await limits.ensureLoaded()

    expect(limits.backgroundDormancyDays.value).toBeNull()
  })

  it('hides again if the deployment stops reporting as cloud', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({
      background: { dormancyDays: 7 },
    }))
    await limits.ensureLoaded()
    expect(limits.backgroundDormancyDays.value).toBe(7)

    setDeploymentMode('self_host')

    expect(limits.backgroundDormancyDays.value).toBeNull()
  })
})

describe('useRuntimeLimits deployment gating', () => {
  it('issues no request at all on self-host', async () => {
    setDeploymentMode('self_host')
    const limits = useRuntimeLimits()

    await limits.ensureLoaded()
    await limits.refresh()

    expect(mockedFetch).not.toHaveBeenCalled()
    expect(limits.supported.value).toBe(false)
    expect(limits.characterCreationBlocked.value).toBeNull()
  })

  it('does not latch self-host: a late cloud probe still loads', async () => {
    // `useAuth` starts at `self_host` and flips only once the config probe
    // resolves; a component mounting first must not disable hosted hints
    // for the rest of the page's life.
    setDeploymentMode('self_host')
    const limits = useRuntimeLimits()
    await limits.ensureLoaded()

    setDeploymentMode('cloud')
    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 2, limit: 3 },
    }))
    await limits.ensureLoaded()

    expect(limits.characterSlots.value).toEqual({ used: 2, limit: 3 })
  })

  it('hides everything again if the deployment stops reporting as cloud', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 3, limit: 3 },
    }))
    await limits.ensureLoaded()

    setDeploymentMode('self_host')

    expect(limits.supported.value).toBe(false)
    expect(limits.characterSlots.value).toBeNull()
    expect(limits.characterCreationBlocked.value).toBeNull()
  })
})

describe('useRuntimeLimits identity binding', () => {
  /** A read the test can settle *after* the identity has moved on. */
  function deferredFetch() {
    let settle: (result: RuntimeLimitsResult) => void = () => {}
    const pending = new Promise<RuntimeLimitsResult>((resolve) => {
      settle = resolve
    })
    mockedFetch.mockReturnValueOnce(pending)
    return { settle }
  }

  it('clears on identity change without any component asking', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 3, limit: 3 },
    }))
    await limits.ensureLoaded()

    notifyIdentityChanged()

    expect(limits.characterSlots.value).toBeNull()
    expect(limits.characterCreationBlocked.value).toBeNull()
  })

  it('drops a read that lands after the player changed', async () => {
    const limits = useRuntimeLimits()
    const outgoing = deferredFetch()
    const first = limits.ensureLoaded()

    notifyIdentityChanged()
    const incoming = deferredFetch()
    const second = limits.ensureLoaded()

    // The stale read comes back last — the worst ordering.
    incoming.settle(snapshot({ character_slots: { used: 0, limit: 5 } }))
    outgoing.settle(snapshot({ character_slots: { used: 3, limit: 3 } }))
    await Promise.all([first, second])

    expect(limits.characterSlots.value).toEqual({ used: 0, limit: 5 })
  })

  it('re-probes for the next player after a profile-less operator', async () => {
    const limits = useRuntimeLimits()
    mockedFetch.mockResolvedValueOnce({ kind: 'unsupported' })
    await limits.ensureLoaded()

    notifyIdentityChanged()
    mockedFetch.mockResolvedValueOnce(snapshot({
      character_slots: { used: 1, limit: 2 },
    }))
    await limits.ensureLoaded()

    expect(limits.characterSlots.value).toEqual({ used: 1, limit: 2 })
  })
})
