/**
 * Where a rejected session lands.
 *
 * A hosted player's identity comes from the Portal — their Cloud account is
 * OAuth-only and has no password — so Core's ``/login`` form is a dead end for
 * them, which is exactly where every 401 used to land. Hosted deployments that
 * advertised a Portal (``/auth/config`` → ``portal_url``) route to the expiry
 * screen instead, which offers the way back in. Self-host — and any deployment
 * that advertises no Portal — keeps the password form.
 *
 * Shared by the axios interceptor, ``authedFetch`` and the router guard so the
 * three cannot drift apart — they previously each hard-coded ``/login``.
 */

export const SIGN_IN_PATH = '/login'
export const SESSION_EXPIRED_PATH = '/session-expired'

/**
 * Screens that ARE the sign-in flow. Bouncing away from one of these would
 * either loop or swallow the error the screen is trying to show.
 *
 * Matched whole, never by prefix: `/loginish` is an ordinary screen and must
 * stay bounceable. Every sign-in screen is a fixed path today; a future
 * parameterised one (`/x/:provider/callback`) would need prefix matching added
 * back here rather than a new entry.
 */
const SIGN_IN_FLOW_PATHS = [
  SIGN_IN_PATH,
  '/setup',
  SESSION_EXPIRED_PATH,
  '/cloud/callback',
]

export interface SessionRedirectContext {
  /** Absolute Portal URL; hosted deployments only, ``null`` on self-host. */
  portalUrl: string | null
}

export interface SignInRoute {
  path: string
  query: Record<string, string>
}

export function isSignInFlowPath(path: string): boolean {
  return SIGN_IN_FLOW_PATHS.includes(path)
}

export function signInRouteFor(
  context: SessionRedirectContext,
  from: string,
): SignInRoute {
  const portal = (context.portalUrl ?? '').trim()
  return {
    path: portal ? SESSION_EXPIRED_PATH : SIGN_IN_PATH,
    query: redirectQuery(from),
  }
}

/**
 * ``?redirect=`` is replayed through the router once the player signs back in,
 * so only same-origin paths may travel in it — an absolute or protocol-relative
 * value would turn sign-in into an open redirect. The root is dropped because
 * it is where sign-in lands anyway.
 */
function redirectQuery(from: string): Record<string, string> {
  const target = from.trim()
  if (!target || target === '/') return {}
  if (!target.startsWith('/') || target.startsWith('//')) return {}
  return { redirect: target }
}
