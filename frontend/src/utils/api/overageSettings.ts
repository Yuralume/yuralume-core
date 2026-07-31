/**
 * Per-player quota-overage switches — cloud mode only (plan AP4).
 *
 * `GET/PUT /api/v1/operator/overage-settings` 404s outside cloud mode, so the
 * reader mirrors the credits/pricing family: a discriminated result rather
 * than a thrown error, letting the settings section render nothing at all on
 * self-host instead of an error box for a feature that has no meaning there.
 *
 * Writes are different: a switch the player just flipped that silently failed
 * would be worse than an error, so `updateOverageSettings` throws and the
 * caller rolls the toggle back. Turning one *on* can also be refused on
 * purpose — consent is to a price, and the backend will not store a yes it
 * cannot price — which arrives as an `OverageConsentError` carrying the reason
 * so the settings pane can say which gate is shut.
 */

import { authedFetch } from '@/utils/authedFetch'

export interface OverageSettings {
  chat_image_enabled: boolean
  feed_post_enabled: boolean
  /** Read-only: the tier decides whether these switches do anything. */
  tier_overage_enabled: boolean
  /** Read-only: purchases allowed per item per rolling 24h. */
  daily_limit: number
  /**
   * Read-only: what one purchase costs on *this player's* tier, or `null` when
   * that cannot be stated — which is also when the switch cannot be turned on.
   * Never treat `null` as free.
   */
  chat_image_price_cr: number | null
  feed_post_price_cr: number | null
}

/** Why the backend refused to arm a switch. Unknown values are possible. */
export type OverageConsentReason =
  | 'tier_closed'
  | 'price_unavailable'
  | (string & {})

/** A deliberate refusal, as opposed to a transport/server failure. */
export class OverageConsentError extends Error {
  readonly reason: OverageConsentReason
  readonly item: string | null

  constructor(reason: OverageConsentReason, item: string | null) {
    super(`overage consent rejected (${reason})`)
    this.name = 'OverageConsentError'
    this.reason = reason
    this.item = item
  }
}

export type OverageSettingsResult =
  | { kind: 'ok'; settings: OverageSettings }
  /** Not a hosted deployment (or the service is off) — hide the section. */
  | { kind: 'unsupported' }
  /** Momentarily unreadable — hide rather than guess at the switch state. */
  | { kind: 'degraded' }

const ENDPOINT = '/api/v1/operator/overage-settings'

export async function fetchOverageSettings(): Promise<OverageSettingsResult> {
  let res: Response
  try {
    res = await authedFetch(ENDPOINT)
  } catch {
    return { kind: 'degraded' }
  }
  if (res.status === 404) return { kind: 'unsupported' }
  if (!res.ok) return { kind: 'degraded' }
  try {
    return { kind: 'ok', settings: normalize(await res.json()) }
  } catch {
    return { kind: 'degraded' }
  }
}

/**
 * Flip one switch. Omitted switches keep their stored value, matching the
 * backend's partial-update contract — the two consents are independent and
 * must never move together implicitly.
 */
export async function updateOverageSettings(
  payload: {
    chat_image_enabled?: boolean
    feed_post_enabled?: boolean
  },
): Promise<OverageSettings> {
  const res = await authedFetch(ENDPOINT, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (res.status === 400) throw await consentError(res)
  if (!res.ok) throw new Error(`overage settings update failed (${res.status})`)
  return normalize(await res.json())
}

/**
 * Read the structured refusal out of a 400, degrading to an unnamed reason
 * rather than throwing while building an error — the caller still has to roll
 * a toggle back either way.
 */
async function consentError(res: Response): Promise<Error> {
  try {
    const detail = ((await res.json()) as Record<string, unknown>)?.detail
    if (detail && typeof detail === 'object') {
      const body = detail as Record<string, unknown>
      return new OverageConsentError(
        typeof body.reason === 'string' ? body.reason : 'unknown',
        typeof body.item === 'string' ? body.item : null,
      )
    }
  } catch {
    // fall through
  }
  return new OverageConsentError('unknown', null)
}

function normalize(payload: unknown): OverageSettings {
  const body = (payload ?? {}) as Record<string, unknown>
  const limit = body.daily_limit
  return {
    chat_image_enabled: body.chat_image_enabled === true,
    feed_post_enabled: body.feed_post_enabled === true,
    tier_overage_enabled: body.tier_overage_enabled === true,
    daily_limit:
      typeof limit === 'number' && Number.isFinite(limit) && limit > 0
        ? Math.floor(limit)
        : 0,
    chat_image_price_cr: price(body.chat_image_price_cr),
    feed_post_price_cr: price(body.feed_post_price_cr),
  }
}

/** A price is a positive finite number or nothing at all — never a coerced 0. */
function price(raw: unknown): number | null {
  return typeof raw === 'number' && Number.isFinite(raw) && raw > 0
    ? raw
    : null
}
