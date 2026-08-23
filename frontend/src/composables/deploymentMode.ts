/**
 * Which deployment this SPA is talking to — hosted (`cloud`) or `self_host`.
 *
 * A leaf module: one ref, no imports beyond Vue. That is the point. The value
 * is *learned* by `useAuth` from `GET /auth/config`, but it is not auth state,
 * and routing the read through `useAuth` would put a module that touches
 * `localStorage` at import time into the import chain of every transport util
 * that quotes a price. `useAuth` re-exports it as `mode` / `cloudMode`, so no
 * consumer needs to know it moved.
 *
 * ## Why the cloud-only clients read it
 *
 * `useCloudCredits`, `useCloudAnnouncements`, `useActionPricing`,
 * `useOverageSettings` and `useRuntimeLimits` each talk to a route that a
 * self-host install does not mount. Each also carries a `supported` flag, but
 * that flag answers a different question — "did the backend answer 404 to a
 * request we already sent?" — and can only be set by sending the pointless
 * request first. Mount-time callers do guard themselves; the fire-and-forget
 * post-action refreshes never did, which is how self-host ended up issuing a
 * real `GET /cloud/credits` after every charged action until the 404 latched.
 *
 * ## Why it is read live and never latched
 *
 * The honest default is `self_host`: until the probe answers there is no
 * evidence of a Cloud. So a hosted deployment spends its first moments looking
 * self-host, and any consumer that cached a reading taken during that window
 * would stay wrong for the rest of the session.
 */

import { computed, ref } from 'vue'

export type DeploymentMode = 'self_host' | 'cloud'

const mode = ref<DeploymentMode>('self_host')

/** The mode itself, read-only. */
export const deploymentMode = computed(() => mode.value)

/** Call once the `/auth/config` probe (or a cloud session login) answers. */
export function setDeploymentMode(next: DeploymentMode): void {
  mode.value = next
}

/**
 * True on a hosted deployment. A plain function rather than a `computed` so
 * callers cannot hold onto a stale reading; used inside `computed`s it still
 * tracks, because it reads the ref.
 */
export function isCloudDeployment(): boolean {
  return mode.value === 'cloud'
}
