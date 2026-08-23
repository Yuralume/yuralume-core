/**
 * "Answer the refusal where the player is standing."
 *
 * `billingRefusalKind` says *which* of the two non-faults a failure is; this
 * owns what a surface then does about it. Both refusals share one property
 * that decides the whole design: **nothing ran and nothing was charged**. So
 * neither may take the surface down with it — no navigating away, no clearing
 * the form, no red "generation failed" line that reads as a broken product.
 *
 * Kept out of the components because the wrong version of this is easy to
 * write and hard to see: the branching-drama player had four entry points
 * that all funnelled a 402 into an `emit('error')` which booted the player
 * out of the VN and lost the line they had just typed (FX2), while the redraw
 * button one header above already did it correctly.
 *
 * One instance per surface (module-scope state would leak one screen's
 * refusal onto the next).
 */

import { ref, type Ref } from 'vue'

import {
  billingRefusalKind,
  refreshQuotedPrices,
} from '@/utils/api/billingRefusal'

export interface BillingNotice {
  /** The wallet is empty — render the shared top-up card. */
  outOfCredits: Ref<boolean>
  /** The quoted price moved — render "prices moved, press it again". */
  priceChanged: Ref<boolean>
  /** Take both notices down (a new press, a new drama, a fresh screen). */
  clear: () => void
  /**
   * Absorb `err` if it is a billing refusal; `false` when it is an ordinary
   * failure the caller still has to handle.
   */
  absorb: (err: unknown) => Promise<boolean>
}

export function useBillingNotice(): BillingNotice {
  const outOfCredits = ref(false)
  const priceChanged = ref(false)

  function clear(): void {
    outOfCredits.value = false
    priceChanged.value = false
  }

  async function absorb(err: unknown): Promise<boolean> {
    const kind = billingRefusalKind(err)
    if (kind === null) return false
    clear()
    if (kind === 'insufficient_credits') {
      outOfCredits.value = true
      return true
    }
    // Load-bearing: the cached price is by definition the number the server
    // just refused, and it is both what the chip advertises and what the
    // next press sends. Without the refresh the retry is guaranteed to be
    // refused again for the same reason.
    await refreshQuotedPrices()
    priceChanged.value = true
    return true
  }

  return { outOfCredits, priceChanged, clear, absorb }
}
