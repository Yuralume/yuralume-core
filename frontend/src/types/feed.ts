/**
 * Feed-wall (動態牆) DTOs — mirror ``application/dto/feed.py``.
 *
 * Each character publishes Instagram-style posts on its own tick. The
 * sidebar panel renders this list reverse-chronologically with cursor
 * pagination via ``next_before``.
 */

export interface FeedSourceDto {
  kind: string
  ref_id: string | null
}

export interface FeedReactionSummary {
  likes: number
  comments: number
}

export interface FeedPost {
  id: string
  character_id: string
  kind: string
  content_text: string
  source: FeedSourceDto
  image_url: string | null
  image_prompt: string | null
  video_url: string | null
  video_prompt: string | null
  reactions: FeedReactionSummary
  reactions_seen_at: string | null
  created_at: string
  liked: boolean
}

export interface FeedReactionState {
  post_id: string
  liked: boolean
  likes: number
}

export interface FeedComment {
  id: string
  post_id: string
  author_id: string
  author_display_name: string | null
  /**
   * True when `author_display_name` is the seeded `操作者` placeholder;
   * render a localized label instead of the raw sentinel.
   */
  author_display_name_is_placeholder?: boolean
  content_text: string
  created_at: string
}

export interface FeedCommentListResponse {
  items: FeedComment[]
}

export interface FeedListResponse {
  items: FeedPost[]
  has_more: boolean
  next_before: string | null
}
