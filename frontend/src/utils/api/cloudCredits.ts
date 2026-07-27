/**
 * Hosted credit ("螢火") balance read — cloud mode only.
 *
 * The Portal balance API is unreachable from this SPA (no CORS on the User
 * service, `connect-src 'self'` CSP, different session system), so Core
 * proxies a display-only snapshot at `GET /api/v1/cloud/credits`.
 *
 * Two failure modes must stay distinguishable, because the badge reacts to
 * them differently (plan §4.1 fail-soft rule — never show a fake zero):
 *
 *   - `404` — self-host, or a cloud operator with no projected tenant. There
 *     is no ledger at all, so the badge is hidden permanently.
 *   - `503` / transport failure — the value is momentarily unknown. Whatever
 *     was last shown stays on screen rather than blinking out.
 */

import { authedFetch } from '@/utils/authedFetch'

export interface CloudCreditsSnapshot {
  gift_cr: number
  purchased_cr: number
  total_cr: number
  /** True when the backend served last-known-good after an upstream miss. */
  stale: boolean
}

export type CloudCreditsResult =
  | { kind: 'ok'; snapshot: CloudCreditsSnapshot }
  /** No ledger for this deployment / operator — stop showing the badge. */
  | { kind: 'unsupported' }
  /** Temporarily unknown — keep the previous value visible. */
  | { kind: 'degraded' }

const ENDPOINT = '/api/v1/cloud/credits'

export async function fetchCloudCredits(
  options: { refresh?: boolean } = {},
): Promise<CloudCreditsResult> {
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

function normalize(payload: unknown): CloudCreditsSnapshot {
  const body = (payload ?? {}) as Record<string, unknown>
  return {
    gift_cr: numberField(body.gift_cr),
    purchased_cr: numberField(body.purchased_cr),
    total_cr: numberField(body.total_cr),
    stale: body.stale === true,
  }
}

function numberField(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}
