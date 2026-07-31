/**
 * The one action taken when the backend rejects our session: drop the dead
 * token and move the player to whatever sign-in surface their deployment
 * actually has (see `@/utils/sessionRedirect` for that decision).
 *
 * Both 401 paths call this — the axios interceptor in ``main.ts`` and the raw
 * ``fetch`` wrapper in ``authedFetch`` — so a stale token produces one
 * consistent outcome no matter which client hit it first.
 */

import { isSignInFlowPath, signInRouteFor } from '@/utils/sessionRedirect'

export interface SessionBounceDeps {
  router: { replace(to: { path: string; query: Record<string, string> }): unknown }
  clearToken: () => void
  /** Deployment's Portal URL; the caller reads it off live auth state. */
  portalUrl: string | null
  /** Injectable for tests; defaults to the real browser location. */
  location?: { pathname: string; search: string }
}

/**
 * Returns whether the bounce happened. A screen that is itself part of signing
 * in is left alone: bouncing it would loop, and it needs to surface its own
 * error (a wrong password is a 401 too).
 */
export function bounceToSignIn(deps: SessionBounceDeps): boolean {
  const location = deps.location ?? window.location
  if (isSignInFlowPath(location.pathname)) return false

  deps.clearToken()
  deps.router.replace(
    signInRouteFor(
      { portalUrl: deps.portalUrl },
      location.pathname + location.search,
    ),
  )
  return true
}
