/**
 * Generic retry-with-backoff for a transient, retryable failure — as
 * opposed to `characterBackupPolling.ts`'s poll loop, which re-reads a
 * long-running job's *status* on a fixed interval. This retries the same
 * *action* after it failed for a reason the caller says is worth waiting
 * out.
 *
 * First user: the branching-drama player's `advance` action. The backend
 * route answers `409` while another replica is still generating the next
 * beat's outline layer (`BranchingGenerationInProgress`); the route
 * comment is explicit that this is transient and the client should simply
 * try again shortly — this is that retry, not a network-failure retry, so
 * only the caller's `isRetryable` predicate triggers it. Any other error
 * rejects immediately. Note that a bare status check is *not* a safe
 * predicate: the same `409` also carries the `price_changed` refusal, which
 * is a decision and never clears by waiting — see `isRetryableConflict` in
 * `utils/api/billingRefusal.ts`.
 */

export interface RetryWithBackoffOptions {
  /** Only errors this accepts are retried; everything else rejects immediately. */
  isRetryable: (error: unknown) => boolean
  /** Delay before the first retry. */
  initialDelayMs?: number
  /** Delay is capped here after growing by `multiplier` each attempt. */
  maxDelayMs?: number
  /** Total time budget spent waiting between attempts before giving up. */
  totalWindowMs?: number
  /** Growth factor applied to the delay after each retry. */
  multiplier?: number
  /** Called before each wait — e.g. to drive a "still working…" status. */
  onRetry?: (attempt: number, delayMs: number) => void
  /**
   * Abandons the loop when the caller no longer wants the answer.
   *
   * A retry window is up to a minute long, so "the player left" is a normal
   * event inside it: without this, an aborted playthrough keeps a timer and a
   * pending request alive, and its eventual rejection lands on a component
   * that is gone — which is how a closed VN pops an error about a beat
   * nobody is waiting for. Aborting rejects with `RetryAbortedError`, which
   * the caller is expected to swallow (see `isRetryAborted`).
   */
  signal?: AbortSignal
}

const DEFAULT_INITIAL_DELAY_MS = 2000
const DEFAULT_MAX_DELAY_MS = 8000
const DEFAULT_TOTAL_WINDOW_MS = 60000
const DEFAULT_MULTIPLIER = 1.4

/**
 * Rejection reason for a retry loop the caller abandoned. Its own type
 * rather than a `DOMException`, so it is recognisable identically in the
 * browser and under the test runner, and never mistaken for a server answer.
 */
export class RetryAbortedError extends Error {
  constructor() {
    super('retry aborted')
    this.name = 'RetryAbortedError'
  }
}

export function isRetryAborted(error: unknown): error is RetryAbortedError {
  return error instanceof RetryAbortedError
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new RetryAbortedError())
      return
    }
    let timer: ReturnType<typeof setTimeout>
    const onAbort = () => {
      clearTimeout(timer)
      reject(new RetryAbortedError())
    }
    timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * Runs `run()`. On a retryable rejection, waits (with growing backoff) and
 * tries again until either it succeeds, a non-retryable error surfaces, or
 * the next wait would exceed `totalWindowMs` — at which point the last
 * error is rethrown. An aborted `signal` ends the loop early with
 * `RetryAbortedError` instead of scheduling another attempt.
 */
export async function retryWithBackoff<T>(
  run: () => Promise<T>,
  options: RetryWithBackoffOptions,
): Promise<T> {
  const initialDelayMs = options.initialDelayMs ?? DEFAULT_INITIAL_DELAY_MS
  const maxDelayMs = options.maxDelayMs ?? DEFAULT_MAX_DELAY_MS
  const totalWindowMs = options.totalWindowMs ?? DEFAULT_TOTAL_WINDOW_MS
  const multiplier = options.multiplier ?? DEFAULT_MULTIPLIER
  const signal = options.signal

  let delay = initialDelayMs
  let elapsed = 0
  let attempt = 0

  for (;;) {
    if (signal?.aborted) throw new RetryAbortedError()
    try {
      return await run()
    } catch (error) {
      // An abort that landed while the request was in flight ends the loop
      // even if the failure itself looks retryable.
      if (signal?.aborted) throw new RetryAbortedError()
      if (!options.isRetryable(error) || elapsed + delay > totalWindowMs) {
        throw error
      }
      attempt += 1
      options.onRetry?.(attempt, delay)
      await sleep(delay, signal)
      elapsed += delay
      delay = Math.min(delay * multiplier, maxDelayMs)
    }
  }
}
