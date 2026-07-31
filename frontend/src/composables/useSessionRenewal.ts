/**
 * Keeps a player who is actually here signed in.
 *
 * Browser wiring only — listeners and the poll timer. The liveness tracking
 * and the "is renewal needed and earned" decision live in
 * `@/utils/sessionRenewal`, which stays DOM-free so it can be tested.
 */

import { onBeforeUnmount, onMounted } from 'vue'

import { getStoredToken, useAuth } from '@/composables/useAuth'
import { createSessionRenewer } from '@/utils/sessionRenewal'

/** How often to re-evaluate. Cheap: local arithmetic, no I/O. */
const CHECK_INTERVAL_MS = 60 * 1000

/**
 * Events that count as "the player is here". Pointer/keyboard/scroll cover
 * active use; focus and visibility cover the far more common hosted pattern of
 * returning to a tab left open — also the moment a renewal is most likely to
 * be both needed and earned.
 */
const ACTIVITY_EVENTS = ['pointerdown', 'keydown', 'scroll'] as const

export function useSessionRenewal() {
  const auth = useAuth()
  const renewer = createSessionRenewer({
    isEnabled: () => auth.authEnabled.value,
    readToken: getStoredToken,
    renew: auth.renewSession,
  })

  let timer: ReturnType<typeof setInterval> | null = null

  const markActive = () => renewer.markActive()

  function onVisibilityChange(): void {
    if (document.visibilityState !== 'visible') return
    renewer.markActive()
    // Coming back to a long-open tab is the classic "about to lapse" moment;
    // don't make the player wait out the poll interval for it.
    void renewer.maybeRenew()
  }

  function start(): void {
    if (timer !== null) return
    for (const event of ACTIVITY_EVENTS) {
      window.addEventListener(event, markActive, { passive: true })
    }
    window.addEventListener('focus', markActive)
    document.addEventListener('visibilitychange', onVisibilityChange)
    timer = setInterval(() => void renewer.maybeRenew(), CHECK_INTERVAL_MS)
  }

  function stop(): void {
    if (timer === null) return
    for (const event of ACTIVITY_EVENTS) {
      window.removeEventListener(event, markActive)
    }
    window.removeEventListener('focus', markActive)
    document.removeEventListener('visibilitychange', onVisibilityChange)
    clearInterval(timer)
    timer = null
  }

  onMounted(start)
  onBeforeUnmount(stop)

  return { start, stop }
}
