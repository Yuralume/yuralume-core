export interface LumeGramUnreadCharacter {
  id: string
  unread_feed_reply_count?: number | null
}

function normalizeUnreadCount(value: number | null | undefined): number {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return 0
  }
  return Math.max(0, Math.floor(value))
}

export function countUnreadFeedReplies(
  characters: readonly LumeGramUnreadCharacter[],
): number {
  return characters.reduce(
    (total, character) => total + normalizeUnreadCount(
      character.unread_feed_reply_count,
    ),
    0,
  )
}

export function characterIdsWithUnreadFeedReplies(
  characters: readonly LumeGramUnreadCharacter[],
): string[] {
  return characters
    .filter(character => normalizeUnreadCount(character.unread_feed_reply_count) > 0)
    .map(character => character.id)
}

export function totalLumeGramUnread(
  unreadFeedPosts: number,
  characters: readonly LumeGramUnreadCharacter[],
): number {
  return normalizeUnreadCount(unreadFeedPosts) + countUnreadFeedReplies(characters)
}

export function formatLumeGramBadge(count: number): string {
  const normalized = normalizeUnreadCount(count)
  return normalized > 99 ? '99+' : String(normalized)
}
