/**
 * "Who is logged in changed" broadcast — the seam that lets per-identity
 * caches drop their contents without anyone importing anyone else's module.
 *
 * Why this exists rather than a direct call:
 *
 *   `useAuth` owns the identity. Per-identity caches (`useCloudCredits`
 *   today, more later) reach the backend through `authedFetch`, which
 *   imports `useAuth` — so a cache importing `useAuth` is fine, but
 *   `useAuth` importing a cache would close the loop
 *   (`useAuth` → cache → api → authedFetch → `useAuth`). Past attempts to
 *   dodge that cycle pushed the reset out to whichever component happened
 *   to render the logout button, which only covered logout and left login /
 *   account switch leaking the previous player's numbers.
 *
 * This module imports nothing, so both sides can depend on it: `useAuth`
 * announces, caches subscribe. Listeners are announced synchronously and a
 * throwing listener can never break a logout — clearing a token must always
 * finish.
 */

export type IdentityChangeListener = () => void

const listeners = new Set<IdentityChangeListener>()

/**
 * Register a cache invalidator. Returns the unsubscribe handle; module-scope
 * singletons subscribe for the life of the page and simply drop it.
 */
export function onIdentityChanged(listener: IdentityChangeListener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/**
 * Announce that the authenticated identity is no longer the one the caches
 * were filled for — login, account switch, logout, or a token revoked
 * underneath us. Never throws.
 */
export function notifyIdentityChanged(): void {
  for (const listener of [...listeners]) {
    try {
      listener()
    } catch {
      /* a cache that cannot clear itself must not block the sign-out */
    }
  }
}

/** Test seam: drop every subscription. Never call from application code. */
export function resetIdentityListenersForTest(): void {
  listeners.clear()
}
