import type { ChatMessage } from '@/types/chat'

/**
 * Reading a chat thread as a mix of dialogue and narration (plan SC, SC2).
 *
 * Kept out of the panel template because both rules below are the kind that
 * look obvious and are not: what counts as narration has to stay true for
 * every message stored before scenes existed, and the heading has to land on
 * the narration that *opened* the scene even after a send-off narration is
 * appended below it.
 */

type KindOnly = Pick<ChatMessage, 'kind'>

/**
 * True for a scene's narration.
 *
 * Explicitly a positive test on the marker, never `kind !== 'chat'`: every
 * message written before SC1-A carries no kind at all, and treating those as
 * narration would reformat entire back catalogues of ordinary conversation.
 * The same test is what keeps `tool_only` — a bare `/pic` artifact, tagged
 * for the backend's own context gating — rendering as the ordinary bubble it
 * has always been.
 */
export function isSceneNarration(
  message: KindOnly | null | undefined,
): boolean {
  return message?.kind === 'scene_narration'
}

export interface SceneHeadingOptions {
  /** We hold session metadata (title / place / mood) worth displaying. */
  hasSession: boolean
  /** Index of a closing narration appended on this screen, if any. */
  closingIndex?: number | null
}

/**
 * Which message carries the scene's heading, or null for none.
 *
 * A thread can hold narrations from several past scenes and we only ever know
 * the metadata of the current one, so the heading goes on the newest narration
 * that is not the send-off — and only while a session is in hand. Without one
 * (a reload into a finished scene) every frame still renders as a scene frame,
 * just untitled, which is the honest rendering of "we no longer know".
 */
export function sceneHeadingIndex(
  messages: ReadonlyArray<KindOnly>,
  options: SceneHeadingOptions,
): number | null {
  if (!options.hasSession) return null
  const closingIndex = options.closingIndex ?? null
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (!isSceneNarration(messages[i])) continue
    if (i === closingIndex) continue
    return i
  }
  return null
}
