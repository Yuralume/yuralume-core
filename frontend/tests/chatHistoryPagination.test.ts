import { readFileSync } from 'node:fs'

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import {
  CONVERSATION_PAGE_SIZE,
  getLatestConversation,
} from '@/utils/api/chat'
import {
  LOAD_OLDER_THRESHOLD_PX,
  prependOlderMessages,
  restoredScrollTop,
  shiftPinnedIndex,
  shouldLoadOlder,
} from '@/utils/chatHistoryScroll'

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

function jsonText(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
  } as Response
}

function requestedUrl(call = 0): URL {
  return new URL(
    mockedAuthedFetch.mock.calls[call][0] as string,
    'https://example.test',
  )
}

// ----------------------------------------------------------------------
// Transport
// ----------------------------------------------------------------------
describe('conversation page requests', () => {
  it('asks for one page, not the whole thread', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonText({
      id: 'conv-1', character_id: 'char-1', messages: [],
      has_more: false, next_before: null,
    }))

    await getLatestConversation('char-1')

    const url = requestedUrl()
    expect(url.pathname).toBe('/api/v1/characters/char-1/conversations/latest')
    expect(url.searchParams.get('limit')).toBe(String(CONVERSATION_PAGE_SIZE))
    // No cursor on the first page — that is what "newest" means here.
    expect(url.searchParams.has('before')).toBe(false)
  })

  it('carries the cursor for an older page', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonText({
      id: 'conv-1', character_id: 'char-1', messages: [],
      has_more: true, next_before: 20,
    }))

    await getLatestConversation('char-1', { before: 70, limit: 25 })

    const url = requestedUrl()
    expect(url.searchParams.get('before')).toBe('70')
    expect(url.searchParams.get('limit')).toBe('25')
  })

  it('treats before: 0 as a real cursor, not an absent one', async () => {
    // Position 0 is the first message ever sent; dropping the parameter
    // because the number is falsy would silently re-fetch the newest page and
    // loop forever at the top of a thread.
    mockedAuthedFetch.mockResolvedValueOnce(jsonText({
      id: 'conv-1', character_id: 'char-1', messages: [], has_more: false,
    }))

    await getLatestConversation('char-1', { before: 0 })

    expect(requestedUrl().searchParams.get('before')).toBe('0')
  })

  it('parses the keyset fields off the page', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonText({
      id: 'conv-1',
      character_id: 'char-1',
      messages: [{ role: 'user', content: 'hi' }],
      has_more: true,
      next_before: 12,
    }))

    const page = await getLatestConversation('char-1')

    expect(page?.has_more).toBe(true)
    expect(page?.next_before).toBe(12)
  })

  it('reads a backend that predates pagination as "nothing older"', async () => {
    // The fields are a superset; an older self-hosted backend sends the whole
    // thread and no flags, and the panel must not then offer to load more.
    mockedAuthedFetch.mockResolvedValueOnce(jsonText({
      id: 'conv-1',
      character_id: 'char-1',
      messages: [{ role: 'user', content: 'hi' }],
    }))

    const page = await getLatestConversation('char-1')

    expect(page?.has_more ?? false).toBe(false)
    expect(page?.next_before ?? null).toBeNull()
  })

  it('still reads a character with no conversation as null', async () => {
    mockedAuthedFetch.mockResolvedValueOnce({
      ok: true, status: 200, text: async () => 'null',
    } as Response)

    expect(await getLatestConversation('char-1')).toBeNull()
  })
})

// ----------------------------------------------------------------------
// When to fetch
// ----------------------------------------------------------------------
describe('shouldLoadOlder', () => {
  const base = { scrollTop: 0, hasMore: true, loading: false }

  it('fires with runway left, before the reader hits the wall', () => {
    expect(shouldLoadOlder({ ...base, scrollTop: LOAD_OLDER_THRESHOLD_PX - 1 }))
      .toBe(true)
  })

  it('stays quiet further down the thread', () => {
    expect(shouldLoadOlder({ ...base, scrollTop: LOAD_OLDER_THRESHOLD_PX + 1 }))
      .toBe(false)
  })

  it('never fires when the server said there is nothing older', () => {
    expect(shouldLoadOlder({ ...base, hasMore: false })).toBe(false)
  })

  it('does not stack requests while one is in flight', () => {
    // Scroll events fire per frame; without this a single flick would queue a
    // dozen fetches for the same cursor.
    expect(shouldLoadOlder({ ...base, loading: true })).toBe(false)
  })

  it('refuses to reflow the thread mid-turn', () => {
    expect(shouldLoadOlder({ ...base, busy: true })).toBe(false)
  })
})

