import axios from 'axios'

import type { LocationHint } from '@/types/playerLocale'

const BASE = '/api/v1/auth'

export interface AuthUser {
  id: string
  display_name: string
  /**
   * True when `display_name` is still the seeded `操作者` placeholder
   * (operator skipped naming). Render a localized label instead of the
   * raw sentinel at the display boundary — the stored value is never
   * mutated.
   */
  display_name_is_placeholder?: boolean
  email: string | null
  is_admin: boolean
  /**
   * BCP 47 tag picked at registration / setup. Immutable in self-host
   * (repair CLI only); hosted players may change it through the guarded
   * `PATCH /auth/me/locale` (plan G2). The frontend reads this to:
   *  1. switch the UI locale after login/token bootstrap,
   *  2. render the "primary language" field in settings — read-only on
   *     self-host, editable-with-a-warning in cloud mode.
   */
  primary_language: string
  /**
   * IANA timezone used only for user-facing civil-time display. Backend
   * storage and server scheduling remain UTC.
   */
  timezone_id: string
  country_code: string | null
  latitude: number | null
  longitude: number | null
  location_label: string | null
  /**
   * Hosted locale lifecycle projection, served **only** by `GET /auth/me`
   * (plan G2). Both are additive and always falsy in self-host, so every
   * consumer must treat them as optional: login / setup / user-CRUD
   * payloads keep their exact previous shape and never carry them.
   */
  needs_locale_confirmation?: boolean
  location_hint?: LocationHint | null
}

export interface BuildMetadata {
  image_tag: string | null
  commit_sha: string | null
  built_at: string | null
}

export interface BuildInfo {
  name: string
  version: string
  api_version: string
  build: BuildMetadata
}

export interface AuthConfig {
  auth_enabled: boolean
  needs_setup: boolean
  mode?: 'self_host' | 'cloud'
  build_info?: BuildInfo
  /**
   * Mirror of backend ``AppSettings.debug_ui_enabled`` (env
   * ``KOKORO_DEBUG_UI_ENABLED``). When false the SPA hides
   * developer-facing admin panels — observability, experiments,
   * pending-follow-ups, subsystem health metrics, persona drift / pattern
   * timelines — so the public build stays clean. Backend admin APIs
   * remain reachable either way.
   */
  debug_ui_enabled?: boolean
  /**
   * Absolute URL of the Yuralume account Portal, advertised only by hosted
   * (cloud) deployments that configured `YURALUME_CLOUD_PORTAL_URL`.
   * Self-host always returns null, so every consumer must treat the link as
   * optional rather than assuming a Portal exists.
   */
  portal_url?: string | null
}

export interface AuthTokenResponse {
  user: AuthUser
  token: string
}

export async function getAuthConfig(): Promise<AuthConfig> {
  const { data } = await axios.get<AuthConfig>(`${BASE}/config`)
  return data
}

export async function setupInitialAdmin(
  email: string,
  password: string,
  primaryLanguage: string,
  timezoneId?: string,
  location?: {
    country_code?: string | null
    latitude?: number | null
    longitude?: number | null
    location_label?: string | null
  },
): Promise<AuthTokenResponse> {
  const { data } = await axios.post<AuthTokenResponse>(`${BASE}/setup`, {
    email,
    password,
    primary_language: primaryLanguage,
    timezone_id: timezoneId,
    ...(location ?? {}),
  })
  return data
}

export async function login(
  email: string,
  password: string,
): Promise<AuthTokenResponse> {
  const { data } = await axios.post<AuthTokenResponse>(`${BASE}/login`, {
    email,
    password,
  })
  return data
}

export async function loginWithCloudSession(payload: {
  code: string
}): Promise<AuthTokenResponse> {
  const { data } = await axios.post<AuthTokenResponse>(
    `${BASE}/cloud/session`,
    payload,
  )
  return data
}

/**
 * Slide the current session forward (plan: sliding renewal). The SPA calls
 * this only after real interaction — see `@/utils/sessionRenewal`. A 401 means
 * the session may not be extended (expired, revoked, or past the deployment's
 * absolute cap) and is handled by the global bounce like any other 401.
 */
export async function refreshSession(): Promise<AuthTokenResponse> {
  const { data } = await axios.post<AuthTokenResponse>(`${BASE}/refresh`)
  return data
}

export async function fetchMe(): Promise<AuthUser> {
  const { data } = await axios.get<AuthUser>(`${BASE}/me`)
  return data
}

export async function listUsers(): Promise<AuthUser[]> {
  const { data } = await axios.get<AuthUser[]>(`${BASE}/users`)
  return data
}

export async function createUser(payload: {
  email: string
  password: string
  display_name: string
  is_admin: boolean
  primary_language?: string
  timezone_id?: string
  country_code?: string | null
  latitude?: number | null
  longitude?: number | null
  location_label?: string | null
}): Promise<AuthUser> {
  const { data } = await axios.post<AuthUser>(`${BASE}/users`, payload)
  return data
}

export async function deleteUser(userId: string): Promise<void> {
  await axios.delete(`${BASE}/users/${userId}`)
}

export async function setUserAdmin(
  userId: string,
  isAdmin: boolean,
): Promise<AuthUser> {
  const { data } = await axios.patch<AuthUser>(
    `${BASE}/users/${userId}/admin`,
    { is_admin: isAdmin },
  )
  return data
}

export async function changePassword(
  userId: string,
  newPassword: string,
): Promise<AuthUser> {
  const { data } = await axios.post<AuthUser>(
    `${BASE}/users/${userId}/password`,
    { new_password: newPassword },
  )
  return data
}

export async function changeOwnPassword(
  currentPassword: string,
  newPassword: string,
): Promise<AuthUser> {
  const { data } = await axios.post<AuthUser>(
    `${BASE}/me/password`,
    {
      current_password: currentPassword,
      new_password: newPassword,
    },
  )
  return data
}
