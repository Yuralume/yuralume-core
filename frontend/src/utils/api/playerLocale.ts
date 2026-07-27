/**
 * Hosted player locale lifecycle client (plan G2) — cloud mode only.
 *
 * Three endpoints, three very different failure contracts:
 *
 *   - `GET /geo/search` is **fail-soft by design**: a blank query, an
 *     upstream outage or an unwired client all mean "no suggestions", never
 *     an error banner while someone is still typing a city name.
 *   - `PATCH /auth/me/locale` can answer `429` with a structured body. That
 *     is not a generic failure — it carries *when* the player may try
 *     again, so it is surfaced as a typed error the UI can render as a
 *     date instead of a shrug.
 *   - `POST /auth/me/locale/confirm` is idempotent and free (it does not
 *     spend the 30-day guardrail), so the onboarding gate may retry it —
 *     but only until the account is confirmed: moving the timezone or
 *     language afterwards answers `409 locale_already_confirmed` pointing
 *     at the guarded endpoint.
 *   - Either write can answer `409 locale_change_in_progress` when the
 *     per-player claim is held by another in-flight write (a double submit,
 *     or the other replica). Nothing is wrong with the player's budget, so
 *     the only honest instruction is "try again in a moment".
 *
 * All three structured bodies become typed errors rather than one anonymous
 * "that didn't save": each one has a different next step for the player, and
 * a generic failure message would hide it.
 *
 * Uses axios rather than `authedFetch` to stay with `utils/api/auth.ts` —
 * the bearer header and the global 401 redirect are already installed on
 * the shared axios instance in `main.ts`.
 */

import axios from 'axios'

import type { AuthUser } from '@/utils/api/auth'
import type {
  GeoSearchResult,
  LocaleChangeRequest,
  LocaleConfirmRequest,
  LocaleState,
} from '@/types/playerLocale'

const AUTH_BASE = '/api/v1/auth'
const GEO_BASE = '/api/v1/geo'

/**
 * Display-only mirrors of the backend guardrail
 * (`PlayerLocaleService.LOCALE_CHANGE_LIMIT` / `_WINDOW_DAYS`). Used for
 * the "you may change this twice per 30 days" hint before any response has
 * told us the real remaining count; every response overrides them.
 */
export const LOCALE_CHANGE_LIMIT = 2
export const LOCALE_CHANGE_WINDOW_DAYS = 30

export const LOCALE_CHANGE_LIMIT_CODE = 'locale_change_limit'
export const LOCALE_ALREADY_CONFIRMED_CODE = 'locale_already_confirmed'
export const LOCALE_CHANGE_IN_PROGRESS_CODE = 'locale_change_in_progress'

export type PlayerLocaleState = LocaleState<AuthUser>

/** Thrown for the structured `429` so callers can say *when*, not just no. */
export class LocaleChangeLimitError extends Error {
  readonly retryAt: string | null
  readonly limit: number
  readonly windowDays: number

  constructor(input: {
    retryAt: string | null
    limit: number
    windowDays: number
  }) {
    super('locale change limit reached')
    this.name = 'LocaleChangeLimitError'
    this.retryAt = input.retryAt
    this.limit = input.limit
    this.windowDays = input.windowDays
  }
}

/**
 * Thrown when the free onboarding confirmation is asked to move a timezone
 * or language it no longer owns. The budget rides along so the message can
 * name the guardrail the player is being sent to.
 */
export class LocaleAlreadyConfirmedError extends Error {
  readonly limit: number
  readonly windowDays: number

  constructor(input: { limit: number; windowDays: number }) {
    super('locale is already confirmed')
    this.name = 'LocaleAlreadyConfirmedError'
    this.limit = input.limit
    this.windowDays = input.windowDays
  }
}

/**
 * Thrown when another write of this player's is still holding the locale
 * claim. Transient by construction — the same request will succeed shortly,
 * so nothing about the player's budget or input is wrong.
 */
export class LocaleChangeInProgressError extends Error {
  constructor() {
    super('another locale change is in progress')
    this.name = 'LocaleChangeInProgressError'
  }
}

/**
 * City suggestions for the "where I live" picker. Never rejects: an empty
 * list is the only failure mode a search box should have.
 */
