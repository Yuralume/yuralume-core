/**
 * The recursive poll loop shared by the export and restore job composables
 * (CB4). Not `setInterval`: each read waits for the previous one to finish
 * before scheduling the next, so a slow response can never stack a second
 * request on top of itself.
 *
 * Cancellation is a caller-owned predicate (`isCancelled`) rather than an
 * `AbortController` here, mirroring the request-sequence-number guard
 * `useStoryScene` uses — the composables below bump their own token on every
 * new job so a poll loop from a job the player already left behind can never
 * write its answer onto the one now on screen.
 *
 * A handful of consecutive transport failures (a dropped connection, a
 * reload race) are swallowed and retried rather than surfaced immediately —
 * a job that is still genuinely running should not fail the UI over one lost
 * response — but the loop gives up and rethrows once the deployment looks
 * truly unreachable rather than polling forever.
 */

export interface BackupJobLike {
  status: string
}

export interface PollBackupJobOptions<T extends BackupJobLike> {
  fetch: () => Promise<T>
  onUpdate: (job: T) => void
  isCancelled: () => boolean
  intervalMs?: number
  maxConsecutiveFailures?: number
}

const DEFAULT_INTERVAL_MS = 1500
const DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export async function pollBackupJob<T extends BackupJobLike>(
  options: PollBackupJobOptions<T>,
): Promise<void> {
  const interval = options.intervalMs ?? DEFAULT_INTERVAL_MS
  const maxFailures = options.maxConsecutiveFailures ?? DEFAULT_MAX_CONSECUTIVE_FAILURES
  let consecutiveFailures = 0

  while (!options.isCancelled()) {
    try {
      const job = await options.fetch()
      consecutiveFailures = 0
      if (options.isCancelled()) return
      options.onUpdate(job)
      if (job.status !== 'running') return
    } catch (error) {
      consecutiveFailures += 1
      if (consecutiveFailures >= maxFailures) throw error
    }
    if (options.isCancelled()) return
    await sleep(interval)
  }
}
