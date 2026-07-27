/**
 * Bounded event-id de-duplication for SSE consumers.
 *
 * The realtime outbox is at-least-once by design: on a ``Last-Event-ID``
 * reconnect the server re-emits the out-of-order-commit overlap tail, so an
 * event the browser already processed live before the drop is replayed again
 * (sol #12). Native ``EventSource`` reconnects transparently and re-fires the
 * same handlers, so the consumer — not the server — owns the de-dup.
 *
 * ``createEventStreamDedup`` returns a predicate: ``true`` = process this event,
 * ``false`` = already seen, skip. It is:
 *
 * - **Backend-aware.** The in-memory (self-host) backend emits no ``id:`` line,
 *   so ``EventSource`` reports an empty ``lastEventId``; with no id there is no
 *   replay and nothing to de-dup, so empty ids always pass through. De-dup is a
 *   postgres-backend-only concern and stays invisible to self-host.
 * - **Bounded.** At most ``capacity`` ids are retained (FIFO eviction of the
 *   oldest). Re-processing an evicted-then-replayed id is harmless for a badge
 *   that far in the past; unbounded memory is not.
 *
 * One instance per logical connection, held in the connect() closure so it
 * survives the EventSource's own auto-reconnects.
 */

const DEFAULT_CAPACITY = 1024

export type EventStreamDedup = (eventId: string | null | undefined) => boolean

export function createEventStreamDedup(
  capacity: number = DEFAULT_CAPACITY,
): EventStreamDedup {
  const cap = Math.max(1, Math.floor(capacity))
  const seen = new Set<string>()
  const order: string[] = []

  return (eventId) => {
    // No durable id (in-memory backend) → no replay → always process.
    if (!eventId) return true
    if (seen.has(eventId)) return false
    seen.add(eventId)
    order.push(eventId)
    if (order.length > cap) {
      const evicted = order.shift()
      if (evicted !== undefined) seen.delete(evicted)
    }
    return true
  }
}
