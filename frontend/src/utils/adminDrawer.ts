/**
 * Pure state-transition helpers for `AdminLayout`'s mobile nav drawer
 * (AM1). Kept separate from the component because the repo's SSR test
 * harness (`createSSRApp` + `renderToString`, no jsdom / `@vue/test-utils`)
 * only observes first-render markup — it can't click the hamburger, click
 * the scrim, press Escape, or navigate. Every rule that decides *whether*
 * the drawer opens or closes lives here so it still has a test gate; the
 * component itself just wires DOM events to these functions.
 */

export type AdminDrawerAction = 'toggle' | 'close'

/** Reducer for the drawer's open/closed boolean. `close` always wins. */
export function nextAdminDrawerOpen(open: boolean, action: AdminDrawerAction): boolean {
  return action === 'close' ? false : !open
}

/** Keys that should dismiss the drawer when it's open. */
export function shouldCloseAdminDrawerOnKey(key: string): boolean {
  return key === 'Escape'
}

/**
 * Whether the mobile "back to overview" affordance and the equivalent
 * breadcrumb "current page" segment should render — true on every admin
 * page except the overview itself.
 */
export function shouldShowAdminMobileBackLink(path: string): boolean {
  return path !== '/admin'
}
