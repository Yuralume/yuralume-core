/**
 * AdminLayout's mobile nav drawer (AM1).
 *
 * This repo has no jsdom / `@vue/test-utils` (see tests/lightbox.test.ts's
 * header for the fuller explanation), so component coverage here is two
 * halves:
 *
 *  - SSR render (`createSSRApp` + `renderToString`) asserts the *first*
 *    render: hamburger + aria wiring, the drawer starting closed, and the
 *    mobile back-link's route-dependent visibility. It cannot click the
 *    hamburger, click the scrim, press Escape, or navigate — those state
 *    transitions are unit-tested directly in tests/adminDrawer.test.ts
 *    against the pure reducer AdminLayout calls.
 *  - A source scan (like lightbox.test.ts's `LIGHTBOX_SOURCE` check) pins
 *    down the CSS contract that first-render markup can't see: the drawer
 *    is viewport-fixed only inside the ≤900px query, the scrim sits under
 *    the drawer's z-index, and desktop's base sidebar rule is untouched.
 */

import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'

function source(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8')
}

const ADMIN_LAYOUT_SOURCE = source('../src/layouts/AdminLayout.vue')

// Mutated by render() before each SSR pass; the mocked useRoute() below
// always reads the current `.path` off this same object.
const routeState = { path: '/admin' }

vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  RouterLink: {
    props: ['to'],
    setup(props: { to: string | { path: string } }, { slots }: any) {
      return () =>
        h(
          'a',
          { href: typeof props.to === 'string' ? props.to : props.to?.path },
          slots.default ? slots.default() : [],
        )
    },
  },
  RouterView: {
    setup() {
      return () => h('div', { class: 'router-view-stub' })
    },
  },
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    cloudMode: { value: false },
    debugUiEnabled: { value: false },
    buildInfo: { value: null },
  }),
}))

vi.mock('@/composables/useLocale', () => ({
  useLocale: () => ({
    locale: { value: 'zh-TW' },
    supported: { value: [{ code: 'zh-TW', label: '繁體中文' }] },
  }),
}))

// SidebarBrand.vue renders `<img src="/logo-mark.png">` — Vite's asset-URL
// transform for that root-absolute src calls `new URL(..., import.meta.url)`
// under the hood, which throws outside a browser (this SSR harness runs in
// Node). Irrelevant to the drawer this file tests, so it's stubbed rather
// than worked around.
vi.mock('@/components/SidebarBrand.vue', () => ({
  default: {
    name: 'SidebarBrandStub',
    setup() {
      return () => h('div', { class: 'sidebar-brand-stub' })
    },
  },
}))

const AdminLayout = (await import('@/layouts/AdminLayout.vue')).default

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  })
}

async function render(path: string): Promise<string> {
  routeState.path = path
  // AdminLayout reads `window.localStorage` directly at setup() time (the
  // models-nav coachmark) — stub the minimal shape, same as
  // fusionStoryExitHub.test.ts does for its own coachmark.
  ;(globalThis as { window?: unknown }).window = {
    localStorage: {
      getItem: () => null,
      setItem: () => undefined,
    },
  }
  try {
    const app = createSSRApp(AdminLayout)
    app.use(i18n())
    return await renderToString(app)
  } finally {
    delete (globalThis as { window?: unknown }).window
  }
}

const L = (zhTW as { admin: { layout: Record<string, string> } }).admin.layout

