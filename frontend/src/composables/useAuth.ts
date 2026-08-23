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
  refreshSession as apiRefreshSession,
  setupInitialAdmin as apiSetup,
} from '@/utils/api/auth'
import type { AuthUser, BuildInfo } from '@/utils/api/auth'
import {
  deploymentMode,
  isCloudDeployment,
  setDeploymentMode,
} from '@/composables/deploymentMode'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { notifyIdentityChanged } from '@/utils/identityLifecycle'

const TOKEN_KEY = 'kokoro_auth_token'

// Singleton state (module-level refs).
const authEnabled = ref<boolean | null>(null) // null = not yet probed
const needsSetup = ref<boolean>(false)
// The deployment mode lives in `@/composables/deploymentMode` — the cloud-only
// clients must read it without importing this module (see that file). `useAuth`
// is still the only writer, and still publishes it as `mode` / `cloudMode`.
const buildInfo = ref<BuildInfo | null>(null)
// Mirror of backend KOKORO_DEBUG_UI_ENABLED. Drives whether the SPA
// renders developer-facing admin panels — observability, experiments,
// pending follow-ups, subsystem health metrics, persona drift / pattern
// timelines. Default false so the public build hides them.
const debugUiEnabled = ref<boolean>(false)
// Absolute URL of the account Portal, advertised by hosted deployments only
// (self-host leaves it null). Drives the "back to the account centre" exits
// and the credit badge's top-up deep link — every consumer gates on it being
// non-null so self-host renders exactly as before.
const portalUrl = ref<string | null>(null)
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

/**
 * The single chokepoint for "the identity behind every request changed":
 * login, the cloud session exchange, first-admin setup, logout and the
 * 401 bounce all land here. Per-identity caches are told from this one
 * place so no new sign-in path can forget to drop the previous player's
 * data (see `@/utils/identityLifecycle` for why it is a broadcast and not
 * a direct call).
 */
function persistToken(next: string | null): void {
  const changed = token.value !== next
  writeToken(next)
  if (changed) notifyIdentityChanged()
}

/**
 * Swap the credential without announcing an identity change.
 *
 * Sliding renewal hands back a *new token for the same player*, so routing it
 * through `persistToken` would fire the identity broadcast and make every
 * per-identity cache drop and refetch — a visible stutter, several times a
 * day, for nothing. Only use this where the subject provably did not change.
 */
function rotateToken(next: string): void {
  writeToken(next)
}

function writeToken(next: string | null): void {
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
    setDeploymentMode(config.mode === 'cloud' ? 'cloud' : 'self_host')
    buildInfo.value = config.build_info ?? null
    debugUiEnabled.value = config.debug_ui_enabled === true
    portalUrl.value = (config.portal_url ?? '').trim() || null

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

async function loginWithCloudSession(payload: {
  code: string
}): Promise<void> {
  const res = await apiLoginWithCloudSession(payload)
  persistToken(res.token)
  currentUser.value = res.user
  needsSetup.value = false
  setDeploymentMode('cloud')
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

/**
 * Re-read `GET /auth/me`.
 *
 * The hosted locale lifecycle (plan G2) lives on that payload:
 * `needs_locale_confirmation` drives the blocking onboarding gate and
 * `location_hint` drives the "you seem to be somewhere else" bar. Both are
 * server-owned, so after confirming / changing / accepting anything the SPA
 * re-reads rather than guessing the next state locally.
 */
async function refreshMe(): Promise<void> {
  currentUser.value = await fetchMe()
  applyUserRuntimePreferences(currentUser.value)
}

/**
 * Extend the current session. Same player, fresh credential — the token is
 * rotated in place so per-identity caches are left intact.
 *
 * Throws on failure; callers treat that as "carry on with the token we have"
 * because the global 401 interceptor already owns the terminal case.
 */
async function renewSession(): Promise<void> {
  const res = await apiRefreshSession()
  rotateToken(res.token)
  currentUser.value = res.user
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
    mode: deploymentMode,
    cloudMode: computed(() => isCloudDeployment()),
    buildInfo: computed(() => buildInfo.value),
    debugUiEnabled: computed(() => debugUiEnabled.value),
    portalUrl: computed(() => portalUrl.value),
    currentUser: computed(() => currentUser.value),
    token: computed(() => token.value),
    isAuthenticated: computed(
      () => authEnabled.value === false || currentUser.value !== null,
    ),
    isAdmin: computed(() => currentUser.value?.is_admin === true),
    /**
     * Hosted-only: this player has not yet acknowledged the GeoIP-seeded
     * place / timezone / language. Double-gated on cloud mode so a stale
     * flag could never block a self-host operator.
     */
    needsLocaleConfirmation: computed(
      () => isCloudDeployment()
        && currentUser.value?.needs_locale_confirmation === true,
    ),
    /** Hosted-only relocation suggestion; never an applied fact. */
    locationHint: computed(
      () => (isCloudDeployment()
        ? currentUser.value?.location_hint ?? null
        : null),
    ),

    // actions
    bootstrapAuth,
    refreshMe,
    renewSession,
    login,
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
