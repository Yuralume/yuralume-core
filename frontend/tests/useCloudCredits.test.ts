import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchCloudCredits } from '@/utils/api/cloudCredits'
import type { CloudCreditsResult } from '@/utils/api/cloudCredits'
import { notifyIdentityChanged } from '@/utils/identityLifecycle'
import { setDeploymentMode } from '@/composables/deploymentMode'
import {
  LOW_BALANCE_THRESHOLD_CR,
  useCloudCredits,
} from '@/composables/useCloudCredits'

vi.mock('@/utils/api/cloudCredits', () => ({
  fetchCloudCredits: vi.fn(),
}))

const mockedFetch = vi.mocked(fetchCloudCredits)

function snapshot(total: number, stale = false) {
  return {
    kind: 'ok' as const,
    snapshot: {
      gift_cr: total / 2,
      purchased_cr: total / 2,
      total_cr: total,
      stale,
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useCloudCredits().reset()
  setDeploymentMode('cloud')
})

describe('useCloudCredits state machine', () => {
  it('starts hidden until a snapshot arrives', async () => {
    const credits = useCloudCredits()
    expect(credits.hasBalance.value).toBe(false)

    mockedFetch.mockResolvedValueOnce(snapshot(1200))
    await credits.ensureLoaded()

    expect(credits.hasBalance.value).toBe(true)
    expect(credits.total.value).toBe(1200)
    expect(credits.gift.value).toBe(600)
    expect(credits.purchased.value).toBe(600)
  })

  it('loads once — a second ensureLoaded does not re-request', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce(snapshot(500))

    await credits.ensureLoaded()
    await credits.ensureLoaded()

    expect(mockedFetch).toHaveBeenCalledTimes(1)
  })

  it('hides the badge permanently on 404 (no ledger for this operator)', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce(snapshot(900))
    await credits.ensureLoaded()

    mockedFetch.mockResolvedValueOnce({ kind: 'unsupported' })
    await credits.refresh()

    expect(credits.hasBalance.value).toBe(false)
    // …and no further reads are attempted: a self-host / tenant-less operator
    // will never grow a ledger mid-session.
    mockedFetch.mockClear()
    await credits.refresh()
    expect(mockedFetch).not.toHaveBeenCalled()
  })

  it('keeps the last-known-good value when the read degrades (503)', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce(snapshot(1500))
    await credits.ensureLoaded()

    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })
    await credits.refresh()

    // Never blink out to a fabricated zero — the player still sees 1500.
    expect(credits.hasBalance.value).toBe(true)
    expect(credits.total.value).toBe(1500)
  })

  it('stays hidden when the very first read degrades', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })

    await credits.ensureLoaded()

    expect(credits.hasBalance.value).toBe(false)
  })

  it('refresh bypasses the backend cache', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce(snapshot(300))
    await credits.ensureLoaded()
    expect(mockedFetch).toHaveBeenLastCalledWith({})

    mockedFetch.mockResolvedValueOnce(snapshot(280))
    await credits.refresh()

    expect(mockedFetch).toHaveBeenLastCalledWith({ refresh: true })
    expect(credits.total.value).toBe(280)
  })

  it('marks a stale snapshot so the badge can dim it', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce(snapshot(1000, true))

    await credits.ensureLoaded()

    expect(credits.stale.value).toBe(true)
  })

  it('flags a low balance below the owner-agreed threshold', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce(snapshot(LOW_BALANCE_THRESHOLD_CR - 1))
    await credits.ensureLoaded()
    expect(credits.lowBalance.value).toBe(true)

    mockedFetch.mockResolvedValueOnce(snapshot(LOW_BALANCE_THRESHOLD_CR))
    await credits.refresh()
    expect(credits.lowBalance.value).toBe(false)
  })

  it('never rejects from the post-action hook', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce(snapshot(400))
    await credits.ensureLoaded()

    mockedFetch.mockRejectedValueOnce(new Error('boom'))
    expect(() => credits.refreshAfterAction()).not.toThrow()
    await Promise.resolve()
  })

  it('collapses concurrent reads onto one request', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValue(snapshot(700))

    await Promise.all([credits.ensureLoaded(), credits.refresh()])

    expect(mockedFetch).toHaveBeenCalledTimes(1)
  })

  it('reset clears the balance so a shared browser does not leak it', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce(snapshot(2000))
    await credits.ensureLoaded()

    credits.reset()

    expect(credits.hasBalance.value).toBe(false)
    expect(credits.total.value).toBe(0)
  })
})

