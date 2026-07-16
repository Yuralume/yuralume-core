import { describe, expect, it } from 'vitest'

import {
  characterIdsWithUnreadFeedReplies,
  countUnreadFeedReplies,
  formatLumeGramBadge,
  totalLumeGramUnread,
} from '@/utils/lumeGramUnread'

describe('LumeGram unread helpers', () => {
  it('adds unread character replies across characters', () => {
    expect(countUnreadFeedReplies([
      { id: 'alice', unread_feed_reply_count: 2 },
      { id: 'bob', unread_feed_reply_count: 1 },
      { id: 'clara', unread_feed_reply_count: 0 },
    ])).toBe(3)
  })

  it('combines unread posts with unread character replies for the launcher badge', () => {
    expect(totalLumeGramUnread(4, [
      { id: 'alice', unread_feed_reply_count: 2 },
      { id: 'bob', unread_feed_reply_count: 3 },
    ])).toBe(9)
  })

  it('ignores invalid or negative unread values', () => {
    expect(totalLumeGramUnread(-2, [
      { id: 'alice', unread_feed_reply_count: -1 },
      { id: 'bob', unread_feed_reply_count: null },
      { id: 'clara' },
    ])).toBe(0)
  })

  it('returns only character ids that need feed seen reconciliation', () => {
    expect(characterIdsWithUnreadFeedReplies([
      { id: 'alice', unread_feed_reply_count: 2 },
      { id: 'bob', unread_feed_reply_count: 0 },
      { id: 'clara', unread_feed_reply_count: 1 },
    ])).toEqual(['alice', 'clara'])
  })

  it('formats large badge counts with the existing compact cap', () => {
    expect(formatLumeGramBadge(100)).toBe('99+')
    expect(formatLumeGramBadge(7)).toBe('7')
  })
})