describe('AdminLayout mobile drawer (SSR render)', () => {
  it('renders the hamburger closed, with matching aria state', async () => {
    const html = await render('/admin')
    expect(html).toContain(`aria-label="${L.toggleNav}"`)
    expect(html).toMatch(/class="admin-layout__menu-btn"[^>]*aria-expanded="false"/)
  })

  it('does not render the scrim while the drawer starts closed', async () => {
    const html = await render('/admin/models')
    expect(html).not.toContain('admin-layout__scrim')
  })

  it('does not add is-open to the sidebar on first render', async () => {
    const html = await render('/admin/models')
    expect(html).not.toMatch(/admin-layout__sidebar[^"]*is-open/)
  })

  it('hides the mobile back link on the overview route itself', async () => {
    const html = await render('/admin')
    expect(html).not.toContain('admin-layout__mobile-back')
  })

  it('shows the mobile back link (and the breadcrumb current segment) on a sub-page', async () => {
    const html = await render('/admin/models')
    expect(html).toContain('admin-layout__mobile-back')
    expect(html).toContain('href="/admin"')
    expect(html).toContain(L.backToOverview)
    expect(html).toContain('admin-layout__crumb-sep')
    expect(html).toContain('is-current')
  })
})

describe('AdminLayout mobile drawer CSS contract (source scan)', () => {
  it('keeps the desktop sidebar rule untouched by the drawer', () => {
    // The base (non-media-query) `.admin-layout__sidebar` rule must not
    // gain `position: fixed` or a `transform` -- those belong only inside
    // the ≤900px query, or desktop would inherit drawer behaviour.
    const base = ADMIN_LAYOUT_SOURCE.split('@media (max-width: 900px)')[0]
    const sidebarBase = base.match(/\.admin-layout__sidebar\s*\{[^}]*\}/)?.[0] ?? ''
    expect(sidebarBase).not.toMatch(/position:\s*fixed/)
    expect(sidebarBase).not.toMatch(/transform:/)
  })

  it('only turns the sidebar into a fixed drawer inside the ≤900px query', () => {
    const mobileBlock = ADMIN_LAYOUT_SOURCE.split('@media (max-width: 900px)')[1] ?? ''
    expect(mobileBlock).toMatch(/\.admin-layout__sidebar\s*\{[^}]*position:\s*fixed/)
    expect(mobileBlock).toMatch(/\.admin-layout__sidebar\.is-open\s*\{[^}]*transform:\s*translateX\(0\)/)
  })

  it('stacks the scrim under the drawer', () => {
    // Scoped to the ≤900px block: `.admin-layout__scrim` also has a base
    // `display: none` rule with no z-index, so searching the whole file
    // non-greedily could walk straight past its closing brace into an
    // unrelated rule's z-index.
    const mobileBlock = ADMIN_LAYOUT_SOURCE.split('@media (max-width: 900px)')[1] ?? ''
    const sidebarZ = Number(mobileBlock.match(/\.admin-layout__sidebar\s*\{[^}]*z-index:\s*(\d+)/)?.[1])
    const scrimZ = Number(mobileBlock.match(/\.admin-layout__scrim\s*\{[^}]*z-index:\s*(\d+)/)?.[1])
    expect(Number.isNaN(sidebarZ)).toBe(false)
    expect(Number.isNaN(scrimZ)).toBe(false)
    expect(scrimZ).toBeLessThan(sidebarZ)
  })

  it('hides the hamburger and mobile back link outside the mobile query', () => {
    const base = ADMIN_LAYOUT_SOURCE.split('@media (max-width: 900px)')[0]
    expect(base).toMatch(/\.admin-layout__menu-btn\s*\{[^}]*display:\s*none/)
    expect(base).toMatch(/\.admin-layout__mobile-back\s*\{[^}]*display:\s*none/)
  })

  it('hides the closed drawer from tab order and assistive tech, not just visually', () => {
    // translateX alone moves the drawer off-screen but leaves its 21 nav
    // links focusable and screen-reader reachable. `visibility: hidden`
    // must accompany the closed transform, and `.is-open` must flip it
    // back to visible so the drawer is reachable while open.
    const mobileBlock = ADMIN_LAYOUT_SOURCE.split('@media (max-width: 900px)')[1] ?? ''
    const closedRule = mobileBlock.match(/\.admin-layout__sidebar\s*\{[^}]*\}/)?.[0] ?? ''
    const openRule = mobileBlock.match(/\.admin-layout__sidebar\.is-open\s*\{[^}]*\}/)?.[0] ?? ''
    expect(closedRule).toMatch(/visibility:\s*hidden/)
    expect(openRule).toMatch(/visibility:\s*visible/)
  })
})

describe('AdminLayout mobile drawer close-on-nav-click', () => {
  it('wires every nav RouterLink to close the drawer on click, including a click on the already-active item', () => {
    // vue-router does not fire a navigation (and thus does not run the
    // route.path watcher that closes the drawer) when the RouterLink's
    // target equals the current route -- so closing must not depend on
    // navigation actually happening. The nav RouterLink itself must call
    // closeSidebarDrawer on every click, active item or not.
    const navLinkBlock = ADMIN_LAYOUT_SOURCE.match(
      /<RouterLink\s+v-for="item in items"[\s\S]*?<\/RouterLink>/,
    )?.[0] ?? ''
    expect(navLinkBlock).toMatch(/@click="closeSidebarDrawer"/)
  })
})
