import { registerSW } from 'virtual:pwa-register'

const UPDATE_CHECK_INTERVAL_MS = 60 * 60 * 1000

/**
 * With `registerType: 'autoUpdate'` (vite.config.ts) the register wrapper
 * reloads the page on its own once a new service worker activates -- it
 * never prompts (that's `onNeedRefresh`, only used by `registerType:
 * 'prompt'`). What it does *not* do is check for updates on its own: a
 * long-lived SPA tab may never trigger the browser's navigation-based
 * check, so `onRegisteredSW` polls `registration.update()`.
 */
export function registerServiceWorker(): void {
  registerSW({
    onRegisteredSW(_swUrl, registration) {
      if (!registration) return
      setInterval(() => {
        void registration.update()
      }, UPDATE_CHECK_INTERVAL_MS)
    },
    onRegisterError(error) {
      console.error('[sw] registration failed', error)
    },
  })
}
