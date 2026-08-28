/**
 * Which feed posts count as "actually seen" by the player, and when to send
 * a batched read-receipt (KB11 of PLAYER_KNOWLEDGE_BOUNDARY_PLAN).
 *
 * Everything here is a pure function of numbers, for the same reason
 * `offscreenImageRelease.ts` is: the frontend test harness is SSR
 * (`createSSRApp` + `renderToString`, no jsdom), so it can neither run an
 * `IntersectionObserver` nor let a `setTimeout`/`setInterval` fire. The
 * browser glue that actually creates the observer and drives a poll loop
 * lives in `feedViewObserver.ts` and calls straight into the functions
 * below — it makes no threshold or batching decision of its own.
 *
 * Two separate questions, two separate pieces of state:
 *
 * 1. "Has this post been on screen long enough to count as viewed?" — a
 *    fling-past scroll must not count, so exposure needs a *dwell*, not a
 *    single intersecting frame. `trackExposure` / `postsDueForView` /
 *    `clearTrackedPosts` answer this from a `postId -> firstCrossedAtMs`
 *    map.
 * 2. "Is it time to flush the pending ids to the server?" — the viewed
 *    endpoint is per-character and batched on purpose (LumeGram's global
 *    wall can show a screenful of posts at once; one request per post
 *    would be one request per card, every scroll). `enqueueViewed` /
 *    `shouldFlushBatch` / `flushBatch` answer this from a pending-id set.
 */

// ----------------------------------------------------------------------
// 1. Dwell — which posts have been visible enough, for long enough
// ----------------------------------------------------------------------

/**
 * Fraction of a post's card that must be on screen before its dwell clock
 * starts. 0.6 rather than "any pixel visible": a card peeking one row in at
 * the bottom of the viewport during a fast scroll is not "looking at it".
 */
export const FEED_VIEW_VISIBILITY_RATIO = 0.6

/**
 * How long a post has to stay above the ratio threshold, continuously,
 * before it counts as viewed. Short on purpose — this is a feed wall, not
 * a chat bubble; the player is expected to scroll past most cards quickly,
 * and only the ones they stop on should count. Dropping below the ratio
 * resets the clock (see `trackExposure`), so a brief scroll-through does
 * not accumulate toward this across multiple passes.
 */
export const FEED_VIEW_DWELL_MS = 800

export interface FeedExposureSample {
  postId: string
  /** `IntersectionObserverEntry.intersectionRatio`, 0..1. */
  ratio: number
  nowMs: number
}

/**
 * Update the dwell-start map from one exposure sample.
 *
 * Crossing the ratio threshold starts the clock (only if it is not
 * already running — a second sample above the threshold must not push the
 * start time forward, or the dwell would never complete under a jittery
 * observer that reports the same crossing more than once). Dropping below
 * the threshold clears it: the post has to accumulate the dwell in one
 * continuous stretch above the ratio, not in fragments.
 */
export function trackExposure(
  visibleSince: ReadonlyMap<string, number>,
  sample: FeedExposureSample,
  ratioThreshold: number = FEED_VIEW_VISIBILITY_RATIO,
): Map<string, number> {
  const next = new Map(visibleSince)
  if (sample.ratio >= ratioThreshold) {
    if (!next.has(sample.postId)) next.set(sample.postId, sample.nowMs)
  } else {
    next.delete(sample.postId)
  }
  return next
}

/**
 * Which tracked posts have dwelled at least `dwellMs` as of `nowMs`.
 *
 * Order is not meaningful — callers batch these, they don't display them.
 */
export function postsDueForView(
  visibleSince: ReadonlyMap<string, number>,
  nowMs: number,
  dwellMs: number = FEED_VIEW_DWELL_MS,
): string[] {
  const due: string[] = []
  for (const [postId, since] of visibleSince) {
    if (nowMs - since >= dwellMs) due.push(postId)
  }
  return due
}

