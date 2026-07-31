/**
 * Where a rejected session lands.
 *
 * A hosted player signs in through the Portal; their Cloud account is
 * OAuth-only and carries no password, so Core's /login form is a dead end for
 * them — which is exactly where every 401 used to send them. These rules are
 * shared by the axios interceptor, authedFetch and the router guard so the
 * three cannot drift apart.
 */

import { describe, expect, it } from 'vitest'

import {
  SESSION_EXPIRED_PATH,
  SIGN_IN_PATH,
  isSignInFlowPath,
  signInRouteFor,
} from '@/utils/sessionRedirect'

describe('isSignInFlowPath', () => {
  it('covers every screen that is itself part of signing in', () => {
    // Bouncing from these would either loop or wipe the error the screen is
    // trying to show.
    expect(isSignInFlowPath('/login')).toBe(true)
    expect(isSignInFlowPath('/setup')).toBe(true)
    expect(isSignInFlowPath('/session-expired')).toBe(true)
    expect(isSignInFlowPath('/cloud/callback')).toBe(true)
    expect(isSignInFlowPath('/demo/oauth/discord/start')).toBe(true)
    expect(isSignInFlowPath('/demo/oauth/google/callback')).toBe(true)
  })

  it('treats ordinary player screens as bounceable', () => {
    expect(isSignInFlowPath('/')).toBe(false)
    expect(isSignInFlowPath('/studio')).toBe(false)
    expect(isSignInFlowPath('/memoir/char-1')).toBe(false)
  })

  it('does not mistake a prefix collision for a sign-in screen', () => {
    expect(isSignInFlowPath('/loginish')).toBe(false)
    expect(isSignInFlowPath('/setup-guide')).toBe(false)
  })
})

describe('signInRouteFor', () => {
  it('sends a hosted player to the expiry screen, not the password form', () => {
    const route = signInRouteFor(
      { portalUrl: 'https://app.yuralume.com' },
      '/studio',
    )

    expect(route.path).toBe(SESSION_EXPIRED_PATH)
    expect(route.query).toEqual({ redirect: '/studio' })
  })

  it('keeps self-host on the password form', () => {
    const route = signInRouteFor({ portalUrl: null }, '/studio')

    expect(route.path).toBe(SIGN_IN_PATH)
    expect(route.query).toEqual({ redirect: '/studio' })
  })

  it('falls back to the password form when a hosted deployment has no Portal', () => {
    // Demo deployments run in cloud mode without a Portal; the /login fallback
    // is the only exit they have.
    expect(signInRouteFor({ portalUrl: '   ' }, '/studio').path).toBe(SIGN_IN_PATH)
  })

  it('omits a redirect back to the root so sign-in does not carry noise', () => {
    expect(signInRouteFor({ portalUrl: null }, '/').query).toEqual({})
  })

  it('preserves the query string of the page the player was on', () => {
    const route = signInRouteFor({ portalUrl: null }, '/memoir/char-1?tab=arcs')

    expect(route.query).toEqual({ redirect: '/memoir/char-1?tab=arcs' })
  })

  it('refuses an off-site redirect target', () => {
    // ?redirect= is echoed straight into a router push after sign-in, so an
    // absolute or protocol-relative URL there is an open redirect.
    expect(signInRouteFor({ portalUrl: null }, 'https://evil.test/x').query).toEqual({})
    expect(signInRouteFor({ portalUrl: null }, '//evil.test/x').query).toEqual({})
  })
})
