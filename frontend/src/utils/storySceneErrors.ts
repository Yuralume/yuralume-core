import { StorySceneError } from '@/utils/api/storyScene'

/**
 * Classification only — the copy lives in the trilingual catalog under
 * `chat.storyScene.errors.*`, so every refusal is read in the player's own
 * language rather than in whatever the server logged (plan U1-D convention:
 * this module maps codes to keys and owns no display string of its own).
 */

/** Where an unrecognised failure lands, per entry point. */
export type StorySceneFallback = 'generic' | 'endGeneric'

/** Every scene refusal is read from this branch of the catalog. */
const KEY_PREFIX = 'chat.storyScene.errors.'

const CODE_TO_KEY: Readonly<Record<string, string>> = {
  // One scene at a time: this character is already in one.
  scene_in_progress: 'inProgress',
  // Nothing left to play — no pending beat, no season, no side story.
  no_material: 'noMaterial',
  // A turn is still in flight on this conversation.
  conversation_busy: 'conversationBusy',
  // The opening generation itself failed. Nothing was spent.
  scene_open_failed: 'openFailed',
  // Manual endings are not wired up yet (SC1-D lands them).
  scene_end_not_implemented: 'endNotAvailable',
  // We asked to end a scene the server no longer has running...
  no_open_scene: 'endAlreadyEnded',
  // ...or one it has since replaced with another.
  scene_session_mismatch: 'endSceneChanged',
  // The tier's rolling-24h ceiling on openings is spent (429, SC3-B).
  story_scene_daily_limit_reached: 'dailyLimitReached',
  // The ceiling exists but could not be checked, so the guard denied
  // fail-closed (503). Nothing about the player is wrong; try again.
  story_scene_quota_unavailable: 'quotaUnavailable',
}

/**
 * The key an opening lands on once today's allowance is spent.
 *
 * Exported because the panel has to react to *this* refusal beyond showing
 * it: the count on screen ("N left today") is now provably stale, and the
 * state machine deliberately swallows the error object, so the key is the
 * only handle a caller has on which refusal it was.
 */
export const STORY_SCENE_DAILY_LIMIT_KEY = `${KEY_PREFIX}dailyLimitReached`

/**
 * The refusals that mean "your view of the scene is out of date".
 *
 * All three are one failure wearing three coats: the client asked about a
 * scene the server does not agree is running. `scene_in_progress` comes back
 * from opening (a scene this tab never learned about — another tab, or a
 * state read that failed at thread open); `no_open_scene` and
 * `scene_session_mismatch` come back from ending (the scene answered its own
 * dramatic question, timed out, or was closed and replaced elsewhere).
 *
 * They are singled out because the honest response to all three is the same
 * and it is not a sentence: go and read what is actually running. Without
 * that, the refusal is a dead end — a scene banner whose End button can only
 * fail the same way for ever, or a "already running" apology for a scene the
 * player cannot see, reach, or close.
 */
const STALE_STATE_CODES: ReadonlySet<string> = new Set([
  'scene_in_progress',
  'no_open_scene',
  'scene_session_mismatch',
])

/** True when the server just told us our scene state is stale. */
export function isStaleSceneState(error: unknown): boolean {
  return (
    error instanceof StorySceneError && STALE_STATE_CODES.has(error.code)
  )
}

/** The catalog key for whatever went wrong, never a raw server string. */
export function storySceneErrorKey(
  error: unknown,
  fallback: StorySceneFallback = 'generic',
): string {
  const suffix = error instanceof StorySceneError
    ? CODE_TO_KEY[error.code]
    : undefined
  return `${KEY_PREFIX}${suffix ?? fallback}`
}