/**
 * Stop tracking the given posts.
 *
 * Called once a post has been handed to `postsDueForView` and enqueued —
 * without this, a post that stays on screen (the player reads a long
 * caption, say) would still be "due" on every subsequent poll tick and get
 * re-enqueued forever. A post removed here that is still visible next
 * frame is fine: it is already reported, and nothing re-tracks a
 * server-confirmed-viewed post (the caller does not observe it again once
 * `FeedPost.viewed_at` is set).
 */
export function clearTrackedPosts(
  visibleSince: ReadonlyMap<string, number>,
  postIds: readonly string[],
): Map<string, number> {
  if (postIds.length === 0) return new Map(visibleSince)
  const next = new Map(visibleSince)
  for (const id of postIds) next.delete(id)
  return next
}

// ----------------------------------------------------------------------
// 2. Batching — when the pending ids actually go over the wire
// ----------------------------------------------------------------------

/** Send the batch once this many ids are pending, even if the interval
 * below hasn't elapsed — caps request payload size and keeps a fast-scroll
 * session from holding an unbounded pending set in memory. */
export const FEED_VIEW_BATCH_MAX_IDS = 50

/** Otherwise, send at most this often — batching exists so a burst of
 * dwelled posts (the player scrolls through several quickly) becomes one
 * request, not so a single card sits unreported for a long time. */
export const FEED_VIEW_BATCH_INTERVAL_MS = 4000

export interface FeedViewBatchState {
  readonly pendingIds: ReadonlySet<string>
  /** `nowMs` the oldest currently-pending id was enqueued at, or `null`
   * while the batch is empty — drives the interval half of the flush
   * decision. */
  readonly oldestPendingAtMs: number | null
}

export function createFeedViewBatchState(): FeedViewBatchState {
  return { pendingIds: new Set(), oldestPendingAtMs: null }
}

/**
 * Add newly-due post ids to the pending batch.
 *
 * `oldestPendingAtMs` only ever gets set from empty — enqueueing more ids
 * onto an already-pending batch does not push the flush deadline out,
 * which is what keeps the interval a ceiling on staleness rather than a
 * clock that a busy scroll session can indefinitely postpone.
 */
export function enqueueViewed(
  state: FeedViewBatchState,
  postIds: readonly string[],
  nowMs: number,
): FeedViewBatchState {
  if (postIds.length === 0) return state
  const pendingIds = new Set(state.pendingIds)
  for (const id of postIds) pendingIds.add(id)
  return {
    pendingIds,
    oldestPendingAtMs: state.oldestPendingAtMs ?? nowMs,
  }
}

export interface FlushBatchOptions {
  intervalMs?: number
  maxIds?: number
}

/** Whether `state` should be flushed right now. Empty batches never flush
 * — there is nothing to send, and a caller that polls unconditionally
 * must not fire empty requests every tick. */
export function shouldFlushBatch(
  state: FeedViewBatchState,
  nowMs: number,
  options: FlushBatchOptions = {},
): boolean {
  if (state.pendingIds.size === 0) return false
  const maxIds = options.maxIds ?? FEED_VIEW_BATCH_MAX_IDS
  if (state.pendingIds.size >= maxIds) return true
  const intervalMs = options.intervalMs ?? FEED_VIEW_BATCH_INTERVAL_MS
  if (state.oldestPendingAtMs === null) return false
  return nowMs - state.oldestPendingAtMs >= intervalMs
}

export interface FlushBatchResult {
  ids: string[]
  state: FeedViewBatchState
}

/** Drain the pending set. Always safe to call — an empty batch drains to
 * an empty id list and its own already-empty state, so a caller does not
 * need to guard with `shouldFlushBatch` first (e.g. an unmount flush that
 * wants "send whatever is left, or nothing"). */
export function flushBatch(state: FeedViewBatchState): FlushBatchResult {
  return {
    ids: [...state.pendingIds],
    state: createFeedViewBatchState(),
  }
}
