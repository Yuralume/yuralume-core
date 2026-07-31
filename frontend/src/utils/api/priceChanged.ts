/**
 * The typed refusal for a fixed-action price that moved mid-session.
 *
 * The backend guard (`api/routes/_cloud_errors.py`) answers HTTP `409
 * {"detail": {"code": "price_changed", "message": …, "current_price_cr": …}}`
 * on sync routes and a terminal SSE `{"error": {"code": "price_changed", …}}`
 * frame on chat streaming. Nothing was reserved in either case: the User
 * service refused the stale quote before touching the wallet, so the only
 * right rendering is "prices moved, send it again" — never a generic failure.
 */

export const PRICE_CHANGED_CODE = 'price_changed'
export const PRICE_CHANGED_STATUS = 409

/** Never rendered — the panels show localized copy keyed off the type. */
const FALLBACK_MESSAGE = 'the action price changed, please resend'

export class PriceChangedError extends Error {
  code: string
  statusCode: number
  currentPriceCr: number | null

  constructor(
    input: { message?: string; currentPriceCr?: number | null } = {},
  ) {
    super(input.message || FALLBACK_MESSAGE)
    this.name = 'PriceChangedError'
    this.code = PRICE_CHANGED_CODE
    this.statusCode = PRICE_CHANGED_STATUS
    this.currentPriceCr
      = typeof input.currentPriceCr === 'number' ? input.currentPriceCr : null
  }
}

export function isPriceChangedError(
  error: unknown,
): error is PriceChangedError {
  return error instanceof PriceChangedError
}

function fromDetailObject(detail: unknown): PriceChangedError | null {
  if (typeof detail !== 'object' || detail === null) return null
  if ((detail as { code?: unknown }).code !== PRICE_CHANGED_CODE) return null
  const message = (detail as { message?: unknown }).message
  const current = (detail as { current_price_cr?: unknown }).current_price_cr
  return new PriceChangedError({
    message: typeof message === 'string' && message ? message : undefined,
    currentPriceCr: typeof current === 'number' ? current : null,
  })
}

/**
 * Typed error for a FastAPI 409 body (`{detail: {code, …}}`), else `null`.
 * The status is checked so any other 4xx carrying a stray `code` field can
 * never be mistaken for a billing refusal.
 */
export function priceChangedFromBody(
  body: unknown,
  status: number,
): PriceChangedError | null {
  if (status !== PRICE_CHANGED_STATUS) return null
  return fromDetailObject((body as { detail?: unknown } | null)?.detail)
}

/**
 * Typed error for an axios rejection, else `null`. Mirrors
 * `insufficientCreditsFromAxios` for the API clients that never migrated off
 * axios (characters, tts) — the portrait and character-draft entry points bind
 * a quoted price (R9), so `409 price_changed` is a normal answer there and
 * must not surface as "generation failed".
 */
export function priceChangedFromAxios(
  error: unknown,
): PriceChangedError | null {
  if (!error || typeof error !== 'object') return null
  const response = (error as { response?: unknown }).response
  if (!response || typeof response !== 'object') return null
  const status = (response as { status?: unknown }).status
  if (typeof status !== 'number') return null
  return priceChangedFromBody((response as { data?: unknown }).data, status)
}

/** Typed error for a terminal SSE `{"error": {…}}` frame, else `null`. */
export function priceChangedFromStreamFrame(
  frame: unknown,
): PriceChangedError | null {
  if (typeof frame !== 'object' || frame === null) return null
  return fromDetailObject((frame as { error?: unknown }).error)
}
