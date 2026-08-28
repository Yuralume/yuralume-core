/**
 * Who a chat turn belongs to, and whether it still belongs to what is on
 * screen.
 *
 * The panel is one long-lived component that changes character underneath a
 * turn that is already in flight (`<ChatPanel>` is reused across characters,
 * and a turn survives a tap on the sidebar by seconds). Aborting the stream
 * covers most of it, but not all: a reply can resolve in the very tick the
 * reader switches away, and the closure that resumes afterwards still holds
 * references to "the thread" and "the character" — which are now somebody
 * else's. That closure writing its result is the erosion bug: the previous
 * character's reply, state and error bubbles landing in the new one's thread.
 *
 * So every turn is stamped when it starts and the stamp is re-checked after
 * every await. Nothing is lost by discarding a stale result: the server
 * finished the turn and stored it, and reopening that character reloads it
 * from the database.
 *
 * The stamp answers two questions, not one, because two turns on the *same*
 * character can legitimately overlap (the multi-bubble reveal returns the
 * composer before the turn it belongs to is finished). "Does this thread
 * still belong to the reader?" is `isCurrent`, and a turn that passes it may
 * still append its reply. "Is this turn the one the screen is showing?" is
 * `ownsScreen`, and only that one may touch the state there is one of.
 * Collapsing them is not a simplification — it silently throws away replies.
 *
 * The comparison is a pure function on purpose (the branch that decides
 * whether a reply is allowed to be shown deserves its own test), and the
 * guard around it owns the one piece of state that cannot be pure — the
 * `AbortController` of whatever is in flight.
 */

/** The identity of a turn: which character, and which "generation". */
export interface ChatTurnStamp {
  /**
   * Bumped every time the panel walks away from a turn (character switch,
   * unmount). Carries the cases a character id alone cannot: switching away
   * and back again lands on the same id but must still discard the turn
   * started before the round trip.
   */
  epoch: number
  /** Null means "no character on screen", which is never current. */
  characterId: string | null
}

/** A begun turn: its stamp, plus the signal that cancels it. */
export interface ChatTurnTicket extends ChatTurnStamp {
  characterId: string
  signal: AbortSignal
  /**
   * Which begun turn this is, counting from the panel's first send.
   *
   * Deliberately *not* folded into `epoch`. The two answer different
   * questions and a turn can fail one while passing the other:
   *
   * - `epoch` — "does this thread still belong to the reader?" A turn that
   *   passes it may still write its result: the reply, the closed scene and
   *   its send-off narration all belong in this thread whenever they land.
   * - `turnSeq` — "is this turn the one the screen is currently showing?"
   *   A second turn can legitimately begin while the first is still
   *   finishing (the multi-bubble DM reveal releases the composer as soon as
   *   the first bubble starts typing), and *that* is when the first turn's
   *   tail must stop touching the shared, single-slot screen state — the
   *   tool indicator, the scene action chips.
   *
   * Bumping the epoch in `begin` would have collapsed the two and silently
   * cost the first turn its reply and its scene close.
   */
  turnSeq: number
}

/**
 * May a turn stamped `ticket` still write to the screen showing `current`?
 *
 * Both halves are required. The epoch alone would be enough today, and the
 * character id alone would not be enough ever (switch away and back) — the
 * pair fails closed if either the panel or a caller forgets to bump.
 */
export function isTurnStillCurrent(
  ticket: ChatTurnStamp,
  current: ChatTurnStamp,
): boolean {
  if (ticket.characterId === null || current.characterId === null) return false
  return (
    ticket.epoch === current.epoch
    && ticket.characterId === current.characterId
  )
}

/** The inverse, named for the call site that actually reads better this way. */
export function shouldDiscardTurnResult(
  ticket: ChatTurnStamp,
  current: ChatTurnStamp,
): boolean {
  return !isTurnStillCurrent(ticket, current)
}

/**
 * Owns "which turn is in flight, and for whom" for one chat panel.
 *
 * Plain TypeScript, no Vue: the panel keeps the rendering decisions and this
 * keeps the identity bookkeeping, which is the part worth testing on its own.
 */
export class ChatTurnGuard {
  private epoch = 0
  private turnSeq = 0
  private characterId: string | null = null
  private controller: AbortController | null = null

  /** The stamp a turn started right now would carry. */
  get current(): ChatTurnStamp {
    return { epoch: this.epoch, characterId: this.characterId }
  }

  /** True while a begun turn has neither settled nor been interrupted. */
  get inFlight(): boolean {
    return this.controller !== null
  }

  /**
   * Start a turn for `characterId`, cancelling any turn still in flight —
   * the panel allows one at a time, and a leftover controller would leave
   * the previous stream running with nowhere to render.
   */
  begin(characterId: string): ChatTurnTicket {
    this.controller?.abort()
    this.characterId = characterId
    this.turnSeq += 1
    const controller = new AbortController()
    this.controller = controller
    return {
      epoch: this.epoch,
      characterId,
      signal: controller.signal,
      turnSeq: this.turnSeq,
    }
  }

  /** May this turn still write to the screen? */
  isCurrent(ticket: ChatTurnStamp): boolean {
    return isTurnStillCurrent(ticket, this.current)
  }

  /**
   * Is this turn the one the screen currently belongs to?
   *
   * Stricter than `isCurrent` by exactly one thing: a newer turn has not
   * begun since. Use it for the state the screen only has one of — the tool
   * indicator, the scene action chips, the composer's focus and scroll —
   * where a turn finishing late would otherwise wipe what its successor
   * just put there. Everything that *accumulates* (a reply appended to the
   * thread, a scene's closing narration) stays on `isCurrent`: those belong
   * to the thread whenever they land, and dropping them loses real content.
   */
  ownsScreen(ticket: ChatTurnTicket): boolean {
    return this.isCurrent(ticket) && ticket.turnSeq === this.turnSeq
  }

  /**
   * Walk away from whatever is in flight and move to `characterId` (null on
   * unmount). Aborts the stream and bumps the epoch, so a result that
   * resolves anyway — the race the abort did not win — is disowned too.
   */
  interrupt(characterId: string | null): void {
    this.controller?.abort()
    this.controller = null
    this.epoch += 1
    this.characterId = characterId
  }

  /**
   * This turn is over (any outcome). Drops its controller unless a newer
   * turn has already taken the slot — settling a finished turn must never
   * disarm the abort of the one that replaced it.
   */
  settle(ticket: ChatTurnTicket): void {
    if (this.controller?.signal !== ticket.signal) return
    this.controller = null
  }
}
