/**
 * Hosted credit ("螢火") balance state for the player surfaces.
 *
 * Module-scope refs make this a de-facto singleton, matching `useAuth` —
 * the sidebar badge, the chat panel and the image panel all read and refresh
 * the same snapshot without pulling pinia in.
 *
 * Lifecycle:
 *   - `ensureLoaded()` on first mount of a cloud player surface.
 *   - `refreshAfterAction()` after a deliberate, charged player action
 *     (chat reply finished, image candidates returned, TTS audio obtained).
 *     Fire-and-forget: a balance read must never surface an error or block
 *     the action that just succeeded.
 *   - `reset()` whenever the authenticated identity changes — login, account
 *     switch, logout, or a token revoked underneath us. Driven by
 *     `onIdentityChanged` rather than by whichever component renders the
 *     logout button, so a shared browser never carries a balance across
 *     accounts on *any* of those paths.
 *
 * Because this is a singleton fed by an async read, clearing the refs is not
 * enough on its own: a request that was already in the air when the identity
 * changed would still resolve and write the previous player's balance back
 * in. Every read therefore carries the `generation` it was issued under and
 * is discarded on arrival if `reset()` has bumped it since.
 *
 * Fail-soft (plan §1-4): a `404` means there is no ledger at all and the
 * badge hides for good; any other failure keeps the last-known-good value on
 * screen. The badge never renders a fabricated zero.
 */

import { computed, ref } from 'vue'

import {
  fetchCloudCredits,
  type CloudCreditsSnapshot,
} from '@/utils/api/cloudCredits'
import { onIdentityChanged } from '@/utils/identityLifecycle'

/**
 * Owner-agreed v1 threshold (plan §8-4). Hard-coded on purpose — it moves
 * into the tier profile control plane only if it needs to differ per tier.
 */
export const LOW_BALANCE_THRESHOLD_CR = 200

const balance = ref<CloudCreditsSnapshot | null>(null)
const loading = ref(false)
/** False once the backend answered 404 — no ledger for this operator. */
const supported = ref(true)
const loaded = ref(false)
let inFlight: Promise<void> | null = null
/**
 * Bumped by every `reset()`. A read that comes back under a stale generation
 * belongs to an identity that is no longer signed in and is dropped whole —
 * both its data and its bookkeeping, since a newer read owns those now.
 */
let generation = 0

async function load(options: { refresh?: boolean } = {}): Promise<void> {
  if (!supported.value) return
  // Collapse concurrent callers (badge mount + a just-finished action) onto
  // one request rather than racing two reads of the same value.
  if (inFlight) return inFlight
  const issuedAt = generation
  loading.value = true
  inFlight = (async () => {
    try {
      const result = await fetchCloudCredits(options)
      if (issuedAt !== generation) return
      if (result.kind === 'ok') {
        balance.value = result.snapshot
      } else if (result.kind === 'unsupported') {
        supported.value = false
        balance.value = null
      }
      // 'degraded' deliberately falls through: keep the previous snapshot.
      loaded.value = true
    } finally {
      // Only the current generation may clear the flags: a superseded read
      // finishing late must not cancel the loading state of the read that
      // replaced it, nor null out its `inFlight` and let a duplicate through.
      if (issuedAt === generation) {
        loading.value = false
        inFlight = null
      }
    }
  })()
  return inFlight
}

function ensureLoaded(): Promise<void> {
  if (loaded.value || loading.value) return inFlight ?? Promise.resolve()
  return load()
}

function refresh(): Promise<void> {
  return load({ refresh: true })
}

/**
 * Post-action hook. One line at each completion point; never throws, never
 * awaited by the caller.
 *
 * Exported standalone as well as on the composable so hot, high-cardinality
 * components (every chat bubble owns a TTS button) can call it without
 * building a fresh set of computed wrappers per instance.
 */
export function refreshCloudCreditsAfterAction(): void {
  if (!supported.value) return
  void refresh().catch(() => {
    /* balance display is best-effort — never disturb the action that ran */
  })
}

/**
 * Drop everything known about the balance and orphan any read still in the
 * air. `supported` goes back to true on purpose: "no ledger" was a fact
 * about the previous operator, not about this browser.
 */
function reset(): void {
  generation += 1
  balance.value = null
  supported.value = true
  loaded.value = false
  loading.value = false
  inFlight = null
}

// The singleton follows the signed-in identity for the life of the page;
// there is nothing to unsubscribe from.
onIdentityChanged(reset)

export function useCloudCredits() {
  return {
    // state
    balance: computed(() => balance.value),
    /** True only when there is a real snapshot to render. */
    hasBalance: computed(() => balance.value !== null),
    total: computed(() => balance.value?.total_cr ?? 0),
    gift: computed(() => balance.value?.gift_cr ?? 0),
    purchased: computed(() => balance.value?.purchased_cr ?? 0),
    stale: computed(() => balance.value?.stale === true),
    lowBalance: computed(
      () => balance.value !== null
        && balance.value.total_cr < LOW_BALANCE_THRESHOLD_CR,
    ),
    loading: computed(() => loading.value),

    // actions
    ensureLoaded,
    refresh,
    refreshAfterAction: refreshCloudCreditsAfterAction,
    reset,
  }
}
