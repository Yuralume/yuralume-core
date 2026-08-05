/**
 * Published action prices for the hosted player surfaces (plan AP2/AP3).
 *
 * One module-scope snapshot, shared by every surface that quotes a price —
 * the chat composer, the picture generator, the overage switches — so a
 * screen full of hints costs exactly one request. Same shape as
 * `useCloudCredits`, with a much calmer refresh policy: prices change when
 * the back office edits them, balances change on every action.
 *
 * ## Why a price can resolve to `null`
 *
 * The proxy publishes the whole tier table (it is public and identical for
 * everyone); nothing in the SPA knows which tier the signed-in player is on.
 * So a price is quotable only when it is unambiguous:
 *
 *   1. at least one tier bills `action_fixed` — under `token_floating` the
 *      fixed number is simply not what gets charged;
 *   2. every `action_fixed` tier publishes this action; and
 *   3. they all publish the same number.
 *
 * Anything else resolves to `null` and the hint stays hidden. The plan's red
 * line is "fail-soft, never a fabricated number" — quoting another tier's
 * price would be exactly that. See `resolveActionPrice` for the rule and the
 * report note about the backend adding the caller's own tier.
 */

import { computed, ref } from 'vue'

import {
  fetchCloudPricing,
  type ActionPrice,
  type TierPricing,
} from '@/utils/api/cloudPricing'

/** First-wave `charging_mode=action` keys from `contracts/action-catalog.json`. */
export const ACTION_CHAT = 'chat'
export const ACTION_IMAGE_CHAT_TOOL = 'image_chat_tool'
export const ACTION_IMAGE_PORTRAIT = 'image_portrait'
export const ACTION_CHARACTER_DRAFT = 'character_draft'
export const ACTION_CHAT_IMAGE_OVERAGE = 'chat_image_overage'
export const ACTION_FEED_POST_OVERAGE = 'feed_post_overage'

/** Second-wave Creator Studio keys — one price per fusion run, one per re-run. */
export const ACTION_FUSION_STORY = 'fusion_story'
export const ACTION_FUSION_STORY_ITERATE = 'fusion_story_iterate'

/**
 * 起幕 (plan SC). One price for raising the curtain — the narration, the
 * character's first line, the scene frame. The turns played inside the scene
 * are ordinary chat and are charged as `chat`, which is why the composer
 * shows this number next to the button and not on the scene itself.
 */
export const ACTION_STORY_SCENE_OPEN = 'story_scene_open'

const BILLING_SHAPE_ACTION_FIXED = 'action_fixed'

/** What the caller knows about the wallet when pre-checking an action. */
export interface BalanceView {
  total: number
  /** False while no snapshot has arrived — never treat that as "broke". */
  known: boolean
  /** True when the snapshot is last-known-good rather than fresh. */
  stale: boolean
}

const tiers = ref<TierPricing[]>([])
const stale = ref(false)
const loading = ref(false)
/** False once the backend answered 404 — this deployment has no price list. */
const supported = ref(true)
const loaded = ref(false)
let inFlight: Promise<void> | null = null

/**
 * The price a player would be charged for one `action_key`, or `null` when
 * that cannot be stated without guessing. Pure and exported so the rule can
 * be tested without a component or a transport.
 */
export function resolveActionPrice(
  list: readonly TierPricing[],
  actionKey: string,
): ActionPrice | null {
  const fixed = list.filter(
    tier => tier.billing_shape === BILLING_SHAPE_ACTION_FIXED,
  )
  if (fixed.length === 0) return null
  let chosen: ActionPrice | null = null
  for (const tier of fixed) {
    const entry = tier.actions.find(action => action.action_key === actionKey)
    if (!entry) return null
    if (chosen === null) {
      chosen = entry
    } else if (entry.price_cr !== chosen.price_cr) {
      return null
    }
  }
  return chosen
}

async function load(options: { refresh?: boolean } = {}): Promise<void> {
  if (!supported.value) return
  if (inFlight) return inFlight
  loading.value = true
  inFlight = (async () => {
    try {
      const result = await fetchCloudPricing(options)
      if (result.kind === 'ok') {
        tiers.value = result.snapshot.tiers
        stale.value = result.snapshot.stale
      } else if (result.kind === 'unsupported') {
        supported.value = false
        tiers.value = []
      }
      // 'degraded' deliberately falls through: keep the previous list.
      loaded.value = true
    } finally {
      loading.value = false
      inFlight = null
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

function reset(): void {
  tiers.value = []
  stale.value = false
  supported.value = true
  loaded.value = false
  loading.value = false
  inFlight = null
}

function priceOf(actionKey: string): ActionPrice | null {
  return resolveActionPrice(tiers.value, actionKey)
}

/**
 * The price this action needs when the wallet demonstrably cannot cover it,
 * otherwise `null` (= let it run).
 *
 * Deliberately conservative in three ways, because a wrong "no" costs the
 * player an action they could afford:
 *   - an unknown price never blocks (the server still enforces);
 *   - an unknown balance never blocks;
 *   - a stale balance never blocks — that is precisely the state a player is
 *     in right after topping up.
 */
function shortfallFor(actionKey: string, balance: BalanceView): number | null {
  const price = priceOf(actionKey)
  if (price === null) return null
  if (!balance.known || balance.stale) return null
  if (!Number.isFinite(balance.total)) return null
  return balance.total < price.price_cr ? price.price_cr : null
}

export function useActionPricing() {
  return {
    // state
    tiers: computed(() => tiers.value),
    stale: computed(() => stale.value),
    loading: computed(() => loading.value),
    /** False on self-host / any deployment without a published price list. */
    supported: computed(() => supported.value),

    // queries
    priceOf,
    shortfallFor,

    // actions
    ensureLoaded,
    refresh,
    reset,
  }
}
