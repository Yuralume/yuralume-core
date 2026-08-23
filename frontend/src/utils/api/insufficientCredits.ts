/**
 * The single place the SPA recognises the hosted "out of credits" refusal.
 *
 * The backend funnels every player-triggered foreground entry point through
 * one guard (`api/routes/_cloud_errors.py`), so it always arrives in one of
 * exactly two shapes:
 *
 *   - HTTP `402 {"detail": {"code": "insufficient_credits", "message": …}}`
 *     — chat (sync), image generation, branching drama, fusion adapt, TTS;
 *   - a terminal SSE frame `data: {"error": {"code": …, "message": …}}`
 *     followed by `data: [DONE]` — chat streaming, where the 200 + SSE
 *     headers are already on the wire and a status code is no longer
 *     available.
 *
 * Both collapse into the same typed error here so the display boundary has
 * one thing to check (`isInsufficientCreditsError`) and no call site has to
 * re-implement the discrimination.
 */

import { formatErrorBody, readErrorBody } from '@/utils/api/httpError'
import { priceChangedFromBody } from '@/utils/api/priceChanged'

export const INSUFFICIENT_CREDITS_CODE = 'insufficient_credits'
export const INSUFFICIENT_CREDITS_STATUS = 402

/**
 * Fallback carried in `Error#message` when the gateway omitted a reason.
 * Never rendered: the notice component shows localized copy and treats this
 * error purely as a signal.
 */
const FALLBACK_MESSAGE = 'insufficient credits for this request'

export class InsufficientCreditsError extends Error {
  code: string
  statusCode: number

  constructor(input: { message?: string; statusCode?: number } = {}) {
    super(input.message || FALLBACK_MESSAGE)
    this.name = 'InsufficientCreditsError'
    this.code = INSUFFICIENT_CREDITS_CODE
    this.statusCode = input.statusCode ?? INSUFFICIENT_CREDITS_STATUS
  }
}

export function isInsufficientCreditsError(
  error: unknown,
): error is InsufficientCreditsError {
  return error instanceof InsufficientCreditsError
}

/**
 * True when `detail` is the structured `{code, message}` body the backend
 * emits for an out-of-credits refusal.
 */
export function isInsufficientCreditsDetail(detail: unknown): boolean {
  return (
    typeof detail === 'object'
    && detail !== null
    && (detail as { code?: unknown }).code === INSUFFICIENT_CREDITS_CODE
  )
}

function messageFromDetail(detail: unknown): string {
  if (typeof detail === 'object' && detail !== null) {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  return FALLBACK_MESSAGE
}

/** Typed error for a `{code, message}` detail, else `null`. */
export function insufficientCreditsFromDetail(
  detail: unknown,
  statusCode: number = INSUFFICIENT_CREDITS_STATUS,
): InsufficientCreditsError | null {
  if (!isInsufficientCreditsDetail(detail)) return null
  return new InsufficientCreditsError({
    message: messageFromDetail(detail),
    statusCode,
  })
}

/**
 * Typed error for a FastAPI 402 body (`{detail: {code, message}}`), else
 * `null`. The status is checked so a stray `code` elsewhere in the API can
 * never be mistaken for a billing refusal.
 */
export function insufficientCreditsFromBody(
  body: unknown,
  status: number,
): InsufficientCreditsError | null {
  if (status !== INSUFFICIENT_CREDITS_STATUS) return null
  const detail = (body as { detail?: unknown } | null)?.detail
  return insufficientCreditsFromDetail(detail, status)
}

/**
 * Typed error for a terminal SSE `{"error": {…}}` frame, else `null`.
 * Frames without the credit code (plain tokens, the `done` frame) are left
 * to the caller's normal handling.
 */
export function insufficientCreditsFromStreamFrame(
  frame: unknown,
): InsufficientCreditsError | null {
  if (typeof frame !== 'object' || frame === null) return null
  return insufficientCreditsFromDetail((frame as { error?: unknown }).error)
}

/**
 * Typed error for an axios rejection, else `null`. Covers the API clients
 * under `utils/api/` that never migrated off axios (characters, tts).
 */
export function insufficientCreditsFromAxios(
  error: unknown,
): InsufficientCreditsError | null {
  if (!error || typeof error !== 'object') return null
  const response = (error as { response?: unknown }).response
  if (!response || typeof response !== 'object') return null
  const status = (response as { status?: unknown }).status
  if (typeof status !== 'number') return null
  return insufficientCreditsFromBody(
    (response as { data?: unknown }).data,
    status,
  )
}

/**
 * An `Error` from `apiErrorFromResponse` that isn't one of the typed
 * refusals above. `statusCode` lets a caller branch on the HTTP status
 * (e.g. retry a transient `409`) without re-parsing the response body it
 * already consumed.
 */
export interface ApiStatusError extends Error {
  statusCode: number
}

export function apiStatusErrorStatus(error: unknown): number | null {
  if (!(error instanceof Error)) return null
  const status = (error as Partial<ApiStatusError>).statusCode
  return typeof status === 'number' ? status : null
}

/**
 * Turn a non-ok `fetch` `Response` into either the typed credit error or a
 * plain `Error` carrying the server message. Reads the body exactly once,
 * so it is a drop-in replacement for `throw new Error(await
 * readErrorResponse(res))` in the hand-rolled fetch clients.
 *
 * The fallback `Error` also carries `statusCode` (see `ApiStatusError`) —
 * additive, so existing callers that only read `.message` are unaffected.
 */
export async function apiErrorFromResponse(res: Response): Promise<Error> {
  const body = await readErrorBody(res)
  const typed =
    insufficientCreditsFromBody(body, res.status)
    ?? priceChangedFromBody(body, res.status)
  if (typed) return typed
  const error = new Error(formatErrorBody(body, res)) as ApiStatusError
  error.statusCode = res.status
  return error
}
