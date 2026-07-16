/**
 * Auth state composable.
 *
 * Module-scope refs make this a de-facto singleton store without
 * pulling pinia in (the project keeps its dependency surface lean).
 * Every component that imports `useAuth()` shares the same state.
 *
 * Lifecycle:
 *   - main.ts calls `bootstrapAuth()` once on startup -> fills
 *     `authEnabled` + `needsSetup` from GET /auth/config, and if a
 *     stored token exists, validates via GET /auth/me.
 *   - Router beforeEach reads authEnabled / token / currentUser to
 *     decide /login vs /setup vs allow.
 *   - LoginPage / SetupPage call login() / setup() which persist the
 *     token to localStorage and refresh currentUser.
 *   - logout() clears the token + redirects to /login (caller decides
 *     when to call router.push).
 */

import { ref, computed } from 'vue'
import {
  fetchMe,
  getAuthConfig,
  login as apiLogin,
  loginWithCloudSession as apiLoginWithCloudSession,
  loginWithDemoSession as apiLoginWithDemoSession,
  setupInitialAdmin as apiSetup,
} from '@/utils/api/auth'
import type { AuthUser, BuildInfo } from '@/utils/api/auth'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'

const TOKEN_KEY = 'kokoro_auth_token'

// Singleton state (module-level refs).
const authEnabled = ref<boolean | null>(null) // null = not yet probed
const needsSetup = ref<boolean>(false)
const mode = ref<'self_host' | 'cloud'>('self_host')
const buildInfo = ref<BuildInfo | null>(null)
// Mirror of backend KOKORO_DEBUG_UI_ENABLED. Drives whether the SPA
// renders developer-facing admin panels — observability, experiments,
// pending follow-ups, subsystem health metrics, persona drift / pattern
// timelines. Default false so the public build hides them.
const debugUiEnabled = ref<boolean>(false)
const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
const currentUser = ref<AuthUser | null>(null)
const bootstrapping = ref<boolean>(false)
const bootstrapped = ref<boolean>(false)

function applyUserRuntimePreferences(user: AuthUser | null): void {
  if (!user) return
  if (user.primary_language) {
    useLocale().applyPrimaryLanguage(user.primary_language)
  }
  useTimezone().applyUserTimezone(user.timezone_id)
}

function persistToken(next: string | null): void {
  token.value = next
  if (next) {
    localStorage.setItem(TOKEN_KEY, next)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

/**
 * Probe /auth/config on startup. Resolves once we know whether the
 * front-end should bother routing through /login. Safe to call
 * multiple times (no-op after first success).
 */
async function bootstrapAuth(): Promise<void> {
  if (bootstrapped.value || bootstrapping.value) return
  bootstrapping.value = true
  try {
    const config = await getAuthConfig()
    authEnabled.value = config.auth_enabled
    needsSetup.value = config.needs_setup
    mode.value = config.mode === 'cloud' ? 'cloud' : 'self_host'
    buildInfo.value = config.build_info ?? null
    debugUiEnabled.value = config.debug_ui_enabled === true

    if (config.auth_enabled && token.value) {
      // Validate stored token. If it doesn't resolve to a user the
      // backend rejected it (revoked / expired / different secret).
      try {
        currentUser.value = await fetchMe()
      } catch {
        persistToken(null)
        currentUser.value = null
      }
    } else if (!config.auth_enabled) {
      // Disabled-mode: /auth/me still returns the default user so
      // the UI can show "logged in as 操作者" if it wants — but most
      // surfaces just check `authEnabled` and skip the badge.
      try {
        currentUser.value = await fetchMe()
      } catch {
        currentUser.value = null
      }
    }
    // Authenticated identity is the runtime source for both user-visible
    // civil time and the first UI language shown after login/token
    // bootstrap. This avoids carrying a previous player's locale across
    // accounts on shared browsers.
    applyUserRuntimePreferences(currentUser.value)
    bootstrapped.value = true
  } finally {
    bootstrapping.value = false
  }
}

async function login(email: string, password: string): Promise<void> {
  const res = await apiLogin(email, password)
  persistToken(res.token)
  currentUser.value = res.user
  needsSetup.value = false
  applyUserRuntimePreferences(res.user)
}

async function loginWithDemoSession(payload: {
  provider: string
  authorization_code: string
  redirect_uri?: string | null
  code_verifier?: string | null
}): Promise<void> {
  const res = await apiLoginWithDemoSession(payload)
  persistToken(res.token)
  currentUser.value = res.user
  needsSetup.value = false
  mode.value = 'cloud'
  applyUserRuntimePreferences(res.user)
}

async function loginWithCloudSession(payload: {
  code: string
}): Promise<void> {
  const res = await apiLoginWithCloudSession(payload)
  persistToken(res.token)
  currentUser.value = res.user
  needsSetup.value = false
  mode.value = 'cloud'
  applyUserRuntimePreferences(res.user)
}

async function setup(
  email: string,
  password: string,
  primaryLanguage: string,
  timezoneId: string,
  location?: {
    country_code?: string | null
    latitude?: number | null
    longitude?: number | null
    location_label?: string | null
  },
): Promise<void> {
  const res = await apiSetup(
    email,
    password,
    primaryLanguage,
    timezoneId,
    location,
  )
  persistToken(res.token)
  currentUser.value = res.user
  needsSetup.value = false
  applyUserRuntimePreferences(res.user)
}

function logout(): void {
  persistToken(null)
  currentUser.value = null
  useTimezone().resetToBrowserTimezone()
}

export function useAuth() {
  return {
    // state (readonly via refs — consumers should treat as such)
    authEnabled: computed(() => authEnabled.value === true),
    authProbed: computed(() => authEnabled.value !== null),
    needsSetup: computed(() => needsSetup.value),
    mode: computed(() => mode.value),
    cloudMode: computed(() => mode.value === 'cloud'),
    buildInfo: computed(() => buildInfo.value),
    debugUiEnabled: computed(() => debugUiEnabled.value),
    currentUser: computed(() => currentUser.value),
    token: computed(() => token.value),
    isAuthenticated: computed(
      () => authEnabled.value === false || currentUser.value !== null,
    ),
    isAdmin: computed(() => currentUser.value?.is_admin === true),

    // actions
    bootstrapAuth,
    login,
    loginWithDemoSession,
    loginWithCloudSession,
    setup,
    logout,
  }
}

export function getStoredToken(): string | null {
  return token.value
}

export function clearStoredToken(): void {
  persistToken(null)
  currentUser.value = null
  useTimezone().resetToBrowserTimezone()
}