/**
 * The singleton is fed by an async read, so clearing the refs is only half
 * the job: a request already in the air when the player changed would
 * otherwise land afterwards and repaint the previous account's number.
 */
describe('useCloudCredits identity binding', () => {
  /** A read the test can resolve *after* the identity has moved on. */
  function deferredFetch() {
    let settle: (result: CloudCreditsResult) => void = () => {}
    const pending = new Promise<CloudCreditsResult>((resolve) => {
      settle = resolve
    })
    mockedFetch.mockReturnValueOnce(pending)
    return { settle }
  }

  it('drops a read that lands after a logout', async () => {
    const credits = useCloudCredits()
    const { settle } = deferredFetch()
    const read = credits.ensureLoaded()

    credits.reset()
    settle(snapshot(9000))
    await read

    expect(credits.hasBalance.value).toBe(false)
    expect(credits.total.value).toBe(0)
  })

  it('drops the previous account read when a second account signs in', async () => {
    const credits = useCloudCredits()
    const outgoing = deferredFetch()
    const first = credits.ensureLoaded()

    // The next player signs in while the first read is still in flight.
    notifyIdentityChanged()
    const incoming = deferredFetch()
    const second = credits.ensureLoaded()

    // The stale read comes back last — the worst ordering.
    incoming.settle(snapshot(120))
    outgoing.settle(snapshot(9000))
    await Promise.all([first, second])

    expect(credits.total.value).toBe(120)
  })

  it('a superseded read does not cancel the loading state of its successor', async () => {
    const credits = useCloudCredits()
    const outgoing = deferredFetch()
    const first = credits.ensureLoaded()

    notifyIdentityChanged()
    const incoming = deferredFetch()
    const second = credits.ensureLoaded()

    outgoing.settle(snapshot(9000))
    await first
    expect(credits.loading.value).toBe(true)

    incoming.settle(snapshot(120))
    await second
    expect(credits.loading.value).toBe(false)
  })

  it('an identity change clears the balance without any component asking', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce(snapshot(2000))
    await credits.ensureLoaded()

    notifyIdentityChanged()

    expect(credits.hasBalance.value).toBe(false)
  })

  it('re-probes for the next player after a ledger-less operator', async () => {
    const credits = useCloudCredits()
    mockedFetch.mockResolvedValueOnce({ kind: 'unsupported' })
    await credits.ensureLoaded()

    // "No ledger" was a fact about that operator, not about this browser.
    notifyIdentityChanged()
    mockedFetch.mockResolvedValueOnce(snapshot(640))
    await credits.ensureLoaded()

    expect(credits.total.value).toBe(640)
  })
})

describe('useCloudCredits deployment gating', () => {
  it('asks nothing on self-host — not on mount, not after a charged action', async () => {
    setDeploymentMode('self_host')
    const credits = useCloudCredits()

    await credits.ensureLoaded()
    // The badge guards its own mount, but this one is called from a dozen
    // completion points (chat, TTS, portraits, the branching drama) that have
    // no reason to know the deployment shape. Before the gate moved into
    // `load()`, each of those fired a real `GET /cloud/credits` that self-host
    // does not route, once per page load.
    credits.refreshAfterAction()
    await Promise.resolve()

    expect(mockedFetch).not.toHaveBeenCalled()
    expect(credits.hasBalance.value).toBe(false)
  })

  it('does not latch self-host: a late cloud probe still loads', async () => {
    setDeploymentMode('self_host')
    const credits = useCloudCredits()
    await credits.ensureLoaded()

    // `/auth/config` has landed — a badge that mounted before it must not be
    // stuck reporting no ledger for the rest of the session.
    setDeploymentMode('cloud')
    mockedFetch.mockResolvedValueOnce(snapshot(300))
    await credits.ensureLoaded()

    expect(credits.total.value).toBe(300)
  })
})
