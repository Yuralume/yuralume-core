/**
 * Case-insensitive, token-based option filtering for {@link UiCombobox}.
 *
 * Splits the query on whitespace and keeps options that contain *every*
 * token (AND semantics), so `"open gpt"` matches `"openrouter/gpt-5.5"`.
 * Results that start with the full trimmed query are ranked first; the
 * relative order of the remaining matches is preserved (Array.sort is
 * stable), so the caller's incoming order (e.g. provider order) survives.
 */
export function filterOptions(
  options: readonly string[],
  query: string,
): string[] {
  const trimmed = query.trim().toLowerCase()
  if (!trimmed) return [...options]
  const tokens = trimmed.split(/\s+/)
  const matches = options.filter((option) => {
    const lower = option.toLowerCase()
    return tokens.every((token) => lower.includes(token))
  })
  return matches
    .map((option, index) => ({ option, index }))
    .sort((a, b) => {
      const aStarts = a.option.toLowerCase().startsWith(trimmed) ? 0 : 1
      const bStarts = b.option.toLowerCase().startsWith(trimmed) ? 0 : 1
      if (aStarts !== bStarts) return aStarts - bStarts
      return a.index - b.index
    })
    .map((entry) => entry.option)
}
