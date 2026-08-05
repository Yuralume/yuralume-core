/**
 * Scroll arithmetic for loading older chat messages (plan IV, ticket IV10).
 *
 * Extracted from `ChatPanel` deliberately: the frontend test harness is SSR
 * (`createSSRApp` + `renderToString`, no jsdom), so it can neither measure
 * layout nor fire a scroll event. What *can* be protected is the arithmetic —
 * and the arithmetic is the part that goes wrong. Everything below is a pure
 * function of numbers the caller reads off the DOM.
 */

/** A scroll container's geometry at one instant. */
export interface ScrollAnchor {
  scrollTop: number
  scrollHeight: number
}

export interface ShouldLoadOlderInput {
  /** Distance from the top of the scroll container, in px. */
  scrollTop: number
  /** The server says older messages exist. */
  hasMore: boolean
  /** A page request is already in flight. */
  loading: boolean
  /** A turn is being sent / revealed — see `ChatPanel` for why that blocks. */
  busy?: boolean
  /** How close to the top counts as "asking for more". */
  thresholdPx?: number
}

/**
 * How near the top the reader must get before the next page is fetched.
 *
 * Not zero: waiting for `scrollTop === 0` means the reader hits the wall,
 * *then* waits for a round trip. A couple of hundred pixels of runway is
 * enough for the fetch to land before they arrive.
 */
export const LOAD_OLDER_THRESHOLD_PX = 240

export function shouldLoadOlder(input: ShouldLoadOlderInput): boolean {
  if (!input.hasMore) return false
  if (input.loading) return false
  if (input.busy) return false
  const threshold = input.thresholdPx ?? LOAD_OLDER_THRESHOLD_PX
  return input.scrollTop <= threshold
}

/**
 * Where `scrollTop` must land after older messages were prepended, so the
 * message the reader was looking at does not move under them.
 *
 * The whole trick is that the container grew *above* the viewport: everything
 * already on screen slid down by exactly the height that was inserted, so
 * scrollTop has to slide down by the same amount. Measuring the growth as a
 * total height delta — rather than summing the inserted rows — is what makes
 * this correct even when the same update also removes the "load older" button
 * (it does, on the last page) or reflows a wrapped line.
 *
 * A non-positive or non-finite delta means the layout did something we did not
 * predict; leaving `scrollTop` alone is the honest fallback, because the
 * alternative is scrolling to a computed position that means nothing.
 */
export function restoredScrollTop(
  before: ScrollAnchor,
  afterScrollHeight: number,
): number {
  const grew = afterScrollHeight - before.scrollHeight
  if (!Number.isFinite(grew) || grew <= 0) return before.scrollTop
  return before.scrollTop + grew
}

/**
 * Prepend one page of older messages to the thread.
 *
 * Trivial on its face, and separated out because the *index* bookkeeping
 * around it is not: `ChatPanel` addresses messages by array index (the scene
 * heading, the closing narration, the typewriter reveal), so anything holding
 * an index has to be shifted by exactly this many. Returning the count makes
 * that shift the caller's explicit obligation instead of an easy omission.
 */
export function prependOlderMessages<T>(
  older: readonly T[],
  current: readonly T[],
): { messages: T[], added: number } {
  return { messages: [...older, ...current], added: older.length }
}

/**
 * Shift an index that pointed into the thread before `added` older messages
 * were prepended. `null` (nothing pinned) stays `null`.
 */
export function shiftPinnedIndex(
  index: number | null,
  added: number,
): number | null {
  if (index === null) return null
  return index + added
}
