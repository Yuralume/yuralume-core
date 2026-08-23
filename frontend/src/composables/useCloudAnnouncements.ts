/**
 * Hosted notice-board unread state for the player surfaces (AN1).
 *
 * Module-scope refs make this a de-facto singleton, matching `useCloudCredits`
 * whose badge the dot sits beside.
 *
 * Lifecycle:
 *   - `ensureLoaded()` on first mount of a cloud player surface.
 *   - `refreshOnReturn()` when the tab becomes visible again — the player may
 *     have just read the board in the Portal, and leaving a stale dot up is the
 *     one failure mode that makes the feature feel broken.
 *   - `reset()` whenever the authenticated identity changes, driven by
 *     `onIdentityChanged` so a shared browser never carries one player's unread
 *     state into another's session.
 *
 * As with the balance, a read that was already in flight when the identity
 * changed must not write the previous player's state back in, so every read
 * carries the `generation` it was issued under.
 *
 * Fail-soft: a `404` means there is no board and the dot hides for good; any
 * other failure keeps the last-known value. The dot is never invented, and
 * never cleared on a failed read — hiding a notice is the costly direction.
 */

import { computed, ref } from 'vue'

import { isCloudDeployment } from '@/composables/deploymentMode'
import {
  fetchCloudAnnouncements,
  type CloudAnnouncementsSnapshot,
} from '@/utils/api/cloudAnnouncements'
import { onIdentityChanged } from '@/utils/identityLifecycle'

const unread = ref<CloudAnnouncementsSnapshot | null>(null)
const loading = ref(false)
/** False once the backend answered 404 — no board for this deployment. */
const supported = ref(true)
const loaded = ref(false)
let inFlight: Promise<void> | null = null
let generation = 0

async function load(options: { refresh?: boolean } = {}): Promise<void> {
  // No board on self-host, and the route is not mounted there either. The dot
  // guards its own mount, but the gate belongs here so a future caller cannot
  // reopen the hole by forgetting to.
  if (!isCloudDeployment()) return
  if (!supported.value) return
  if (inFlight) return inFlight
  const issuedAt = generation
  loading.value = true
  inFlight = (async () => {
    try {
      const result = await fetchCloudAnnouncements(options)
      if (issuedAt !== generation) return
      if (result.kind === 'ok') {
        unread.value = result.snapshot
        loaded.value = true
      } else if (result.kind === 'unsupported') {
        supported.value = false
        unread.value = null
        loaded.value = true
      } else if (unread.value !== null) {
        // Degraded with something already on screen: keep it, and consider the
        // state loaded — the dot is showing a real, if slightly old, answer.
        loaded.value = true
      }
      // Degraded with nothing known stays UNloaded on purpose. Marking it loaded
      // would make `ensureLoaded()` a no-op for the rest of the session, and
      // since an absent snapshot renders as "no unread", a single failed first
      // read would hide every notice until the player reloaded the page.
    } finally {
      if (issuedAt === generation) {
        loading.value = false
        inFlight = null
      }
    }
  })()
  return inFlight
}

function ensureLoaded(): Promise<void> {
  if (loaded.value || loading.value) return inFlight ?? Promise.resolve()
  return load()
}

/**
 * Re-read after the player has been away — most likely in the Portal, reading
 * the very notice this dot is pointing at. Never awaited, never throws.
 */
export function refreshCloudAnnouncements(): void {
  if (!supported.value) return
  void load({ refresh: true }).catch(() => {
    /* the dot is best-effort — never disturb the surface it sits on */
  })
}

function reset(): void {
  generation += 1
  unread.value = null
  supported.value = true
  loaded.value = false
  loading.value = false
  inFlight = null
}

onIdentityChanged(reset)

export function useCloudAnnouncements() {
  return {
    hasUnread: computed(() => unread.value?.has_unread === true),
    unreadCount: computed(() => unread.value?.unread_count ?? 0),
    stale: computed(() => unread.value?.stale === true),
    loading: computed(() => loading.value),

    ensureLoaded,
    refreshOnReturn: refreshCloudAnnouncements,
    reset,
  }
}
