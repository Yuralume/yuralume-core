/**
 * Feed-post view tracking (KB11 of PLAYER_KNOWLEDGE_BOUNDARY_PLAN).
 *
 * **What is NOT guarded here, honestly.** The harness is SSR
 * (`createSSRApp` + `renderToString`, no jsdom by choice — the same call
 * `offscreenImageRelease.test.ts` documents). It cannot run an
 * `IntersectionObserver` and it cannot let a real timer fire, so nothing
 * below proves that scrolling a card into view for real actually reports it,
 * that the poll loop in `feedViewObserver.ts` really ticks every 200 ms, or
 * that Vue's `onMounted`/`onBeforeUnmount` really call what the wiring tests
 * below assert they call. Those are **manual acceptance** — see the
 * checklist at the bottom of this file.
 *
 * What *is* guarded: the two decisions that determine what gets sent and
 * when — "has this post dwelled long enough" and "is it time to flush the
 * batch" — as pure functions of numbers, plus (read straight from source)
 * that `FeedCard` and `FeedPanel` are still plugged into them the way this
 * file assumes.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import {
  FEED_VIEW_BATCH_INTERVAL_MS,
  FEED_VIEW_BATCH_MAX_IDS,
  FEED_VIEW_DWELL_MS,
  FEED_VIEW_VISIBILITY_RATIO,
  clearTrackedPosts,
  createFeedViewBatchState,
  enqueueViewed,
  flushBatch,
  postsDueForView,
  shouldFlushBatch,
  trackExposure,
} from '@/utils/feedViewTracking'

// ----------------------------------------------------------------------
// Dwell: which posts have been visible enough, for long enough
// ----------------------------------------------------------------------

describe('trackExposure', () => {
  it('starts the clock the moment a post crosses the ratio threshold', () => {
    const state = trackExposure(new Map(), {
      postId: 'p1', ratio: FEED_VIEW_VISIBILITY_RATIO, nowMs: 1000,
    })
    expect(state.get('p1')).toBe(1000)
  })

  it('does not track a post below the threshold', () => {
    const state = trackExposure(new Map(), {
      postId: 'p1', ratio: FEED_VIEW_VISIBILITY_RATIO - 0.01, nowMs: 1000,
    })
    expect(state.has('p1')).toBe(false)
  })

  it('does not push the start time forward on a repeat sample above threshold', () => {
    // A jittery observer can report the same crossing more than once; the
    // dwell has to measure from the *first* crossing or it would never
    // complete under a noisy callback.
    let state = trackExposure(new Map(), {
      postId: 'p1', ratio: 0.9, nowMs: 1000,
    })
    state = trackExposure(state, { postId: 'p1', ratio: 0.95, nowMs: 1500 })
    expect(state.get('p1')).toBe(1000)
  })

  it('resets the clock when a post drops back below the threshold', () => {
    // The dwell must accumulate in one continuous stretch, not fragments —
    // a fling-past-and-back must not count the same as lingering.
    let state = trackExposure(new Map(), {
      postId: 'p1', ratio: 0.9, nowMs: 1000,
    })
    state = trackExposure(state, { postId: 'p1', ratio: 0.1, nowMs: 1200 })
    expect(state.has('p1')).toBe(false)
    state = trackExposure(state, { postId: 'p1', ratio: 0.9, nowMs: 1300 })
    expect(state.get('p1')).toBe(1300)
  })

  it('takes a caller threshold — the export is a default, not the rule', () => {
    const state = trackExposure(
      new Map(), { postId: 'p1', ratio: 0.5, nowMs: 1000 }, 0.4,
    )
    expect(state.get('p1')).toBe(1000)
  })

  it('tracks multiple posts independently', () => {
    let state = trackExposure(new Map(), {
      postId: 'p1', ratio: 0.9, nowMs: 1000,
    })
    state = trackExposure(state, { postId: 'p2', ratio: 0.9, nowMs: 1050 })
    expect(state.get('p1')).toBe(1000)
    expect(state.get('p2')).toBe(1050)
  })

  it('does not mutate the map it was handed', () => {
    const original = new Map<string, number>()
    trackExposure(original, { postId: 'p1', ratio: 0.9, nowMs: 1000 })
    expect(original.size).toBe(0)
  })
})

describe('postsDueForView', () => {
  it('is empty until the dwell elapses', () => {
    const visibleSince = new Map([['p1', 1000]])
    expect(postsDueForView(visibleSince, 1000 + FEED_VIEW_DWELL_MS - 1))
      .toEqual([])
  })

  it('becomes due exactly at the dwell threshold', () => {
    const visibleSince = new Map([['p1', 1000]])
    expect(postsDueForView(visibleSince, 1000 + FEED_VIEW_DWELL_MS))
      .toEqual(['p1'])
  })

  it('returns every post that has independently dwelled long enough', () => {
    const visibleSince = new Map([['p1', 1000], ['p2', 1000], ['p3', 5000]])
    const due = postsDueForView(visibleSince, 1000 + FEED_VIEW_DWELL_MS)
    expect(new Set(due)).toEqual(new Set(['p1', 'p2']))
  })

  it('takes a caller dwell — the export is a default, not the rule', () => {
    const visibleSince = new Map([['p1', 1000]])
    expect(postsDueForView(visibleSince, 1300, 300)).toEqual(['p1'])
    expect(postsDueForView(visibleSince, 1299, 300)).toEqual([])
  })
})

describe('clearTrackedPosts', () => {
  it('removes only the given ids', () => {
    const visibleSince = new Map([['p1', 1000], ['p2', 1000]])
    const next = clearTrackedPosts(visibleSince, ['p1'])
    expect(next.has('p1')).toBe(false)
    expect(next.has('p2')).toBe(true)
  })

  it('is a no-op copy for an empty id list, not the same reference', () => {
    const visibleSince = new Map([['p1', 1000]])
    const next = clearTrackedPosts(visibleSince, [])
    expect(next).toEqual(visibleSince)
    expect(next).not.toBe(visibleSince)
  })

  it('does not mutate the map it was handed', () => {
    const original = new Map([['p1', 1000]])
    clearTrackedPosts(original, ['p1'])
    expect(original.has('p1')).toBe(true)
  })
})

// ----------------------------------------------------------------------
// Batching: when the pending ids actually go over the wire
// ----------------------------------------------------------------------

describe('enqueueViewed', () => {
  it('adds ids and stamps the enqueue time on the first add', () => {
    const state = enqueueViewed(createFeedViewBatchState(), ['p1'], 1000)
    expect(state.pendingIds.has('p1')).toBe(true)
    expect(state.oldestPendingAtMs).toBe(1000)
  })

  it('does not push oldestPendingAtMs forward on later adds', () => {
    // The interval is a ceiling on staleness, not a clock a busy scroll
    // session can indefinitely postpone.
    let state = enqueueViewed(createFeedViewBatchState(), ['p1'], 1000)
    state = enqueueViewed(state, ['p2'], 3000)
    expect(state.oldestPendingAtMs).toBe(1000)
    expect(state.pendingIds).toEqual(new Set(['p1', 'p2']))
  })

  it('is a no-op for an empty id list', () => {
    const initial = createFeedViewBatchState()
    expect(enqueueViewed(initial, [], 1000)).toBe(initial)
  })

  it('deduplicates ids across repeated enqueues', () => {
    let state = enqueueViewed(createFeedViewBatchState(), ['p1'], 1000)
    state = enqueueViewed(state, ['p1'], 1000)
    expect(state.pendingIds.size).toBe(1)
  })
})

describe('shouldFlushBatch', () => {
  it('never flushes an empty batch', () => {
    expect(shouldFlushBatch(createFeedViewBatchState(), 999999)).toBe(false)
  })

  it('flushes once the interval elapses since the oldest pending id', () => {
    const state = enqueueViewed(createFeedViewBatchState(), ['p1'], 1000)
    expect(shouldFlushBatch(state, 1000 + FEED_VIEW_BATCH_INTERVAL_MS - 1))
      .toBe(false)
    expect(shouldFlushBatch(state, 1000 + FEED_VIEW_BATCH_INTERVAL_MS))
      .toBe(true)
  })

  it('flushes early once the batch hits the size cap, interval or not', () => {
    let state = createFeedViewBatchState()
    const ids = Array.from({ length: FEED_VIEW_BATCH_MAX_IDS }, (_, i) => `p${i}`)
    state = enqueueViewed(state, ids, 1000)
    expect(shouldFlushBatch(state, 1000)).toBe(true)
  })

  it('takes caller options — the exports are defaults, not the rule', () => {
    const state = enqueueViewed(createFeedViewBatchState(), ['p1'], 1000)
    expect(shouldFlushBatch(state, 1100, { intervalMs: 100 })).toBe(true)
    expect(shouldFlushBatch(state, 1099, { intervalMs: 100 })).toBe(false)
    expect(shouldFlushBatch(state, 1000, { maxIds: 1 })).toBe(true)
  })
})

describe('flushBatch', () => {
  it('drains every pending id and resets the state', () => {
    const state = enqueueViewed(createFeedViewBatchState(), ['p1', 'p2'], 1000)
    const { ids, state: next } = flushBatch(state)
    expect(new Set(ids)).toEqual(new Set(['p1', 'p2']))
    expect(next.pendingIds.size).toBe(0)
    expect(next.oldestPendingAtMs).toBeNull()
  })

  it('is always safe to call on an empty batch', () => {
    const { ids, state } = flushBatch(createFeedViewBatchState())
    expect(ids).toEqual([])
    expect(state.pendingIds.size).toBe(0)
  })

  it('lets enqueueing resume with a fresh deadline after a flush', () => {
    let state = enqueueViewed(createFeedViewBatchState(), ['p1'], 1000)
    ;({ state } = flushBatch(state))
    state = enqueueViewed(state, ['p2'], 9000)
    expect(state.oldestPendingAtMs).toBe(9000)
  })
})

// ----------------------------------------------------------------------
// Wiring, read from source
//
// The SSR harness cannot mount `FeedCard`'s `onMounted` hook (Vue SSR
// never runs lifecycle hooks during `renderToString`) and cannot exercise
// a scroll or a timer. These read the connections that, if dropped, leave
// every assertion above passing and the feature entirely absent.
// ----------------------------------------------------------------------

function source(file: string): string {
  return readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
}

describe('FeedCard view wiring', () => {
  const card = source('components/FeedCard.vue')

  it('observes its own root element, skipping a post already known viewed', () => {
    expect(card).toContain('ref="cardEl"')
    expect(card).toContain('if (props.post.viewed_at) return')
    expect(card).toContain('observeFeedPostView(cardEl.value, props.post.id')
  })

  it('emits viewed rather than calling the API itself', () => {
    // The batching/grouping-by-character decision belongs one level up
    // (FeedPanel knows every currently-rendered post's character_id; a
    // single card does not need to). A card that called the API directly
    // would defeat the batching entirely.
    expect(card).toMatch(/\(e: 'viewed', postId: string\): void/)
    expect(card).toContain("emit('viewed', postId)")
  })

  it('tears down the observer on unmount even if it never fired', () => {
    expect(card).toMatch(/onBeforeUnmount\(\(\) => \{\s*stopObservingView\?\.\(\)/)
  })
})

describe('FeedPanel view-batch wiring', () => {
  const panel = source('components/FeedPanel.vue')

  it('wires the card emit into the batcher', () => {
    expect(panel).toContain('@viewed="handlePostViewed"')
    expect(panel).toContain('enqueueViewed(viewBatch,')
  })

  it('groups a flush by character_id before calling the per-character endpoint', () => {
    // The endpoint is `/characters/{character_id}/feed/viewed` — a global
    // wall's pending batch can span more than one of the caller's
    // characters, and the id alone doesn't say which.
    expect(panel).toContain('byCharacter.get(post.character_id)')
    expect(panel).toContain('markFeedPostsViewed(characterId, postIds)')
  })

  it('marks posts viewed locally so a re-render does not re-observe them', () => {
    expect(panel).toContain('applyViewedLocally(ids)')
  })

  it('flushes on unmount instead of dropping whatever is still pending', () => {
    expect(panel).toMatch(/onBeforeUnmount\(\(\) => \{[\s\S]{0,400}flushViewBatch\(\)/)
  })

  it('polls the flush decision so a batch below the size cap eventually ships', () => {
    // Without a periodic check, a batch that never reaches
    // FEED_VIEW_BATCH_MAX_IDS would sit pending until something else
    // happened to enqueue another id.
    expect(panel).toContain('shouldFlushBatch(viewBatch, Date.now())')
    expect(panel).toMatch(/setInterval\(\(\) => \{[\s\S]{0,200}flushViewBatch/)
  })
})

// ----------------------------------------------------------------------
// Manual acceptance — no guard exists for any of this, by construction
// ----------------------------------------------------------------------
//
// 1. Open LumeGram, scroll a card fully into view and hold the scroll for
//    ~1s: `POST /api/v1/characters/{id}/feed/viewed` should fire in the
//    network tab shortly after (batched with any other card that dwelled
//    in the same window), carrying that card's post id.
// 2. Fling past a card without stopping: it must NOT appear in the next
//    viewed batch.
// 3. Reload the page after (1): the card's viewed_at should now be set (no
//    server round trip needed to re-establish it — it came back with the
//    list), and it should not fire a second viewed report on re-scroll.
// 4. Like or comment on a post you have never scrolled past (e.g. jump
//    straight to it via a permalink): the like/comment fallback should
//    still set viewed_at, confirmed by reloading and checking the post
//    payload.
// 5. Close the feed panel (unmount) immediately after a card dwells but
//    before the 4s batch interval elapses: the unmount flush should still
//    send it — check the network tab for a request right at close, not a
//    dropped report.
