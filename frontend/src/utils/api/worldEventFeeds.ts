import axios from 'axios'

/**
 * World-event RSS feed CRUD (CORE_ENV_TO_ADMIN_CONFIG track 3). Backed by
 * the rss_sources table; replaces the deprecated KOKORO_WORLD_EVENT_FEED_*
 * env family. Feeds seeded from env / rss_sources.yaml appear here too.
 */

export interface WorldEventFeed {
  id: string
  name: string
  feed_url: string
  category: string
  locale: string
  /** Content region (TW / JP …). null = global source, curated for every player. */
  region: string | null
  enabled: boolean
  health_status: string
  last_success_at: string | null
  last_attempt_at: string | null
  last_error: string | null
  fetched_count_total: number
}

export interface WorldEventFeedList {
  sources: WorldEventFeed[]
  total: number
  enabled: number
  failing: number
}

export interface FeedCreatePayload {
  id: string
  name?: string
  feed_url: string
  category?: string
  locale?: string
  region?: string
  enabled?: boolean
}

/**
 * Partial feed update. Omitting a field keeps its current value; passing an
 * empty string for `region` is the explicit "back to global" clear.
 */
export type FeedUpdatePayload = Partial<
  Pick<WorldEventFeed, 'name' | 'feed_url' | 'category' | 'locale' | 'enabled'>
> & { region?: string }

export async function listWorldEventFeeds(): Promise<WorldEventFeedList> {
  const { data } = await axios.get<WorldEventFeedList>(
    '/api/v1/admin/world-events/sources',
  )
  return data
}

export async function createWorldEventFeed(
  payload: FeedCreatePayload,
): Promise<WorldEventFeed> {
  const { data } = await axios.post<WorldEventFeed>(
    '/api/v1/admin/world-events/sources',
    payload,
  )
  return data
}

export async function updateWorldEventFeed(
  id: string,
  patch: FeedUpdatePayload,
): Promise<WorldEventFeed> {
  const { data } = await axios.patch<WorldEventFeed>(
    `/api/v1/admin/world-events/sources/${encodeURIComponent(id)}`,
    patch,
  )
  return data
}

export async function deleteWorldEventFeed(id: string): Promise<void> {
  await axios.delete(`/api/v1/admin/world-events/sources/${encodeURIComponent(id)}`)
}
