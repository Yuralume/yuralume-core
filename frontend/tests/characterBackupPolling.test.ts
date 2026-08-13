import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { pollBackupJob } from '@/utils/characterBackupPolling'

interface FakeJob {
  status: 'running' | 'succeeded' | 'failed'
  n: number
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('pollBackupJob', () => {
  it('stops on the first terminal answer without a second read', async () => {
    const fetch = vi.fn(async (): Promise<FakeJob> => ({ status: 'succeeded', n: 1 }))
    const updates: FakeJob[] = []

    await pollBackupJob<FakeJob>({
      fetch,
      onUpdate: (job) => updates.push(job),
      isCancelled: () => false,
    })

    expect(fetch).toHaveBeenCalledTimes(1)
    expect(updates).toEqual([{ status: 'succeeded', n: 1 }])
  })

  it('keeps reading at the interval while the job is still running', async () => {
    let call = 0
    const fetch = vi.fn(async (): Promise<FakeJob> => {
      call += 1
      return call < 3 ? { status: 'running', n: call } : { status: 'succeeded', n: call }
    })
    const updates: FakeJob[] = []

    const done = pollBackupJob<FakeJob>({
      fetch,
      onUpdate: (job) => updates.push(job),
      isCancelled: () => false,
      intervalMs: 1000,
    })

    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(1000)
    await vi.advanceTimersByTimeAsync(1000)
    await done

    expect(fetch).toHaveBeenCalledTimes(3)
    expect(updates.map((u) => u.status)).toEqual(['running', 'running', 'succeeded'])
  })

  it('does not schedule a next read once cancelled', async () => {
    const fetch = vi.fn(async (): Promise<FakeJob> => ({ status: 'running', n: 1 }))
    let cancelled = false

    const done = pollBackupJob<FakeJob>({
      fetch,
      onUpdate: () => {},
      isCancelled: () => cancelled,
      intervalMs: 1000,
    })

    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(1)
    cancelled = true
    await vi.advanceTimersByTimeAsync(5000)
    await done

    // No second read: the sleep between reads checks cancellation before
    // scheduling the next one.
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('never applies an update read after cancellation flipped mid-flight', async () => {
    let resolveFetch: ((job: FakeJob) => void) | undefined
    const fetch = vi.fn(() => new Promise<FakeJob>((resolve) => { resolveFetch = resolve }))
    let cancelled = false
    const updates: FakeJob[] = []

    const done = pollBackupJob<FakeJob>({
      fetch,
      onUpdate: (job) => updates.push(job),
      isCancelled: () => cancelled,
    })

    cancelled = true
    resolveFetch?.({ status: 'succeeded', n: 1 })
    await done

    expect(updates).toEqual([])
  })

  it('retries a handful of transient failures before giving up', async () => {
    const fetch = vi.fn()
      .mockRejectedValueOnce(new Error('net-1'))
      .mockRejectedValueOnce(new Error('net-2'))
      .mockResolvedValueOnce({ status: 'succeeded', n: 1 } satisfies FakeJob)
    const updates: FakeJob[] = []

    const done = pollBackupJob<FakeJob>({
      fetch,
      onUpdate: (job) => updates.push(job),
      isCancelled: () => false,
      intervalMs: 1000,
      maxConsecutiveFailures: 5,
    })

    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(1000)
    await vi.advanceTimersByTimeAsync(1000)
    await done

    expect(fetch).toHaveBeenCalledTimes(3)
    expect(updates).toEqual([{ status: 'succeeded', n: 1 }])
  })

  it('gives up once the deployment looks truly unreachable', async () => {
    const fetch = vi.fn().mockRejectedValue(new Error('offline'))

    const done = pollBackupJob<FakeJob>({
      fetch,
      onUpdate: () => {},
      isCancelled: () => false,
      intervalMs: 1000,
      maxConsecutiveFailures: 3,
    })
    const assertion = expect(done).rejects.toThrow('offline')

    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(1000)
    await vi.advanceTimersByTimeAsync(1000)
    await assertion

    expect(fetch).toHaveBeenCalledTimes(3)
  })
})
