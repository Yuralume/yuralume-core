/**
 * When to extend an in-progress session.
 *
 * Hosted sessions run on a fixed TTL (12h in the shipped hosted env), which
 * used to evict a player mid-conversation on a wall-clock timer. Renewal is
 * gated on *recent interaction* rather than "a tab is open": an abandoned tab
 * must not keep a hosted session — and the paid tier behind it — alive
 * indefinitely, and the backend's absolute cap bounds it either way.
 *
 * Decision rules plus the renewer that applies them. Neither touches the DOM,
 * so both are testable in this repo's node test environment; the browser
 * wiring (listeners, poll timer) lives in `@/composables/useSessionRenewal`.
 */

/** Renew once the session is inside this fraction of its total lifetime. */
export const RENEW_WHEN_REMAINING_RATIO = 0.25

/** How recently the player must have interacted for renewal to be earned. */
export const ACTIVITY_WINDOW_MS = 30 * 60 * 1000

/** Threshold used when the token exposes an expiry but no issue time. */
export const FALLBACK_RENEW_WINDOW_MS = 15 * 60 * 1000

export interface SessionClaims {
  /** Epoch milliseconds, or null when the token doesn't say. */
  issuedAt: number | null
  expiresAt: number | null
}

const UNKNOWN_CLAIMS: SessionClaims = { issuedAt: null, expiresAt: null }

/**
 * Read `iat` / `exp` out of the stored JWT.
 *
 * Deliberately *unverified* — the signature is the backend's business and the
 * SPA holds no secret. These claims only decide when to ask for a renewal; a
 * forged value can at worst make the client ask too early, and the backend
 * still adjudicates. Anything unparseable degrades to "unknown", which
 * disables renewal rather than breaking a session that otherwise works.
 */
export function readSessionClaims(token: string | null): SessionClaims {
  if (!token) return UNKNOWN_CLAIMS
  const segments = token.split('.')
  if (segments.length !== 3) return UNKNOWN_CLAIMS
  try {
    const payload = JSON.parse(decodeBase64Url(segments[1])) as Record<string, unknown>
    const expiresAt = epochSeconds(payload.exp)
    if (expiresAt === null) return UNKNOWN_CLAIMS
    return { issuedAt: epochSeconds(payload.iat), expiresAt }
  } catch {
    return UNKNOWN_CLAIMS
  }
}

export interface RenewalDecision {
  claims: SessionClaims
  lastActivityAt: number
  now: number
}

export function shouldRenewSession(input: RenewalDecision): boolean {
  const { expiresAt } = input.claims
  if (expiresAt === null) return false

  const remaining = expiresAt - input.now
  // Already dead: renewal would 401 too, and the bounce owns that path.
  if (remaining <= 0) return false

  if (input.now - input.lastActivityAt > ACTIVITY_WINDOW_MS) return false

  return remaining <= renewThreshold(input.claims, expiresAt)
}

function renewThreshold(claims: SessionClaims, expiresAt: number): number {
  if (claims.issuedAt === null) return FALLBACK_RENEW_WINDOW_MS
  const lifetime = expiresAt - claims.issuedAt
  // A non-positive lifetime means the clocks disagree; fall back rather than
  // derive a threshold that would suppress renewal until the token is dead.
  if (lifetime <= 0) return FALLBACK_RENEW_WINDOW_MS
  return lifetime * RENEW_WHEN_REMAINING_RATIO
}

export interface SessionRenewerDeps {
  /** False when the deployment has auth off — nothing to renew. */
  isEnabled: () => boolean
  readToken: () => string | null
  renew: () => Promise<void>
  /** Injectable for tests. */
  now?: () => number
}

export interface SessionRenewer {
  /** Record that the player just did something. */
  markActive(): void
  /** Renew if the rules say it is both needed and earned. */
  maybeRenew(): Promise<void>
}

/**
 * Tracks liveness and single-flights the renewal call.
 *
 * A failed renewal is swallowed on purpose: the token we already hold is still
 * valid, so the right move is to carry on and re-evaluate on the next tick.
 * The terminal case (expired / revoked / past the deployment's absolute cap)
 * arrives as a 401 on the next real request, which the global bounce owns.
 */
export function createSessionRenewer(deps: SessionRenewerDeps): SessionRenewer {
  const now = deps.now ?? (() => Date.now())
  let lastActivityAt = now()
  let inFlight = false

  return {
    markActive(): void {
      lastActivityAt = now()
    },

    async maybeRenew(): Promise<void> {
      if (inFlight || !deps.isEnabled()) return
      const due = shouldRenewSession({
        claims: readSessionClaims(deps.readToken()),
        lastActivityAt,
        now: now(),
      })
      if (!due) return

      inFlight = true
      try {
        await deps.renew()
      } catch {
        /* keep the still-valid token; the next tick (or a 401) decides */
      } finally {
        inFlight = false
      }
    },
  }
}

function epochSeconds(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value * 1000 : null
}

function decodeBase64Url(segment: string): string {
  const base64 = segment.replace(/-/g, '+').replace(/_/g, '/')
  const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=')
  return atob(padded)
}
