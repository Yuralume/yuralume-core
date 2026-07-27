import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createEventStreamDedup } from '@/utils/eventStreamDedup'

vi.mock('@/composables/useAuth', () => ({
  getStoredToken: () => null,
}))

// --- pure dedup predicate -------------------------------------------------

describe('createEventStreamDedup', () => {
  it('processes each durable id once, skips replays', () => {
    const shouldProcess = createEventStreamDedup()
    expect(shouldProcess('5')).toBe(true)
    expect(shouldProcess('6')).toBe(true)
    expect(shouldProcess('5')).toBe(false) // replay of a confirmed id
    expect(shouldProcess('6')).toBe(false)
    expect(shouldProcess('7')).toBe(true)
  })

  it('always processes empty ids (in-memory backend has no id: line)', () => {
    const shouldProcess = createEventStreamDedup()
    // Self-host red line: no durable id → no replay → never de-duped.
    expect(shouldProcess('')).toBe(true)
    expect(shouldProcess('')).toBe(true)
    expect(shouldProcess(null)).toBe(true)
    expect(shouldProcess(undefined)).toBe(true)
  })

  it('is bounded — evicts the oldest id past capacity', () => {
    const shouldProcess = createEventStreamDedup(2)
    expect(shouldProcess('1')).toBe(true)
    expect(shouldProcess('2')).toBe(true)
    expect(shouldProcess('2')).toBe(false) // still remembered
    expect(shouldProcess('3')).toBe(true) // over capacity → evicts oldest '1'
    // '1' was evicted, so it is treated as new again (bounded, not perfect).
    expect(shouldProcess('1')).toBe(true)
    // '3' is still remembered.
    expect(shouldProcess('3')).toBe(false)
  })
})

// --- consumer wiring (replay de-dup on reconnect) -------------------------

type Listener = (ev: { data: string; lastEventId: string }) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  listeners = new Map<string, Listener[]>()
  closed = false
  url: string

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, handler: Listener) {
    const list = this.listeners.get(type) ?? []
    list.push(handler)
    this.listeners.set(type, list)
  }

  emit(type: string, data: unknown, lastEventId = '') {
    for (const handler of this.listeners.get(type) ?? []) {
      handler({ data: JSON.stringify(data), lastEventId })
    }
  }

  close() {
    this.closed = true
  }
}

describe('SSE consumers de-dupe replayed events on reconnect', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('proactive: a replayed id fires the handler only once', async () => {
    const { connectProactiveEvents } = await import('@/utils/proactiveEvents')
    const seen: string[] = []
    connectProactiveEvents((e) => seen.push(e.conversation_id))
    const source = FakeEventSource.instances[0]

    const payload = {
      type: 'proactive_message',
      character_id: 'c1',
      conversation_id: 'conv-1',
      message: 'hi',
      unread_count: 1,
      created_at: '2026-07-23T00:00:00Z',
    }
    source.emit('proactive_message', payload, '10') // live
    source.emit('proactive_message', payload, '10') // replay after reconnect

    expect(seen).toEqual(['conv-1']) // delivered once, replay skipped
  })

  it('feed: a replayed feed_post does not double-count', async () => {
    const { connectFeedEvents } = await import('@/utils/feedEvents')
    let count = 0
    connectFeedEvents(() => {
      count += 1
    })
    const source = FakeEventSource.instances[0]

    const post = {
      type: 'feed_post',
      character_id: 'c1',
      post_id: 'p1',
      kind: 'daily',
      content_text: 'x',
      image_url: null,
      created_at: '2026-07-23T00:00:00Z',
    }
    source.emit('feed_post', post, '20') // live
    source.emit('feed_post', post, '20') // replay after reconnect

    expect(count).toBe(1) // the badge increments once, not twice
  })

  it('in-memory backend (empty id) is never de-duped', async () => {
    const { connectFeedEvents } = await import('@/utils/feedEvents')
    let count = 0
    connectFeedEvents(() => {
      count += 1
    })
    const source = FakeEventSource.instances[0]
    const post = {
      type: 'feed_post',
      character_id: 'c1',
      post_id: 'p1',
      kind: 'daily',
      content_text: 'x',
      image_url: null,
      created_at: '2026-07-23T00:00:00Z',
    }
    source.emit('feed_post', post, '') // no durable id
    source.emit('feed_post', post, '')

    expect(count).toBe(2) // self-host behaviour unchanged
  })
})
