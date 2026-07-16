// Pure presentation logic for the Stage (scene) access steering notice.
//
// The notice is the only surface carrying the phone-message / ask-to-meet
// / retry / add-context affordances when a Stage interaction is blocked or
// warned, so the scene-access-hint preference must NEVER remove it from
// explicit interactions — it only silences the AMBIENT trigger (a verdict
// loading in the background when the player opens a chat). A player who
// actively clicks the Stage tab or hits retry always gets the notice.
//
// The notice always opens collapsed (title + reason + expand affordance) —
// that is the shipped immersion behavior; expansion is a per-notice player
// toggle, not a preference.

export type StageAccessNoticeDecision = 'allow' | 'warn' | 'block'

/**
 * How the notice came to open:
 * - 'explicit' — the player acted (clicked the Stage tab, pressed retry).
 * - 'ambient'  — a verdict resolved in the background (chat opened,
 *   character switched) without the player asking for Stage.
 */
export type StageAccessNoticeTrigger = 'explicit' | 'ambient'

/**
 * Whether the notice should open for a non-allow verdict.
 *
 * Explicit triggers always open — refusing a player's own Stage attempt
 * without explanation would strand them. Ambient triggers respect the
 * scene-access-hint preference: with the hint off the player has said
 * "don't nag me when I'm just chatting".
 */
export function shouldOpenStageAccessNotice(
  trigger: StageAccessNoticeTrigger,
  hintEnabled: boolean,
): boolean {
  return trigger === 'explicit' || hintEnabled
}

export interface StageAccessNoticeInput {
  /** ``stageAccessNoticeOpen`` — the notice has been armed for display. */
  noticeOpen: boolean
  /** The current verdict decision, or ``null`` when none resolved yet. */
  decision: StageAccessNoticeDecision | null
  /** Live expanded flag (the player's toggle state). */
  expanded: boolean
}

export interface StageAccessNoticeState {
  /** Whether the notice block renders at all. */
  visible: boolean
  /** Whether it renders collapsed (title + reason + expand affordance). */
  collapsed: boolean
  /** Whether the phone/meet/retry/add-context actions render. */
  showDetails: boolean
}

/**
 * Resolve the three template booleans for the notice.
 *
 * ``allow`` (and a missing verdict) is always hidden — the notice only
 * exists to steer ``warn`` / ``block`` cases. When visible, the notice is
 * collapsed unless the player has expanded it; details ride on the same
 * expansion so the affordances stay reachable in both preference states.
 */
export function resolveStageAccessNotice(
  input: StageAccessNoticeInput,
): StageAccessNoticeState {
  const visible = input.noticeOpen
    && input.decision !== null
    && input.decision !== 'allow'
  if (!visible) {
    return { visible: false, collapsed: false, showDetails: false }
  }
  const collapsed = !input.expanded
  return { visible: true, collapsed, showDetails: !collapsed }
}
