import type {
  CreateStorySeedRequest,
  StoryEvent,
  StorySeed,
  UpdateStorySeedRequest,
} from '../../types/story'
import { authedFetch } from '@/utils/authedFetch'

export async function listStoryEvents(
  characterId: string,
  limit = 10,
): Promise<StoryEvent[]> {
  const res = await authedFetch(
    `/api/v1/characters/${characterId}/story-events?limit=${limit}`,
  )
  if (!res.ok) throw new Error(`Failed to list story events (${res.status})`)
  return res.json()
}

export async function rollStoryEvent(characterId: string): Promise<StoryEvent[]> {
  const res = await authedFetch(
    `/api/v1/characters/${characterId}/story-events/roll`,
    { method: 'POST' },
  )
  if (!res.ok) throw new Error(`Roll failed (${res.status})`)
  return res.json()
}

export async function listStorySeeds(
  characterId: string,
  opts: { includeGlobal?: boolean; enabledOnly?: boolean } = {},
): Promise<StorySeed[]> {
  const qs = new URLSearchParams()
  if (opts.includeGlobal !== undefined) qs.set('include_global', String(opts.includeGlobal))
  if (opts.enabledOnly !== undefined) qs.set('enabled_only', String(opts.enabledOnly))
  const url = `/api/v1/characters/${characterId}/story-seeds${qs.toString() ? '?' + qs : ''}`
  const res = await authedFetch(url)
  if (!res.ok) throw new Error(`Failed to list seeds (${res.status})`)
  return res.json()
}

export async function createStorySeed(
  characterId: string,
  payload: CreateStorySeedRequest,
): Promise<StorySeed> {
  const res = await authedFetch(
    `/api/v1/characters/${characterId}/story-seeds`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  )
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`Create failed (${res.status}): ${body}`)
  }
  return res.json()
}

export async function updateStorySeed(
  seedId: string,
  payload: UpdateStorySeedRequest,
): Promise<StorySeed> {
  const res = await authedFetch(`/api/v1/story-seeds/${seedId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`Update failed (${res.status}): ${body}`)
  }
  return res.json()
}

export async function deleteStorySeed(seedId: string): Promise<void> {
  const res = await authedFetch(`/api/v1/story-seeds/${seedId}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 204) {
    const body = await res.text()
    throw new Error(`Delete failed (${res.status}): ${body}`)
  }
}
