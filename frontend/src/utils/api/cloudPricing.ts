/**
 * Published hosted price list read — cloud mode only (plan AP3).
 *
 * Sibling of `cloudCredits.ts`, and for the same reason: the SPA cannot reach
 * the User service directly, so Core proxies the public price list at
 * `GET /api/v1/cloud/pricing`.
 *
 * The three failure modes stay distinguishable because the UI reacts to them
 * differently — and because the plan's red line is that a player is *never*
 * shown a fabricated number:
 *
 *   - `404` — self-host, or a deployment with no control plane. There is no
 *     price list at all, so every price hint stays hidden for good.
 *   - `503` / transport failure — momentarily unknown. Whatever was last
 *     quoted stays on screen (the proxy itself already serves last-known-good
 *     under a `stale` flag).
 *   - malformed row — dropped. A price of `0`, a negative price or a
 *     non-numeric one is not "free", it is "unknown", and unknown prices are
 *     not rendered.
 */

import { authedFetch } from '@/utils/authedFetch'

export interface ActionPrice {
  action_key: string
  /** `per_message` | `per_image` | `per_portrait` | `per_draft` | `per_post`. */
  unit: string
  price_cr: number
  /** True for the quota-overage keys, which are opt-in purchases. */
  overage: boolean
}

export interface TierPricing {
  tier_name: string
  /** `token_floating` (billed by usage) | `action_fixed` (one price per action). */
  billing_shape: string
  actions: ActionPrice[]
}

export interface CloudPricingSnapshot {
  tiers: TierPricing[]
  /** True when the proxy served last-known-good after an upstream miss. */
  stale: boolean
}

export type CloudPricingResult =
  | { kind: 'ok'; snapshot: CloudPricingSnapshot }
  /** No price list for this deployment — stop quoting prices. */
  | { kind: 'unsupported' }
  /** Temporarily unknown — keep the previous list. */
  | { kind: 'degraded' }

const ENDPOINT = '/api/v1/cloud/pricing'

export async function fetchCloudPricing(
  options: { refresh?: boolean } = {},
): Promise<CloudPricingResult> {
  let res: Response
  try {
    res = await authedFetch(options.refresh ? `${ENDPOINT}?refresh=1` : ENDPOINT)
  } catch {
    return { kind: 'degraded' }
  }
  if (res.status === 404) return { kind: 'unsupported' }
  if (!res.ok) return { kind: 'degraded' }
  try {
    return { kind: 'ok', snapshot: normalize(await res.json()) }
  } catch {
    return { kind: 'degraded' }
  }
}

function normalize(payload: unknown): CloudPricingSnapshot {
  const body = (payload ?? {}) as Record<string, unknown>
  const rawTiers = Array.isArray(body.tiers) ? body.tiers : []
  return {
    stale: body.stale === true,
    tiers: rawTiers
      .map(normalizeTier)
      .filter((tier): tier is TierPricing => tier !== null),
  }
}

function normalizeTier(raw: unknown): TierPricing | null {
  if (!raw || typeof raw !== 'object') return null
  const row = raw as Record<string, unknown>
  const tierName = stringField(row.tier_name)
  if (!tierName) return null
  const rawActions = Array.isArray(row.actions) ? row.actions : []
  return {
    tier_name: tierName,
    billing_shape: stringField(row.billing_shape),
    actions: rawActions
      .map(normalizeAction)
      .filter((action): action is ActionPrice => action !== null),
  }
}

function normalizeAction(raw: unknown): ActionPrice | null {
  if (!raw || typeof raw !== 'object') return null
  const row = raw as Record<string, unknown>
  const actionKey = stringField(row.action_key)
  const price = row.price_cr
  // Strict on purpose: anything we cannot read as a positive number is
  // "no quote", never a zero the player would read as free.
  if (!actionKey) return null
  if (typeof price !== 'number' || !Number.isFinite(price) || price <= 0) {
    return null
  }
  return {
    action_key: actionKey,
    unit: stringField(row.unit),
    price_cr: price,
    overage: row.overage === true,
  }
}

function stringField(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}