export async function searchPlaces(query: string): Promise<GeoSearchResult[]> {
  const q = query.trim()
  if (!q) return []
  try {
    const { data } = await axios.get<{ results?: unknown }>(
      `${GEO_BASE}/search`,
      { params: { q } },
    )
    return normalizeResults(data?.results)
  } catch {
    return []
  }
}

/**
 * Deliberate, guardrail-consuming change of timezone and/or language.
 * Throws {@link LocaleChangeLimitError} when the rolling window is spent,
 * or {@link LocaleChangeInProgressError} while another write is in flight.
 */
export async function changeLocale(
  payload: LocaleChangeRequest,
): Promise<PlayerLocaleState> {
  try {
    const { data } = await axios.patch<PlayerLocaleState>(
      `${AUTH_BASE}/me/locale`,
      payload,
    )
    return data
  } catch (error) {
    throw localeErrorFrom(error) ?? error
  }
}

/**
 * First-login confirmation (and the "dismiss the relocation hint" action).
 * Free and idempotent — it never spends a change from the 30-day window.
 * Throws {@link LocaleAlreadyConfirmedError} once the account is confirmed
 * and the body would still move the timezone or language, and
 * {@link LocaleChangeInProgressError} while another write is in flight.
 */
export async function confirmLocale(
  payload: LocaleConfirmRequest,
): Promise<PlayerLocaleState> {
  try {
    const { data } = await axios.post<PlayerLocaleState>(
      `${AUTH_BASE}/me/locale/confirm`,
      payload,
    )
    return data
  } catch (error) {
    throw localeErrorFrom(error) ?? error
  }
}

function normalizeResults(raw: unknown): GeoSearchResult[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap((entry) => {
    const place = normalizeResult(entry)
    return place ? [place] : []
  })
}

function normalizeResult(entry: unknown): GeoSearchResult | null {
  if (!entry || typeof entry !== 'object') return null
  const row = entry as Record<string, unknown>
  const label = typeof row.label === 'string' ? row.label.trim() : ''
  const latitude = finiteNumber(row.latitude)
  const longitude = finiteNumber(row.longitude)
  if (!label || latitude === null || longitude === null) return null
  return {
    label,
    latitude,
    longitude,
    country_code: optionalString(row.country_code),
    timezone_id: optionalString(row.timezone_id),
  }
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function optionalString(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const text = value.trim()
  return text || null
}

/**
 * Recognize the structured bodies FastAPI wraps as `{ detail: {...} }`, and
 * only those. Anything else (a bare 429 from a proxy, a 409 from something
 * that is not this contract, a 500) is left to bubble as the original error
 * rather than be mis-rendered as a rule the player did not actually hit.
 *
 * The status is checked alongside the code so a body that merely echoes a
 * known string cannot masquerade as a different outcome.
 */
function localeErrorFrom(error: unknown): Error | null {
  const body = structuredDetail(error)
  if (!body) return null
  const { status, detail } = body
  if (status === 429 && detail.code === LOCALE_CHANGE_LIMIT_CODE) {
    return new LocaleChangeLimitError({
      retryAt: optionalString(detail.retry_at),
      limit: finiteNumber(detail.limit) ?? LOCALE_CHANGE_LIMIT,
      windowDays: finiteNumber(detail.window_days) ?? LOCALE_CHANGE_WINDOW_DAYS,
    })
  }
  if (status !== 409) return null
  if (detail.code === LOCALE_ALREADY_CONFIRMED_CODE) {
    return new LocaleAlreadyConfirmedError({
      limit: finiteNumber(detail.limit) ?? LOCALE_CHANGE_LIMIT,
      windowDays: finiteNumber(detail.window_days) ?? LOCALE_CHANGE_WINDOW_DAYS,
    })
  }
  if (detail.code === LOCALE_CHANGE_IN_PROGRESS_CODE) {
    return new LocaleChangeInProgressError()
  }
  return null
}

function structuredDetail(
  error: unknown,
): { status: number; detail: Record<string, unknown> } | null {
  if (!error || typeof error !== 'object') return null
  const response = (error as { response?: unknown }).response
  if (!response || typeof response !== 'object') return null
  const status = (response as { status?: unknown }).status
  if (typeof status !== 'number') return null
  const detail = (response as { data?: { detail?: unknown } }).data?.detail
  if (!detail || typeof detail !== 'object') return null
  return { status, detail: detail as Record<string, unknown> }
}
