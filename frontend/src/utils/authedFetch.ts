/**
 * ``authedFetch`` — drop-in replacement for the native ``fetch`` that
 * injects the stored bearer token into ``Authorization``.
 *
 * The repo has six API client modules under ``src/utils/api/*.ts`` that
 * predate axios and still use raw ``fetch()``. Once multi-user auth
 * landed those calls started 401-ing because only axios runs through
 * the global Authorization interceptor (see ``main.ts``). Re-routing
 * them through axios would touch a lot of code; this wrapper keeps the
 * existing call sites intact — they just swap ``fetch`` for
 * ``authedFetch``.
 *
 * Behaviour:
 *   - Reads the stored token via :func:`getStoredToken`.
 *   - Adds ``Authorization: Bearer <token>`` when present. Existing
 *     headers (e.g. ``Content-Type``) are preserved.
 *   - On 401, kicks the user back to their deployment's sign-in surface
 *     (mirrors the axios interceptor) so a stale token doesn't manifest
 *     as cryptic UI errors deep inside polling loops.
 *   - Passes through 4xx/5xx as-is; callers continue to inspect
 *     ``res.ok`` / ``res.status`` like before.
 */

import { clearStoredToken, getStoredToken, useAuth } from '@/composables/useAuth'
import router from '@/router'
import { bounceToSignIn } from '@/utils/sessionBounce'

export async function authedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const token = getStoredToken()
  const headers = new Headers(init.headers || {})
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  const res = await fetch(input, { ...init, headers })
  if (res.status === 401) {
    bounceToSignIn({
      router,
      clearToken: clearStoredToken,
      portalUrl: useAuth().portalUrl.value,
    })
  }
  return res
}
