/**
 * ARIA APG radiogroup keyboard pattern: arrow keys move focus *and* change
 * the selection to the newly focused option (wrapping past either end);
 * Home/End jump straight to the first/last option. Returns the index the
 * caller should select+focus next, or `null` when the key isn't part of
 * the pattern — the caller then lets the event fall through untouched
 * (e.g. Tab still moves focus out of the whole group, not between options).
 *
 * Pulled out as a pure function (mirrors `chatInputKeys.ts`) because this
 * repo has no DOM test harness (`@vue/test-utils` / jsdom aren't
 * installed) — the branching logic needs to be unit-testable without one.
 */
export function nextRovingRadioIndex(
  key: string,
  currentIndex: number,
  length: number,
): number | null {
  if (length <= 0) return null

  // Normalise so an out-of-range currentIndex (e.g. -1 for "not found")
  // still wraps predictably instead of producing a negative array index.
  const normalised = ((currentIndex % length) + length) % length

  switch (key) {
    case 'ArrowRight':
    case 'ArrowDown':
      return (normalised + 1) % length
    case 'ArrowLeft':
    case 'ArrowUp':
      return (normalised - 1 + length) % length
    case 'Home':
      return 0
    case 'End':
      return length - 1
    default:
      return null
  }
}
