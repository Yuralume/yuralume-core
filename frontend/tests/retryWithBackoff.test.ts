import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  isRetryAborted,
  retryWithBackoff,
  RetryAbortedError,
} from '@/utils/retryWithBackoff'

class StatusError extends Error {
  statusCode: number
  constructor(statusCode: number) {
    super(`status ${statusCode}`)
    this.statusCode = statusCode
  }
}

const is409 = (error: unknown): boolean =>
  error instanceof StatusError && error.statusCode === 409

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('retryWithBackoff', () => {
  it('returns the result on the first try without waiting', async () => {
    const run = vi.fn(async () => 'ok')

    const result = await retryWithBackoff(run, { isRetryable: is409 })

    expect(result).toBe('ok')
    expect(run).toHaveBeenCalledTimes(1)
  })

  it('retries a retryable failure and succeeds once it clears', async () => {
    const run = vi.fn()
      .mockRejectedValueOnce(new StatusError(409))
      .mockRejectedValueOnce(new StatusError(409))
      .mockResolvedValueOnce('ready')
    const onRetry = vi.fn()

    const done = retryWithBackoff(run, {
      isRetryable: is409,
      initialDelayMs: 1000,
      multiplier: 1,
      onRetry,
    })

    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(1000)
    await vi.advanceTimersByTimeAsync(1000)

    await expect(done).resolves.toBe('ready')
    expect(run).toHaveBeenCalledTimes(3)
    expect(onRetry).toHaveBeenCalledTimes(2)
    expect(onRetry).toHaveBeenNthCalledWith(1, 1, 1000)
    expect(onRetry).toHaveBeenNthCalledWith(2, 2, 1000)
  })

  it('rejects immediately on a non-retryable error, without waiting', async () => {
    const run = vi.fn().mockRejectedValueOnce(new StatusError(500))

    await expect(
      retryWithBackoff(run, { isRetryable: is409 }),
    ).rejects.toMatchObject({ statusCode: 500 })
    expect(run).toHaveBeenCalledTimes(1)
  })

  it('grows the delay by the multiplier up to the cap', async () => {
    const run = vi.fn()
      .mockRejectedValueOnce(new StatusError(409))
      .mockRejectedValueOnce(new StatusError(409))
      .mockRejectedValueOnce(new StatusError(409))
      .mockResolvedValueOnce('ready')
    const onRetry = vi.fn()

    const done = retryWithBackoff(run, {
      isRetryable: is409,
      initialDelayMs: 1000,
      maxDelayMs: 1500,
      multiplier: 2,
      totalWindowMs: 60000,
      onRetry,
    })

    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(1000) // attempt 1 delay
    await vi.advanceTimersByTimeAsync(1500) // attempt 2 delay, capped
    await vi.advanceTimersByTimeAsync(1500) // attempt 3 delay, still capped

    await expect(done).resolves.toBe('ready')
    expect(onRetry).toHaveBeenNthCalledWith(1, 1, 1000)
    expect(onRetry).toHaveBeenNthCalledWith(2, 2, 1500)
    expect(onRetry).toHaveBeenNthCalledWith(3, 3, 1500)
  })

  it('gives up once the next wait would exceed the total window', async () => {
    const run = vi.fn().mockRejectedValue(new StatusError(409))

    const done = retryWithBackoff(run, {
      isRetryable: is409,
      initialDelayMs: 10000,
      multiplier: 1,
      totalWindowMs: 15000,
    })
    const assertion = expect(done).rejects.toMatchObject({ statusCode: 409 })

    // First retry: elapsed(0) + delay(10000) <= 15000 -> waits.
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(10000)
    // Second retry: elapsed(10000) + delay(10000) > 15000 -> gives up.
    await assertion

    expect(run).toHaveBeenCalledTimes(2)
  })
})

/**
 * The window is up to a minute long, so "the player left" is an ordinary
 * event inside it — not an edge case. An uncancellable loop keeps a timer and
 * a pending request alive past the screen that asked for them, and its
 * eventual rejection lands on a component that is gone.
 */
describe('retryWithBackoff cancellation', () => {
  it('stops mid-wait and never runs the next attempt', async () => {
    const run = vi.fn().mockRejectedValue(new StatusError(409))
    const controller = new AbortController()

    const done = retryWithBackoff(run, {
      isRetryable: is409,
      initialDelayMs: 5000,
      multiplier: 1,
      signal: controller.signal,
    })
    const assertion = expect(done).rejects.toBeInstanceOf(RetryAbortedError)

    await vi.advanceTimersByTimeAsync(0)
    expect(run).toHaveBeenCalledTimes(1)

    controller.abort()
    await assertion

    // The wait it was sitting in never turns into another call.
    await vi.advanceTimersByTimeAsync(60000)
    expect(run).toHaveBeenCalledTimes(1)
  })

  it('refuses to start at all when the signal is already aborted', async () => {
    const run = vi.fn(async () => 'ok')
    const controller = new AbortController()
    controller.abort()

    await expect(
      retryWithBackoff(run, { isRetryable: is409, signal: controller.signal }),
    ).rejects.toBeInstanceOf(RetryAbortedError)
    expect(run).not.toHaveBeenCalled()
  })

  it('reports an abort that landed while the request was in flight', async () => {
    // The player left during the call, not during the wait: the answer is
    // still owed to nobody, so it must not be retried or reported either.
    const controller = new AbortController()
    const run = vi.fn(async () => {
      controller.abort()
      throw new StatusError(409)
    })

    const done = retryWithBackoff(run, {
      isRetryable: is409,
      initialDelayMs: 1000,
      signal: controller.signal,
    })
    const assertion = expect(done).rejects.toBeInstanceOf(RetryAbortedError)
    await vi.advanceTimersByTimeAsync(0)
    await assertion

    expect(run).toHaveBeenCalledTimes(1)
  })

  it('is recognisable to the caller so it can be swallowed', async () => {
    // The player who walked away is owed no error message.
    expect(isRetryAborted(new RetryAbortedError())).toBe(true)
    expect(isRetryAborted(new StatusError(409))).toBe(false)
  })

  it('leaves an unsignalled call exactly as it was', async () => {
    const run = vi.fn()
      .mockRejectedValueOnce(new StatusError(409))
      .mockResolvedValueOnce('ready')

    const done = retryWithBackoff(run, {
      isRetryable: is409,
      initialDelayMs: 1000,
      multiplier: 1,
    })
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(1000)

    await expect(done).resolves.toBe('ready')
  })
})
