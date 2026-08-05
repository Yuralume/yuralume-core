/**
 * Story scene ("Start a Scene" / 起幕) contract types — plan SC, ticket SC1-A.
 *
 * A scene is a stretch of the *existing* chat thread, not a separate
 * conversation: the opening narration and the character's first line land in
 * the same message list as everything else, marked by `kind`. What makes it a
 * scene is the session row the backend keeps alongside it, which is what this
 * module describes.
 */

import type { ChatMessage } from '@/types/chat'

/** `open` until it is closed; a character has at most one open at a time. */
export type StorySceneStatus = 'open' | 'closed'

/**
 * Which layer of the material waterfall opened this scene: a pending beat of
 * the running arc, a season the button forced open, or an off-canon side
 * story when neither was available.
 */
export type StorySceneSourceLayer = 'beat' | 'new_season' | 'side_story'

/** Why the scene ended — player, story, or the idle timeout. */
export type StorySceneCloseReason = 'manual' | 'resolved' | 'timeout'

export interface StorySceneSession {
  id: string
  character_id: string
  conversation_id: string
  status: StorySceneStatus
  source_layer: StorySceneSourceLayer
  arc_id: string | null
  beat_id: string | null
  /** Scene-frame heading. Always present. */
  title: string
  location: string | null
  mood: string | null
  scene_type: string | null
  dramatic_question: string | null
  opened_at: string
  last_activity_at: string
  closed_at: string | null
  closed_reason: StorySceneCloseReason | null
}

/**
 * One suggested action chip.
 *
 * Deliberately a single-field object rather than a bare string: the backend
 * ships `{"text": …}` so the shape has room to grow (a reason, a tone) without
 * a protocol break, and the player-facing text has exactly one home.
 */
export interface StorySceneSuggestedAction {
  text: string
}

export interface StorySceneOpenResponse {
  session: StorySceneSession
  /** The scene-frame narration. Arrives with `kind: 'scene_narration'`. */
  narration: ChatMessage
  /** The character's first line of the scene — an ordinary chat bubble. */
  character_message: ChatMessage
  suggested_actions: StorySceneSuggestedAction[]
}

export interface StorySceneEndResponse {
  session: StorySceneSession
  /** Null when the scene closed without a written send-off. */
  closing_narration: ChatMessage | null
}
