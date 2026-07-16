/**
 * SSE consumer for feed-wall events.
 *
 * The backend emits two event names on the same
 * ``/api/v1/events/stream`` connection that already carries proactive
 * messages:
 *
 * - ``feed_post`` — a fresh character post landed (refresh feed list).
 * - ``feed_comment_reply`` — a scheduler-tick character reply landed
 *   on a user comment (bump the LumeGram launcher badge).
 *
 * Caller passes one or both handlers; we close on teardown. Wraps the
 * native ``EventSource`` so callers don't have to deal with
 * ``addEventListener`` / parse-error handling.
 *
 * Auth: ``EventSource`` cannot send Authorization headers, so when a
 * bearer token is stored we append it as ``?access_token=…``. Backend
 * `get_current_user` accepts the query param as an SSE-only fallback.
 */

import { getStoredToken } from '@/composables/useAuth'

export interface FeedPostEvent {
  type: 'feed_post'
  character_id: string
  post_id: string
  kind: string
  content_text: string
  image_url: string | null
  created_at: string
}

export interface FeedCommentReplyEvent {
  type: 'feed_comment_reply'
  character_id: string
  post_id: string
  comment_id: string
  content_text: string
  unread_count: number
  created_at: string
}

export type FeedEventHandler = (event: FeedPostEvent) => void
export type FeedCommentReplyHandler = (event: FeedCommentReplyEvent) => void

export interface FeedEventStreamHandle {
  close(): void
}

export interface FeedEventStreamOptions {
  onError?: (ev: Event) => void
  onCommentReply?: FeedCommentReplyHandler
}

export function connectFeedEvents(
  onEvent: FeedEventHandler,
  options: FeedEventStreamOptions = {},
): FeedEventStreamHandle {
  const token = getStoredToken()
  const url = token
    ? `/api/v1/events/stream?access_token=${encodeURIComponent(token)}`
    : '/api/v1/events/stream'
  const source = new EventSource(url)

  source.addEventListener('feed_post', (ev: MessageEvent) => {
    try {
      const payload = JSON.parse(ev.data) as FeedPostEvent
      onEvent(payload)
    } catch {
      // Malformed event — skip rather than tear down the stream.
    }
  })

  if (options.onCommentReply) {
    const handler = options.onCommentReply
    source.addEventListener('feed_comment_reply', (ev: MessageEvent) => {
      try {
        const payload = JSON.parse(ev.data) as FeedCommentReplyEvent
        handler(payload)
      } catch {
        // Malformed event — skip.
      }
    })
  }

  if (options.onError) {
    source.addEventListener('error', options.onError)
  }

  return {
    close() {
      source.close()
    },
  }
}
