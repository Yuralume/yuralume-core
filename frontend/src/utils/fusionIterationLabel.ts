/**
 * Player-facing label for a fusion story's version-history row (FX6).
 *
 * `iteration_label` arrives over the wire as the backend's own bookkeeping
 * string — `outline_regenerate`, `beat_3_regenerate`, `polish`, `restore_v2`,
 * or the `iterate` fallback `FusionStory.snapshot_version` substitutes for a
 * blank label. Those are internal identifiers, not copy: rendered raw they
 * read as leaked English enum values inside an otherwise localized panel,
 * which is the same defect `dramaToneLabel` fixed for drama tones (BD11), and
 * this is the same shape of fix.
 *
 * Two of the five carry a number, so this cannot be a flat key lookup: the
 * beat index and the restored version number are parsed out and handed to the
 * catalogue as named parameters. The beat index is **0-based on the wire**
 * (`FusionStoryService.iterate_beat` validates `0 <= beat_index < len(beats)`)
 * and is shown 1-based, because no player-facing surface numbers beats at all
 * and "第 1 段" for the first beat is the only counting a reader would expect.
 * The restore number is left alone — it is already the `v{n}` printed on the
 * row the player restored *to*, and shifting it would point at a wrong row.
 *
 * Every label names the *operation that produced the row*, not the row's own
 * contents: `snapshot_version` tags the pre-operation head, so `restore_v3`
 * reads "restored to v3 here", not "this text came from v3".
 *
 * Anything outside the known set falls back to the raw value rather than
 * throwing or rendering nothing: a future label should still show *something*
 * in the history list instead of leaving a blank cell.
 */

/** The i18n call this helper needs, narrowed so vue-i18n stays out of here. */
export interface FusionIterationTranslate {
  (key: string, named?: Record<string, unknown>): string
}

const I18N_PREFIX = 'fusionStory.viewer.versionsPanel.iterationLabels'

/** Labels emitted verbatim by `FusionStoryService` / the blank-label fallback. */
const PLAIN_LABELS: ReadonlySet<string> = new Set([
  'outline_regenerate',
  'polish',
  'iterate',
])

const BEAT_REGENERATE = /^beat_(\d+)_regenerate$/
const RESTORE_VERSION = /^restore_v(\d+)$/

export function fusionIterationLabel(
  label: string | null | undefined,
  t: FusionIterationTranslate,
): string {
  if (!label) return ''

  const beat = BEAT_REGENERATE.exec(label)
  if (beat) {
    return t(`${I18N_PREFIX}.beatRegenerate`, { index: Number(beat[1]) + 1 })
  }

  const restore = RESTORE_VERSION.exec(label)
  if (restore) {
    return t(`${I18N_PREFIX}.restore`, { version: Number(restore[1]) })
  }

  return PLAIN_LABELS.has(label) ? t(`${I18N_PREFIX}.${label}`) : label
}
