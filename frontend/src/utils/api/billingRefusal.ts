/**
 * One place to ask "is this failure an *answer*?" — and one place to react.
 *
 * Under a hosted fixed-price tier every generation button can come back with
 * two refusals that are not faults at all:
 *
 *   - `402 insufficient_credits` — the wallet is empty. Nothing ran and
 *     nothing was charged, so the right surface is the shared top-up card,
 *     never a red error line that reads as "the generator is broken".
 *   - `409 price_changed` — the published price moved between the number this
 *     screen quoted and the charge. Nothing was reserved either, so the right
 *     surface is "prices moved, send it again".
 *
 * Each surface used to re-derive that pair with its own `if` ladder, which is
 * how `/images/candidates` ended up rendering a moved price as a generic
 * generation failure. The classification lives here so a new priced button
 * inherits it instead of re-inventing two thirds of it.
 */

import { isInsufficientCreditsError } from '@/utils/api/insufficientCredits'
import { isPriceChangedError } from '@/utils/api/priceChanged'
import { useActionPricing } from '@/composables/useActionPricing'

export type BillingRefusalKind = 'insufficient_credits' | 'price_changed'

/** Which billing refusal `err` is, or `null` when it is an ordinary failure. */
export function billingRefusalKind(err: unknown): BillingRefusalKind | null {
  if (isInsufficientCreditsError(err)) return 'insufficient_credits'
  if (isPriceChangedError(err)) return 'price_changed'
  return null
}

/**
 * Re-pull the published prices after a `price_changed` refusal.
 *
 * Load-bearing, not cosmetic: the cached list is by definition the stale one
 * the server just rejected, and every priced surface both *displays* it in its
 * hint and *sends* it as the quote. Skipping the refresh would tell the player
 * to resend a number that is guaranteed to be refused again, and the hint
 * beside the button would keep advertising a price nobody can be charged.
 *
 * Fail-soft: a refresh that cannot land leaves the previous list in place (the
 * composable's own policy), so the retry is merely unimproved, never blocked.
 */
export async function refreshQuotedPrices(): Promise<void> {
  await useActionPricing().refresh()
}
