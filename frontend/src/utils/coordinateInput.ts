/**
 * Normalizes a latitude/longitude form field into an optional number.
 *
 * Vue 3 auto-casts `v-model` on `<input type="number">` to a JS `number`
 * even without the `.number` modifier (see vModelText / looseToNumber in
 * @vue/runtime-dom). Refs seeded as `ref('')` therefore hold a `number`
 * once the user types into the field, a `string` when restored from a
 * stringified profile value or left untouched, or `null`/`undefined` in
 * edge cases. Calling `.trim()` directly on such a ref throws
 * `TypeError: value.trim is not a function` whenever it already holds a
 * number.
 *
 * This helper accepts any of those shapes, trims string input, and
 * returns `null` for "no value" (so callers can omit/clear the field)
 * or a `number` otherwise. It does not enforce latitude/longitude range
 * validation — that stays a server-side concern (see the `ge`/`le`
 * Field constraints in kokoro_link/api/routes/auth.py).
 */
export function normalizeCoordinateInput(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null
  }
  const trimmed = value.trim()
  if (trimmed === '') return null
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}
