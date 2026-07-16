import axios from 'axios'

const BASE = '/api/v1/auth'

export interface AuthUser {
  id: string
  display_name: string
  /**
   * True when `display_name` is still the seeded `操作者` placeholder
   * (operator skipped naming). Render a localized label instead of the
   * raw sentinel at the display boundary — the stored value is never
   * mutated (see SHIPPED_CONTENT_LOCALIZATION_PLAN #6).
   */
  display_name_is_placeholder?: boolean
  email: string | null
  is_admin: boolean
  /**
   * BCP 47 tag picked at registration / setup. Immutable on the
   * backend (no PATCH endpoint). The frontend reads this to:
   *  1. switch the UI locale after login/token bootstrap,
   *  2. display the "primary language" read-only field in settings.
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
}

export interface AuthTokenResponse {
  user: AuthUser
  token: string
}

export class DemoSessionLoginError extends Error {
  code: string
  statusCode: number
  retryable: boolean

  constructor(input: {
    code: string
    message: string
    retryable: boolean
    statusCode: number
  }) {
    super(input.message)
    this.name = 'DemoSessionLoginError'
    this.code = input.code
    this.statusCode = input.statusCode
    this.retryable = input.retryable
  }
}

export async function getAuthConfig(): Promise<AuthConfig> {
  const { data } = await axios.get<AuthConfig>(`${BASE}/config`)
  return data
}

interface DemoOAuthConfigResponse {
  providers?: Record<string, { client_id?: string } | undefined>
}

/**
 * Public demo OAuth client ids fetched at runtime (plan Phase 5.1), so the SPA no
 * longer needs them baked into the Vite build. Returns a map keyed by provider;
 * a missing/empty id keeps the downstream missing-client-id fail-fast.
 */
export async function getDemoOAuthConfig(): Promise<Record<string, string>> {
  const { data } = await axios.get<DemoOAuthConfigResponse>(`${BASE}/demo/oauth/config`)
  const ids: Record<string, string> = {}
  for (const [provider, config] of Object.entries(data.providers ?? {})) {
    const clientId = (config?.client_id ?? '').trim()
    if (clientId) {
      ids[provider] = clientId
    }
  }
  return ids
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

export async function loginWithDemoSession(payload: {
  provider: string
  authorization_code: string
  redirect_uri?: string | null
  code_verifier?: string | null
}): Promise<AuthTokenResponse> {
  try {
    const { data } = await axios.post<AuthTokenResponse>(
      `${BASE}/demo/session`,
      payload,
    )
    return data
  } catch (error) {
    const demoError = demoSessionLoginErrorFromAxios(error)
    if (demoError) throw demoError
    throw error
  }
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

function demoSessionLoginErrorFromAxios(error: unknown): DemoSessionLoginError | null {
  const response = responseFromUnknown(error)
  if (!response) return null
  const body = errorBodyFromUnknown(response.data)
  if (!body) return null
  const code = stringField(body, 'code')
  const message = stringField(body, 'message')
  if (!code || !message) return null
  return new DemoSessionLoginError({
    code,
    message,
    retryable: boolField(body, 'retryable', response.status >= 500),
    statusCode: response.status,
  })
}

function responseFromUnknown(error: unknown): {
  status: number
  data: unknown
} | null {
  if (!error || typeof error !== 'object') return null
  const response = (error as { response?: unknown }).response
  if (!response || typeof response !== 'object') return null
  const status = (response as { status?: unknown }).status
  if (typeof status !== 'number') return null
  return {
    status,
    data: (response as { data?: unknown }).data,
  }
}

function errorBodyFromUnknown(data: unknown): Record<string, unknown> | null {
  if (!data || typeof data !== 'object') return null
  const payload = data as Record<string, unknown>
  if (payload.error && typeof payload.error === 'object') {
    return payload.error as Record<string, unknown>
  }
  if (!payload.detail || typeof payload.detail !== 'object') return null
  const detail = payload.detail as Record<string, unknown>
  if (!detail.error || typeof detail.error !== 'object') return null
  return detail.error as Record<string, unknown>
}

function stringField(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key]
  if (typeof value !== 'string') return null
  const text = value.trim()
  return text || null
}

function boolField(
  payload: Record<string, unknown>,
  key: string,
  fallback: boolean,
): boolean {
  const value = payload[key]
  return typeof value === 'boolean' ? value : fallback
}
