import { DRAMA_TONES } from '@/types/branchingDrama'

/**
 * Player-facing label for a drama's tone branch (BD11).
 *
 * `chosen_tone` (`DramaSessionTurn`) and `tone` (`DramaNode` /
 * `CollectedScene`) arrive over the wire as the backend's closed English
 * vocabulary (`dark` / `sunny` / `neutral` — see `VALID_TONES` in
 * `branching_drama.py`), which reads as unexplained raw English to a
 * player, not a translated word. Every surface that shows a tone to the
 * player goes through this one function — not a `t(\`branchingDrama.tone.${tone}\`)`
 * call re-derived locally — so the three labels and their fallback
 * behaviour live in exactly one place.
 *
 * Falls back to the raw value for anything outside the known set rather
 * than throwing or rendering nothing: an unrecognized future tone should
 * still show *something* mid-playthrough instead of going blank.
 */
export function dramaToneLabel(
  tone: string | null | undefined,
  t: (key: string) => string,
): string {
  if (!tone) return ''
  return (DRAMA_TONES as readonly string[]).includes(tone)
    ? t(`branchingDrama.tone.${tone}`)
    : tone
}
