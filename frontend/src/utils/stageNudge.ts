import type { ChatMessage } from '@/types/chat'

/**
 * The star-wrap rule for a 示意 (stage-nudge, "let the character speak
 * first") supplement, mirrored from the backend contract (plan SN §2/§5).
 *
 * The backend wraps a non-empty supplement in `*…*` before persisting it as
 * an ordinary user message, unless the player already used an asterisk
 * somewhere in what they typed — a second pass there would double the
 * stars. Kept here, independently, so the optimistic bubble this client
 * renders matches byte-for-byte what a reload of the same conversation will
 * show; the backend does the actual persisting, this only has to agree with
 * it about the rule.
 */
export function wrapStageNudgeAsterisks(text: string): string {
  return text.includes('*') ? text : `*${text}*`
}

/** What one press of the 示意 popover's confirm button turns into. */
export interface StageNudgeTurn {
  /**
   * What goes on the wire as `message`. Sent un-wrapped — the backend owns
   * the `*…*` persistence rule, so this is the raw (trimmed) supplement,
   * and an empty string is a valid, intentional value (a wordless nudge).
   */
  message: string
  /**
   * What renders as the player's own bubble the instant they press confirm,
   * or `null` for a wordless nudge — which must show no player-side bubble
   * at all, only the character's reply (plan SN §2).
   */
  optimisticMessage: ChatMessage | null
}

/**
 * Turns the popover's raw input (whatever the player typed, or left empty)
 * into the two things one 示意 turn needs: the message to send, and — since
 * the two are no longer identical once the star-wrap enters — what to show
 * optimistically while the request is in flight.
 *
 * Pure and independent of the send pipeline on purpose: this is the one
 * piece of the feature's branching (empty vs. not, wrap vs. don't) worth
 * pinning with a unit test on its own, without standing up a component or
 * mocking the transport to reach it.
 */
export function buildStageNudgeTurn(rawText: string): StageNudgeTurn {
  const trimmed = rawText.trim()
  if (trimmed.length === 0) {
    return { message: '', optimisticMessage: null }
  }
  return {
    message: trimmed,
    optimisticMessage: { role: 'user', content: wrapStageNudgeAsterisks(trimmed) },
  }
}