// ----------------------------------------------------------------------
// Keeping the reader's place
// ----------------------------------------------------------------------
describe('restoredScrollTop', () => {
  it('slides scrollTop by exactly what was inserted above it', () => {
    // 1200px of older messages went in above the viewport, so the message the
    // reader was looking at is now 1200px further down.
    expect(restoredScrollTop({ scrollTop: 40, scrollHeight: 3000 }, 4200))
      .toBe(1240)
  })

  it('absorbs a shrink in the same update', () => {
    // The last page also removes the "load older" button (say 34px), so the
    // net growth — not the height of the inserted rows — is the correction.
    expect(restoredScrollTop({ scrollTop: 40, scrollHeight: 3000 }, 4166))
      .toBe(1206)
  })

  it('leaves the position alone when the container did not grow', () => {
    expect(restoredScrollTop({ scrollTop: 120, scrollHeight: 3000 }, 3000))
      .toBe(120)
    expect(restoredScrollTop({ scrollTop: 120, scrollHeight: 3000 }, 2800))
      .toBe(120)
  })

  it('leaves the position alone when the measurement is not a number', () => {
    expect(restoredScrollTop({ scrollTop: 120, scrollHeight: 3000 }, NaN))
      .toBe(120)
  })

  it('keeps a reader pinned at the very top pinned to the same message', () => {
    // scrollTop 0 is the one case where "do nothing" looks fine and is not:
    // the thread would jump to the newly loaded oldest message.
    expect(restoredScrollTop({ scrollTop: 0, scrollHeight: 800 }, 2400))
      .toBe(1600)
  })
})

// ----------------------------------------------------------------------
// Index bookkeeping
// ----------------------------------------------------------------------
describe('prependOlderMessages', () => {
  it('puts the older page first and reports how many arrived', () => {
    const result = prependOlderMessages(['a', 'b'], ['c', 'd'])
    expect(result.messages).toEqual(['a', 'b', 'c', 'd'])
    expect(result.added).toBe(2)
  })

  it('does not mutate either input', () => {
    const older = ['a']
    const current = ['b']
    prependOlderMessages(older, current)
    expect(older).toEqual(['a'])
    expect(current).toEqual(['b'])
  })
})

describe('shiftPinnedIndex', () => {
  it('moves an index that pointed into the thread', () => {
    // The closing narration of a scene is addressed by index; leave it alone
    // and the scene heading jumps onto an unrelated message.
    expect(shiftPinnedIndex(3, 50)).toBe(53)
  })

  it('leaves "nothing pinned" alone', () => {
    expect(shiftPinnedIndex(null, 50)).toBeNull()
  })
})

// ----------------------------------------------------------------------
// Panel wiring
//
// The SSR harness cannot render ChatPanel (too large) and cannot measure
// layout or fire scroll events at all, so the wiring is read from source.
// These are the connections that, if dropped, leave the arithmetic above
// perfectly correct and the feature perfectly broken.
// ----------------------------------------------------------------------
function source(file: string): string {
  return readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
}

describe('chat panel pagination wiring', () => {
  const panel = source('components/ChatPanel.vue')

  it('listens to the message container scrolling', () => {
    expect(panel).toContain('@scroll.passive="handleMessagesScroll"')
  })

  it('restores the anchor rather than scrolling to bottom after a prepend', () => {
    expect(panel).toContain('restoredScrollTop(anchor, el.scrollHeight)')
  })

  it('shifts the closing-narration index by the prepended count', () => {
    expect(panel).toContain('shiftPinnedIndex(')
  })

  it('still scrolls to the bottom when the parent hands over a thread', () => {
    expect(panel).toMatch(/watch\(\(\) => props\.messages[\s\S]{0,400}scrollToBottom\(\)/)
  })
})

describe('stage page loads one page', () => {
  const page = source('pages/StagePage.vue')

  it('seeds the panel cursor from the loaded page', () => {
    expect(page).toContain('historyPage.value = {')
    expect(page).toContain(':history-page="historyPage"')
  })
})
