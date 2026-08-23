import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchCloudAnnouncements } from '@/utils/api/cloudAnnouncements'
import { useCloudAnnouncements } from '@/composables/useCloudAnnouncements'
import { notifyIdentityChanged } from '@/utils/identityLifecycle'
import { setDeploymentMode } from '@/composables/deploymentMode'

vi.mock('@/utils/api/cloudAnnouncements', () => ({
  fetchCloudAnnouncements: vi.fn(),
}))

const mockedFetch = vi.mocked(fetchCloudAnnouncements)

function snapshot(unreadCount = 1) {
  return {
    has_unread: unreadCount > 0,
    unread_count: unreadCount,
    latest_published_at: '2026-07-29T00:00:00Z',
    stale: false,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useCloudAnnouncements().reset()
  setDeploymentMode('cloud')
})

afterEach(() => {
  useCloudAnnouncements().reset()
})

describe('useCloudAnnouncements', () => {
  it('shows the dot once an unread standing is known', async () => {
    mockedFetch.mockResolvedValueOnce({ kind: 'ok', snapshot: snapshot(2) })
    const announcements = useCloudAnnouncements()

    await announcements.ensureLoaded()

    expect(announcements.hasUnread.value).toBe(true)
    expect(announcements.unreadCount.value).toBe(2)
  })

  it('stays retryable after a first read that failed', async () => {
    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })
    const announcements = useCloudAnnouncements()

    await announcements.ensureLoaded()
    expect(announcements.hasUnread.value).toBe(false)

    // The critical part: a degraded first read must NOT mark the state loaded.
    // An absent snapshot renders as "nothing unread", so latching here would
    // hide every notice for the rest of the session.
    mockedFetch.mockResolvedValueOnce({ kind: 'ok', snapshot: snapshot(1) })
    await announcements.ensureLoaded()

    expect(mockedFetch).toHaveBeenCalledTimes(2)
    expect(announcements.hasUnread.value).toBe(true)
  })

  it('keeps the dot up when a later read degrades', async () => {
    mockedFetch
      .mockResolvedValueOnce({ kind: 'ok', snapshot: snapshot(1) })
      .mockResolvedValueOnce({ kind: 'degraded' })
    const announcements = useCloudAnnouncements()

    await announcements.ensureLoaded()
    announcements.refreshOnReturn()
    await vi.waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(2))

    // Clearing it on a failed read would hide a notice the operator had to give.
    expect(announcements.hasUnread.value).toBe(true)
  })

  it('stops asking once the deployment says it has no board', async () => {
    mockedFetch.mockResolvedValueOnce({ kind: 'unsupported' })
    const announcements = useCloudAnnouncements()

    await announcements.ensureLoaded()
    await announcements.ensureLoaded()
    announcements.refreshOnReturn()

    expect(mockedFetch).toHaveBeenCalledTimes(1)
    expect(announcements.hasUnread.value).toBe(false)
  })

  it('drops a read that belonged to the previous player', async () => {
    let release: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => {
      release = resolve
    })
    mockedFetch.mockImplementationOnce(async () => {
      await pending
      return { kind: 'ok', snapshot: snapshot(5) }
    })
    const announcements = useCloudAnnouncements()

    const inFlight = announcements.ensureLoaded()
    announcements.reset()
    release(null)
    await inFlight

    // A shared browser must never carry one player's unread state into another's
    // session, and clearing the refs alone does not stop an in-flight read.
    expect(announcements.hasUnread.value).toBe(false)
    expect(announcements.unreadCount.value).toBe(0)
  })

  it('clears itself when the signed-in identity changes', async () => {
    mockedFetch.mockResolvedValueOnce({ kind: 'ok', snapshot: snapshot(3) })
    const announcements = useCloudAnnouncements()

    await announcements.ensureLoaded()
    expect(announcements.hasUnread.value).toBe(true)

    notifyIdentityChanged()

    expect(announcements.hasUnread.value).toBe(false)
  })
})

describe('useCloudAnnouncements deployment gating', () => {
  it('asks nothing on self-host — there is no board and no route', async () => {
    setDeploymentMode('self_host')
    const announcements = useCloudAnnouncements()

    await announcements.ensureLoaded()
    announcements.refreshOnReturn()
    await Promise.resolve()

    expect(mockedFetch).not.toHaveBeenCalled()
    expect(announcements.hasUnread.value).toBe(false)
  })

  it('does not latch self-host: a late cloud probe still loads', async () => {
    setDeploymentMode('self_host')
    const announcements = useCloudAnnouncements()
    await announcements.ensureLoaded()

    setDeploymentMode('cloud')
    mockedFetch.mockResolvedValueOnce({ kind: 'ok', snapshot: snapshot(2) })
    await announcements.ensureLoaded()

    expect(announcements.unreadCount.value).toBe(2)
  })
})
