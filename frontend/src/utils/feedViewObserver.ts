/**
 * The browser half of feed-post view tracking (KB11 of
 * PLAYER_KNOWLEDGE_BOUNDARY_PLAN).
 *
 * Deliberately thin — every threshold and timing decision is imported from
 * `feedViewTracking.ts`, which is where the tests are. What is left here is
 * bookkeeping the SSR harness cannot execute at all: creating an
 * `IntersectionObserver`, running a poll loop against real wall-clock time,
 * and calling back into the caller exactly once per post.
 *
 * One shared observer + one shared poll timer for the whole feed wall,
 * mirroring `offscreenImageObserver.ts`'s per-root-margin sharing: a global
 * or per-character feed can show a screenful of cards at once, and N
 * observers each running their own callback is no better than one observer
 * evaluating every card in the same pass.
 *
 * "Exactly once per post" matters because `IntersectionObserver` keeps
 * firing for as long as an element is observed — without self-terminating,
 * a card the player lingers on would dwell-complete, get reported, and then
 * (having never stopped being tracked) dwell-complete again on the next
 * ratio wobble. So the poll loop drops a post's registration the moment it
 * fires `onViewed`, the same way a fired timer doesn't refire.
 */

import {
  FEED_VIEW_VISIBILITY_RATIO,
  clearTrackedPosts,
  postsDueForView,
  trackExposure,
} from './feedViewTracking'

/** Called once, the first time a post's exposure dwell completes. */
export type FeedViewedListener = (postId: string) => void

interface Registration {
  postId: string
  onViewed: FeedViewedListener
}

/** How often the dwell map is checked for posts that have crossed the
 * threshold. Well under `FEED_VIEW_DWELL_MS` so the reported moment never
 * lags the actual dwell completion by more than a frame or two of polling,
 * without running a timer so tight it does real work every tick. */
const POLL_INTERVAL_MS = 200

const registrations = new Map<Element, Registration>()
let visibleSince = new Map<string, number>()
let sharedObserver: IntersectionObserver | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPoll(): void {
  if (pollTimer === null) return
  clearInterval(pollTimer)
  pollTimer = null
}

function dropRegistration(el: Element): void {
  registrations.delete(el)
  sharedObserver?.unobserve(el)
}

function runPollTick(): void {
  if (registrations.size === 0) {
    stopPoll()
    return
  }
  const due = postsDueForView(visibleSince, Date.now())
  if (due.length === 0) return
  visibleSince = clearTrackedPosts(visibleSince, due)

  for (const postId of due) {
    // Normally exactly one element carries a given post id; iterate
    // defensively in case a post briefly renders in two places at once
    // (e.g. an SSE-driven optimistic prepend racing a reload).
    const fired: FeedViewedListener[] = []
    for (const [el, reg] of registrations) {
      if (reg.postId !== postId) continue
      fired.push(reg.onViewed)
      dropRegistration(el)
    }
    for (const listener of fired) listener(postId)
  }
}

function ensurePoll(): void {
  if (pollTimer !== null) return
  pollTimer = setInterval(runPollTick, POLL_INTERVAL_MS)
}

function ensureObserver(): IntersectionObserver | null {
  if (typeof IntersectionObserver === 'undefined') return null
  if (sharedObserver) return sharedObserver
  sharedObserver = new IntersectionObserver((entries) => {
    const nowMs = Date.now()
    for (const entry of entries) {
      const reg = registrations.get(entry.target)
      if (!reg) continue
      visibleSince = trackExposure(visibleSince, {
        postId: reg.postId,
        ratio: entry.intersectionRatio,
        nowMs,
      })
    }
  }, { threshold: [0, FEED_VIEW_VISIBILITY_RATIO, 1] })
  return sharedObserver
}

/**
 * Watch `el` and report `postId` as viewed once its exposure dwell
 * completes. Fires `onViewed` at most once, then stops watching on its own
 * — no need to call the returned teardown after that.
 *
 * Returns the teardown. Callers must run it on unmount regardless of
 * whether `onViewed` already fired: the registration map holds a strong
 * reference to `el`, so a card that unmounts (scrolled out and reloaded
 * away) without unobserving would leak — the same reason
 * `observeOffscreenRelease`'s teardown is mandatory.
 *
 * With no `IntersectionObserver` this never fires and returns a no-op
 * teardown — the post simply never gets a client-side view report. The
 * like/comment fallback (`FeedReactionService` / `FeedCommentService`
 * backfilling `viewed_at`) is what covers a browser this old.
 */
export function observeFeedPostView(
  el: Element,
  postId: string,
  onViewed: FeedViewedListener,
): () => void {
  const observer = ensureObserver()
  if (!observer) return () => {}

  registrations.set(el, { postId, onViewed })
  observer.observe(el)
  ensurePoll()

  return () => {
    if (!registrations.has(el)) return
    dropRegistration(el)
  }
}
