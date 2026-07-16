/**
 * Client-side mirror of the backend's ``StartStoryArcRequest`` bounds
 * (``kokoro_link/application/dto/story_arc.py``). Kept in sync manually —
 * if the backend bounds change, update these constants too.
 *
 * Pacing rationale: beats land on distinct real calendar days, at most
 * one beat surfaced per day, so ``duration_days`` must cover
 * ``beat_count``.
 */
export const ARC_DURATION_MIN_DAYS = 3
export const ARC_DURATION_MAX_DAYS = 90
export const ARC_BEAT_COUNT_MIN = 3
export const ARC_BEAT_COUNT_MAX = 7

export type NewArcValidationReason =
  | 'durationOutOfRange'
  | 'beatCountOutOfRange'
  | 'durationShorterThanBeatCount'

export interface NewArcDraft {
  duration_days: number
  beat_count: number
}

/**
 * Validate a "start new arc" draft against the same bounds the backend
 * enforces. Returns ``null`` when valid, otherwise the first violated
 * reason (checked in a fixed order so callers get one clear message).
 */
export function validateNewArcDraft(
  draft: NewArcDraft,
): NewArcValidationReason | null {
  if (
    draft.duration_days < ARC_DURATION_MIN_DAYS
    || draft.duration_days > ARC_DURATION_MAX_DAYS
    || !Number.isFinite(draft.duration_days)
  ) {
    return 'durationOutOfRange'
  }
  if (
    draft.beat_count < ARC_BEAT_COUNT_MIN
    || draft.beat_count > ARC_BEAT_COUNT_MAX
    || !Number.isFinite(draft.beat_count)
  ) {
    return 'beatCountOutOfRange'
  }
  if (draft.duration_days < draft.beat_count) {
    return 'durationShorterThanBeatCount'
  }
  return null
}
