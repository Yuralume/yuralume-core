import axios from 'axios'

import { i18n } from '@/i18n'
import type { IngestRunResult, WorldEvent } from '@/types/worldEvent'

export interface ListWorldEventsOptions {
  limit?: number
  maxAgeDays?: number
}

export async function listRecentWorldEvents(
  options: ListWorldEventsOptions = {},
): Promise<WorldEvent[]> {
  try {
    const params: Record<string, string | number> = {}
    if (options.limit !== undefined) params.limit = options.limit
    if (options.maxAgeDays !== undefined) params.max_age_days = options.maxAgeDays
    const res = await axios.get<WorldEvent[]>('/api/v1/world-events', { params })
    return res.data
  } catch {
    return []
  }
}

export async function triggerWorldEventIngest(): Promise<IngestRunResult> {
  try {
    const res = await axios.post<IngestRunResult>('/api/v1/world-events/ingest')
    return res.data
  } catch {
    return {
      fetched: 0,
      new: 0,
      embedded: 0,
      evicted: 0,
      errors: [i18n.global.t('worldAwarenessPanel.ingestUnavailable')],
    }
  }
}
