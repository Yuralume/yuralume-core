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
 * Read a non-ok `fetch` `Response` body and return a human-readable
 * error message. Never throws — falls back to `"<status> <statusText>"`
 * when the body is missing, not JSON, or an unrecognized shape.
 */
export async function readErrorResponse(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (typeof body?.detail === 'string') return body.detail
    if (Array.isArray(body?.detail)) {
      return body.detail.map(formatDetailEntry).join('\n')
    }
  } catch {
    /* ignore — fall through to status text below */
  }
  return `${res.status} ${res.statusText}`
}
