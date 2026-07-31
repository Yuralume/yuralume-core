/**
 * Hosted notice-board unread read — cloud mode only.
 *
 * Sibling of `cloudCredits.ts`, and deliberately just as thin: the player reads
 * announcements in the Portal, so all that crosses this boundary is whether
 * something is waiting. No titles, no bodies, no locales.
 *
 * Two failure modes must stay distinguishable, because the dot reacts to them
 * differently:
 *
 *   - **there is no board** — self-host, or a cloud operator with no projected
 *     tenant. The dot is hidden permanently. This is a 404 that *names itself*
 *     (`detail.reason`); a bare 404 does not qualify, because during a rolling
 *     deploy a new SPA routinely reaches an older replica that does not have
 *     this route yet, and treating that as permanent would switch the dot off
 *     for the rest of the session over a transient topology fact.
 *   - **momentarily unknown** — 503, transport failure, an unnamed 404, or a
 *     response we cannot read. Whatever was last known stays, rather than a dot
 *     blinking out and hiding a notice.
 */

import { authedFetch } from '@/utils/authedFetch'

export interface CloudAnnouncementsSnapshot {
  has_unread: boolean
  unread_count: number
  latest_published_at: string | null
  /** True when the backend served last-known-good after an upstream miss. */
  stale: boolean
}

export type CloudAnnouncementsResult =
  | { kind: 'ok'; snapshot: CloudAnnouncementsSnapshot }
  /** No board for this deployment / operator — stop showing the dot. */
  | { kind: 'unsupported' }
  /** Temporarily unknown — keep whatever was last known. */
  | { kind: 'degraded' }

const ENDPOINT = '/api/v1/cloud/announcements/unread'

/** 404 reasons that really do mean "this deployment has no notice board". */
const NO_BOARD_REASONS = new Set(['not_cloud_mode', 'no_cloud_tenant'])

export async function fetchCloudAnnouncements(
  options: { refresh?: boolean } = {},
): Promise<CloudAnnouncementsResult> {
  let res: Response
  try {
    res = await authedFetch(options.refresh ? `${ENDPOINT}?refresh=1` : ENDPOINT)
  } catch {
    return { kind: 'degraded' }
  }
  if (res.status === 404) {
    return (await namesNoBoard(res)) ? { kind: 'unsupported' } : { kind: 'degraded' }
  }
  if (!res.ok) return { kind: 'degraded' }
  let payload: unknown
  try {
    payload = await res.json()
  } catch {
    return { kind: 'degraded' }
  }
  const snapshot = parse(payload)
  return snapshot === null ? { kind: 'degraded' } : { kind: 'ok', snapshot }
}

/** True only for a 404 the endpoint itself produced, naming why there is no board. */
async function namesNoBoard(res: Response): Promise<boolean> {
  try {
    const body = (await res.json()) as { detail?: { reason?: unknown } }
    return (
      typeof body?.detail?.reason === 'string' && NO_BOARD_REASONS.has(body.detail.reason)
    )
  } catch {
    return false
  }
}

/**
 * Strict: a payload missing a field, or carrying the wrong type, or contradicting
 * itself, is `null` — which routes to `degraded`.
 *
 * Coercing junk into a well-formed `has_unread: false` is the single worst thing
 * this file could do. It looks like a successful read, gets stored as the current
 * value, and silently turns off a notice the operator was required to give. There
 * is no symmetric harm in the other direction: a dot that stays up costs a glance.
 */
function parse(payload: unknown): CloudAnnouncementsSnapshot | null {
  if (payload === null || typeof payload !== 'object') return null
  const body = payload as Record<string, unknown>
  const hasUnread = body.has_unread
  const unreadCount = body.unread_count
  if (typeof hasUnread !== 'boolean') return null
  if (
    typeof unreadCount !== 'number'
    || !Number.isInteger(unreadCount)
    || unreadCount < 0
  ) {
    return null
  }
  // The two fields state the same fact; disagreement means this is not one
  // coherent answer, so neither half is trustworthy.
  if (hasUnread !== unreadCount > 0) return null
  const latest = body.latest_published_at
  if (latest !== null && latest !== undefined && typeof latest !== 'string') return null
  return {
    has_unread: hasUnread,
    unread_count: unreadCount,
    latest_published_at: typeof latest === 'string' ? latest : null,
    stale: body.stale === true,
  }
}
