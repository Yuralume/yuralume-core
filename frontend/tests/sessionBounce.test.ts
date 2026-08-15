/**
 * The 401 bounce shared by the axios interceptor and authedFetch.
 *
 * The bug this closes: hosted players were dropped on ``/login``, a password
 * form their OAuth-only Cloud account can never satisfy. With a 12h hosted
 * session TTL that happened to every player, twice a day.
 */

import { describe, expect, it, vi } from 'vitest'

import { bounceToSignIn } from '@/utils/sessionBounce'

function fakeRouter() {
  return { replace: vi.fn() }
}

describe('bounceToSignIn', () => {
  it('sends a hosted player to the Portal exit and drops the dead token', () => {
    const router = fakeRouter()
    const clearToken = vi.fn()

    const bounced = bounceToSignIn({
      router,
      clearToken,
      location: { pathname: '/studio', search: '?tab=cards' },
      portalUrl: 'https://app.yuralume.com',
    })

    expect(bounced).toBe(true)
    expect(clearToken).toHaveBeenCalledOnce()
    expect(router.replace).toHaveBeenCalledWith({
      path: '/session-expired',
      query: { redirect: '/studio?tab=cards' },
    })
  })

  it('keeps a self-host operator on the password form', () => {
    const router = fakeRouter()

    bounceToSignIn({
      router,
      clearToken: vi.fn(),
      location: { pathname: '/studio', search: '' },
      portalUrl: null,
    })

    expect(router.replace).toHaveBeenCalledWith({
      path: '/login',
      query: { redirect: '/studio' },
    })
  })

  it.each([
    '/login',
    '/setup',
    '/session-expired',
    '/cloud/callback',
  ])('leaves %s alone so it can show its own error', (pathname) => {
    const router = fakeRouter()
    const clearToken = vi.fn()

    const bounced = bounceToSignIn({
      router,
      clearToken,
      location: { pathname, search: '' },
      portalUrl: 'https://app.yuralume.com',
    })

    // A wrong password is a 401 too — bouncing here would wipe the message
    // and, on the expiry screen itself, loop.
    expect(bounced).toBe(false)
    expect(clearToken).not.toHaveBeenCalled()
    expect(router.replace).not.toHaveBeenCalled()
  })
})
