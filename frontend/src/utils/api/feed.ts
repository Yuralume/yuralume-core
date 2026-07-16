/**
 * Feed-wall API wrappers.
 *
 * Phase 1 read paths + Phase A1 like/unlike. Both like endpoints are
 * idempotent and the server stamps the local user identity, so the UI
 * never plumbs a liker_id.
 */

import axios from 'axios'

import type {
  FeedComment,
  FeedCommentListResponse,
  FeedListResponse,
  FeedPost,
  FeedReactionState,
} from '@/types/feed'

export interface ListFeedOptions {
  limit?: number
  before?: string | null
}

export async function listCharacterFeed(
  characterId: string,
  options: ListFeedOptions = {},
): Promise<FeedListResponse> {
  const params: Record<string, string | number> = {}
  if (options.limit !== undefined) params.limit = options.limit
  if (options.before) params.before = options.before
  const res = await axios.get<FeedListResponse>(
    `/api/v1/characters/${characterId}/feed`,
    { params },
  )
  return res.data
}

// 全局動態牆 —— 跨所有角色、按 created_at 倒序。前端拿到之後再用
// 已經 cache 在記憶體裡的 character map 補上頭像 / 名字。
export async function listGlobalFeed(
  options: ListFeedOptions = {},
): Promise<FeedListResponse> {
  const params: Record<string, string | number> = {}
  if (options.limit !== undefined) params.limit = options.limit
  if (options.before) params.before = options.before
  const res = await axios.get<FeedListResponse>('/api/v1/feed', { params })
  return res.data
}

export interface FeedUnreadResult {
  count: number
}

// 全局未讀貼文數 —— ``since`` 是前端 ``localStorage`` 維護的 watermark
// （上次打開動態牆的時間）。沒傳 since 就回 0，不會在初次造訪時亮紅
// 點吵到使用者。
export async function getGlobalFeedUnread(
  since: string | null,
): Promise<FeedUnreadResult> {
  const params: Record<string, string> = {}
  if (since) params.since = since
  const res = await axios.get<FeedUnreadResult>(
    '/api/v1/feed/unread', { params },
  )
  return res.data
}

export async function getFeedPost(postId: string): Promise<FeedPost> {
  const res = await axios.get<FeedPost>(`/api/v1/feed/posts/${postId}`)
  return res.data
}

export async function likeFeedPost(postId: string): Promise<FeedReactionState> {
  const res = await axios.post<FeedReactionState>(
    `/api/v1/feed/posts/${postId}/like`,
  )
  return res.data
}

export async function unlikeFeedPost(postId: string): Promise<FeedReactionState> {
  const res = await axios.delete<FeedReactionState>(
    `/api/v1/feed/posts/${postId}/like`,
  )
  return res.data
}

export async function listFeedComments(
  postId: string,
  options: { limit?: number } = {},
): Promise<FeedCommentListResponse> {
  const params: Record<string, string | number> = {}
  if (options.limit !== undefined) params.limit = options.limit
  const res = await axios.get<FeedCommentListResponse>(
    `/api/v1/feed/posts/${postId}/comments`,
    { params },
  )
  return res.data
}

export async function createFeedComment(
  postId: string,
  contentText: string,
): Promise<FeedComment> {
  const res = await axios.post<FeedComment>(
    `/api/v1/feed/posts/${postId}/comments`,
    { content_text: contentText },
  )
  return res.data
}

export async function deleteFeedComment(commentId: string): Promise<void> {
  await axios.delete(`/api/v1/feed/comments/${commentId}`)
}

export interface ManualFeedPostInput {
  content_text: string
  kind?: string
  image_url?: string | null
  image_prompt?: string | null
}

export async function createManualFeedPost(
  characterId: string,
  payload: ManualFeedPostInput,
): Promise<FeedPost> {
  const res = await axios.post<FeedPost>(
    `/api/v1/characters/${characterId}/feed`,
    payload,
  )
  return res.data
}

export interface FeedSeenResult {
  updated: number
}

// Fire when the user opens the feed panel so unseen likes/comments still
// turn into character memories even if the user never opens chat in the
// same session. Safe to call repeatedly — the server is idempotent via
// the per-post ``reactions_seen_at`` watermark.
export async function markFeedReactionsSeen(
  characterId: string,
): Promise<FeedSeenResult> {
  const res = await axios.post<FeedSeenResult>(
    `/api/v1/characters/${characterId}/feed/seen`,
  )
  return res.data
}
