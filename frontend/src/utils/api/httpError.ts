/**
 * Shared HTTP error-body reader for the small hand-rolled `fetch` API
 * clients under `src/utils/api/`.
 *
 * FastAPI error bodies come in two shapes:
 * - `HTTPException`: `{ detail: string }`
 * - pydantic 422 validation failures: `{ detail: Array<{ loc, msg, ... }> }`
 *
 * Previously each API client `JSON.stringify`'d the array case, which
 * surfaced the raw pydantic error objects verbatim in the UI (e.g.
 * `[{"type":"greater_than_equal","loc":["body","duration_days"],...}]`).
 * This formats each entry as `<field>: <message>` instead.
 */

interface PydanticErrorDetail {
  loc?: unknown
  msg?: unknown
}

function formatDetailEntry(entry: unknown): string {
  if (typeof entry !== 'object' || entry === null) return String(entry)
  const { loc, msg } = entry as PydanticErrorDetail
  const message = typeof msg === 'string' ? msg : JSON.stringify(entry)

  if (!Array.isArray(loc) || loc.length === 0) return message

  // Last loc segment is the field name (e.g. ["body", "duration_days"]);
  // fall back to joining the whole path for body-level cross-field errors.
  const field = String(loc[loc.length - 1])
  return `${field}: ${message}`
}

/**
 * Consume a non-ok `Response` body as JSON. Never throws — returns
 * `null` when the body is missing or not JSON.
 *
 * Exposed separately from `readErrorResponse` because a `Response` body
 * can only be read once: callers that need to *inspect* the structured
 * detail (e.g. the shared `insufficient_credits` discriminator) must
 * share this single read with the message formatter below.
 */
export async function readErrorBody(res: Response): Promise<unknown> {
  try {
    return await res.json()
  } catch {
    return null
  }
}

/**
 * Format an already-read error body into a human-readable message.
 * Falls back to `"<status> <statusText>"` for unrecognized shapes.
 */
export function formatErrorBody(
  body: unknown,
  res: { status: number; statusText: string },
): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map(formatDetailEntry).join('\n')
  }
  return `${res.status} ${res.statusText}`
}

/**
 * Read a non-ok `fetch` `Response` body and return a human-readable
 * error message. Never throws — falls back to `"<status> <statusText>"`
 * when the body is missing, not JSON, or an unrecognized shape.
 */
export async function readErrorResponse(res: Response): Promise<string> {
  return formatErrorBody(await readErrorBody(res), res)
}
