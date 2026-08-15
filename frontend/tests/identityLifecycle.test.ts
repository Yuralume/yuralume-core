import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// `useAuth` reads localStorage at module scope and this repo runs vitest in
// the node environment (no jsdom — see cloudCreditsSurface.test.ts). A
// hoisted stub installs the storage before the module graph is imported.
vi.hoisted(() => {
  const store = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
    },
  })
})

vi.mock('@/utils/api/auth', () => ({
  fetchMe: vi.fn(),
  getAuthConfig: vi.fn(),
  login: vi.fn(),
  loginWithCloudSession: vi.fn(),
  setupInitialAdmin: vi.fn(),
}))

// Locale / timezone side effects are irrelevant here and pull in the i18n
// runtime; stub them so the test is about the identity broadcast only.
vi.mock('@/composables/useLocale', () => ({
  useLocale: () => ({ applyPrimaryLanguage: vi.fn() }),
}))
vi.mock('@/composables/useTimezone', () => ({
  useTimezone: () => ({
    applyUserTimezone: vi.fn(),
    resetToBrowserTimezone: vi.fn(),
  }),
}))

const {
  notifyIdentityChanged,
  onIdentityChanged,
  resetIdentityListenersForTest,
} = await import('@/utils/identityLifecycle')
const authApi = await import('@/utils/api/auth')
const { clearStoredToken, useAuth } = await import('@/composables/useAuth')

type AuthUser = import('@/utils/api/auth').AuthUser

const USER: AuthUser = {
  id: 'u1',
  display_name: 'Alex',
  email: 'alex@example.com',
  is_admin: false,
  primary_language: 'zh-TW',
  timezone_id: 'Asia/Taipei',
  country_code: 'TW',
  latitude: null,
  longitude: null,
  location_label: null,
}

function session(token: string) {
  return { token, user: USER }
}

beforeEach(() => {
  vi.clearAllMocks()
  resetIdentityListenersForTest()
  localStorage.clear()
})

afterEach(() => {
  resetIdentityListenersForTest()
})

describe('identity change broadcast', () => {
  it('tells every subscriber, once, per change', () => {
    const first = vi.fn()
    const second = vi.fn()
    onIdentityChanged(first)
    onIdentityChanged(second)

    notifyIdentityChanged()

    expect(first).toHaveBeenCalledTimes(1)
    expect(second).toHaveBeenCalledTimes(1)
  })

  it('stops telling a subscriber that unsubscribed', () => {
    const listener = vi.fn()
    const off = onIdentityChanged(listener)

    off()
    notifyIdentityChanged()

    expect(listener).not.toHaveBeenCalled()
  })

  it('a cache that throws cannot block the sign-out', () => {
    const exploding = vi.fn(() => {
      throw new Error('cache is on fire')
    })
    const survivor = vi.fn()
    onIdentityChanged(exploding)
    onIdentityChanged(survivor)

    expect(() => notifyIdentityChanged()).not.toThrow()
    expect(survivor).toHaveBeenCalledTimes(1)
  })
})

describe('useAuth announces every identity change', () => {
  it('announces on password login', async () => {
    const listener = vi.fn()
    onIdentityChanged(listener)
    vi.mocked(authApi.login).mockResolvedValueOnce(session('t1'))

    await useAuth().login('alex@example.com', 'pw')

    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('announces on the cloud session exchange', async () => {
    const listener = vi.fn()
    onIdentityChanged(listener)
    vi.mocked(authApi.loginWithCloudSession).mockResolvedValueOnce(session('t3'))

    await useAuth().loginWithCloudSession({ code: 'c' })

    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('announces on logout', async () => {
    vi.mocked(authApi.login).mockResolvedValueOnce(session('t4'))
    await useAuth().login('alex@example.com', 'pw')

    const listener = vi.fn()
    onIdentityChanged(listener)
    useAuth().logout()

    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('announces when a stale token is dropped by the 401 bounce', async () => {
    vi.mocked(authApi.login).mockResolvedValueOnce(session('t5'))
    await useAuth().login('alex@example.com', 'pw')

    const listener = vi.fn()
    onIdentityChanged(listener)
    clearStoredToken()

    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('stays quiet when the token did not actually move', () => {
    useAuth().logout()

    // A second logout is not an identity change and must not make every
    // cache throw away a snapshot it just legitimately fetched.
    const listener = vi.fn()
    onIdentityChanged(listener)
    useAuth().logout()

    expect(listener).not.toHaveBeenCalled()
  })
})
