/**
 * How the SPA reads a *job-shaped* studio failure (plan U4b).
 *
 * Fusion stories and branching dramas are created with `202 + poll`, so the
 * background run's failure never arrives as an HTTP status the request-time
 * `insufficientCredits` helpers can inspect. It arrives later, as a field on
 * the polled DTO: `status === 'failed'` plus an additive `error_code`.
 *
 * `error_code` is an **open set** forwarded verbatim from the refusal chain.
 * Only `insufficient_credits` is player-actionable (show the top-up notice);
 * every other value — and `null`, which is what an ordinary crash records —
 * must fall through to the existing generic failure copy, so an unknown code
 * shipped by a newer backend degrades quietly instead of blanking the card.
 */

import { INSUFFICIENT_CREDITS_CODE } from '@/utils/api/insufficientCredits'

/** The shape both studio poll DTOs share for failure reporting. */
export interface StudioJobFailure {
  status: string
  error_code?: string | null
}

/**
 * True only for a failed job the backend attributed to an empty balance.
 *
 * The status check is load-bearing: a re-run clears the code, but a stale
 * code on a *running* job must never resurrect the top-up prompt.
 */
export function isInsufficientCreditsFailure(
  job: StudioJobFailure | null | undefined,
): boolean {
  if (!job) return false
  return job.status === 'failed' && job.error_code === INSUFFICIENT_CREDITS_CODE
}

/**
 * True when a failed job should render the generic failure copy — i.e. it
 * failed for any reason other than credits (including unknown future codes).
 */
export function isGenericStudioFailure(
  job: StudioJobFailure | null | undefined,
): boolean {
  if (!job) return false
  return job.status === 'failed' && !isInsufficientCreditsFailure(job)
}
