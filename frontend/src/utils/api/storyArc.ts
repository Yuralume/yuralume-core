import type {
  AddStoryArcBeatPayload,
  StartStoryArcPayload,
  StoryArc,
  UpdateStoryArcBeatPayload,
  UpdateStoryArcMetaPayload,
} from '@/types/storyArc'
import { authedFetch } from '@/utils/authedFetch'
import { readErrorResponse } from '@/utils/api/httpError'

const BASE = '/api/v1'

async function _req<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await authedFetch(`${BASE}${path}`, {
    headers: init.body
      ? { 'Content-Type': 'application/json', ...(init.headers || {}) }
      : init.headers,
    ...init,
  })
  if (!res.ok) throw new Error(await readErrorResponse(res))
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export async function listStoryArcs(
  characterId: string,
): Promise<StoryArc[]> {
  return _req(`/characters/${characterId}/story-arcs`)
}

export async function getActiveStoryArc(
  characterId: string,
): Promise<StoryArc | null> {
  return _req(`/characters/${characterId}/story-arcs/active`)
}

export async function startStoryArc(
  characterId: string,
  payload: StartStoryArcPayload = {},
): Promise<StoryArc> {
  return _req(`/characters/${characterId}/story-arcs`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function regenerateStoryArc(
  arcId: string,
  hint?: string,
): Promise<StoryArc> {
  return _req(`/story-arcs/${arcId}/regenerate`, {
    method: 'POST',
    body: JSON.stringify({ hint: hint || null }),
  })
}

export async function abandonStoryArc(arcId: string): Promise<StoryArc> {
  return _req(`/story-arcs/${arcId}/abandon`, { method: 'POST' })
}

export async function deleteStoryArc(arcId: string): Promise<void> {
  await _req(`/story-arcs/${arcId}`, { method: 'DELETE' })
}

export async function updateStoryArcMeta(
  arcId: string,
  payload: UpdateStoryArcMetaPayload,
): Promise<StoryArc> {
  return _req(`/story-arcs/${arcId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export async function addStoryArcBeat(
  arcId: string,
  payload: AddStoryArcBeatPayload,
): Promise<StoryArc> {
  return _req(`/story-arcs/${arcId}/beats`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function updateStoryArcBeat(
  beatId: string,
  payload: UpdateStoryArcBeatPayload,
): Promise<StoryArc> {
  return _req(`/story-arc-beats/${beatId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export async function deleteStoryArcBeat(beatId: string): Promise<StoryArc> {
  return _req(`/story-arc-beats/${beatId}`, { method: 'DELETE' })
}
