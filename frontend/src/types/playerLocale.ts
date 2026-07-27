/**
 * Hosted player locale / place lifecycle DTOs (plan G2).
 *
 * Mirrors `api/contracts/player_locale.py`. Every one of these surfaces is
 * cloud-only on the backend (404 in self-host), so the SPA must treat them
 * as optional capabilities rather than as always-present state — see
 * `AuthUser.needs_locale_confirmation` / `location_hint`, both additive on
 * `GET /auth/me` and always falsy for self-host.
 */

/** A "you seem to be somewhere else now" suggestion — never an applied fact. */
export interface LocationHint {
  country_code: string
  label: string | null
  latitude: number | null
  longitude: number | null
  timezone_id: string | null
  detected_at: string | null
}

/** One city suggestion from `GET /api/v1/geo/search`, ready to store. */
export interface GeoSearchResult {
  label: string
  latitude: number
  longitude: number
  country_code: string | null
  timezone_id: string | null
}

/** Body of `PATCH /auth/me/locale` — at least one field is required. */
export interface LocaleChangeRequest {
  timezone_id?: string
  primary_language?: string
}

/**
 * Body of `POST /auth/me/locale/confirm`.
 *
 * The location keys are tri-state on the backend: an omitted key leaves the
 * stored value alone, while an explicit `null` clears it. So callers must
 * omit rather than send `null` unless they really mean "erase".
 */
export interface LocaleConfirmRequest {
  timezone_id?: string
  primary_language?: string
  country_code?: string | null
  latitude?: number | null
  longitude?: number | null
  location_label?: string | null
  dismiss_location_hint?: boolean
}

/** Post-write projection returned by both locale endpoints. */
export interface LocaleState<TUser = unknown> {
  user: TUser
  remaining_changes: number
  change_limit: number
  window_days: number
}
