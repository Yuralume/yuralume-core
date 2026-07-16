/**
 * In-app push via Server-Sent Events.
 *
 * Opens one long-lived ``EventSource`` to ``/api/v1/events/stream`` and
 * dispatches ``proactive_message`` events to a handler supplied by the
 * caller. Native ``EventSource`` auto-reconnects with an exponential
 * backoff so we don't need to implement it ourselves; we only close()
 * explicitly on app teardown.
 *
 * We stay on the built-in EventSource API (no fetch-streaming polyfill)
 * because the backend sets proper ``Content-Type: text/event-stream``
 * and Vite proxies it through unchanged. If CORS / auth headers become
 * an issue we can swap to a polyfill here without changing callers.
 *
 * Auth: ``EventSource`` cannot send custom Authorization headers, so
 * when a bearer token is stored we append it as ``?access_token=…``.
 * The backend dependency (`get_current_user`) accepts the query
 * parameter as a fallback specifically for SSE.
 */

import { getStoredToken } from '@/composables/useAuth'

export interface ProactiveMessageEvent {
  type: 'proactive_message'
  character_id: string
  conversation_id: string
  message: string
  unread_count: number
  created_at: string
}

export type ProactiveEventHandler = (event: ProactiveMessageEvent) => void

export interface EventStreamHandle {
  close(): void
}

export function connectProactiveEvents(
  onEvent: ProactiveEventHandler,
  options: { onError?: (ev: Event) => void } = {},
): EventStreamHandle {
  const token = getStoredToken()
  const url = token
    ? `/api/v1/events/stream?access_token=${encodeURIComponent(token)}`
    : '/api/v1/events/stream'
  const source = new EventSource(url)

  source.addEventListener('proactive_message', (ev: MessageEvent) => {
    try {
      const payload = JSON.parse(ev.data) as ProactiveMessageEvent
      onEvent(payload)
    } catch {
      // Malformed event — ignore rather than let the stream die.
    }
  })

  if (options.onError) {
    source.addEventListener('error', options.onError)
  }

  return {
    close() {
      source.close()
    },
  }
}
