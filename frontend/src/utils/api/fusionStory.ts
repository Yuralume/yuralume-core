import type {
  CreateFusionStoryPayload,
  FusionStory,
  FusionToArcDraftPayload,
  FusionStorySummary,
  IterateBeatPayload,
  IterateOutlinePayload,
} from '@/types/fusionStory'
import type { TemplateDraftPayload } from '@/types/arcTemplateIntake'
import { authedFetch } from '@/utils/authedFetch'
import { readErrorResponse } from '@/utils/api/httpError'

const BASE = '/api/v1'

async function _req<T>(path: string, init: RequestInit = {}): Promise<T> {
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

export async function listFusionStories(
  limit = 50,
): Promise<FusionStorySummary[]> {
  return _req(`/fusion-stories?limit=${encodeURIComponent(limit)}`)
}

export async function getFusionStory(storyId: string): Promise<FusionStory> {
  return _req(`/fusion-stories/${encodeURIComponent(storyId)}`)
}

export async function createFusionStory(
  payload: CreateFusionStoryPayload,
): Promise<FusionStory> {
  return _req(`/fusion-stories`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function deleteFusionStory(storyId: string): Promise<void> {
  await _req(`/fusion-stories/${encodeURIComponent(storyId)}`, {
    method: 'DELETE',
  })
}

export async function iterateFusionOutline(
  storyId: string,
  payload: IterateOutlinePayload = {},
): Promise<FusionStory> {
  return _req(`/fusion-stories/${encodeURIComponent(storyId)}/iterate/outline`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function iterateFusionBeat(
  storyId: string,
  payload: IterateBeatPayload,
): Promise<FusionStory> {
  return _req(`/fusion-stories/${encodeURIComponent(storyId)}/iterate/beat`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function polishFusionStory(
  storyId: string,
): Promise<FusionStory> {
  return _req(`/fusion-stories/${encodeURIComponent(storyId)}/polish`, {
    method: 'POST',
  })
}

export async function adaptFusionStoryToArc(
  storyId: string,
  payload: FusionToArcDraftPayload = {},
): Promise<TemplateDraftPayload> {
  return _req(`/fusion-stories/${encodeURIComponent(storyId)}/adapt-to-arc`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function restoreFusionStoryVersion(
  storyId: string,
  versionNumber: number,
): Promise<FusionStory> {
  return _req(
    `/fusion-stories/${encodeURIComponent(storyId)}/versions/${versionNumber}/restore`,
    { method: 'POST' },
  )
}

export type FusionStoryExportFormat = 'markdown' | 'txt' | 'epub'

export interface FusionStoryExportFile {
  blob: Blob
  filename: string
}

export async function exportFusionStory(
  storyId: string,
  format: FusionStoryExportFormat,
): Promise<FusionStoryExportFile> {
  const res = await authedFetch(
    `${BASE}/fusion-stories/${encodeURIComponent(storyId)}/export?format=${format}`,
  )
  if (!res.ok) throw new Error(await readErrorResponse(res))
  const fallbackExt = format === 'markdown' ? 'md' : format
  return {
    blob: await res.blob(),
    filename:
      parseDispositionFilename(res.headers.get('content-disposition'))
      || `fusion-story.${fallbackExt}`,
  }
}

function parseDispositionFilename(header: string | null): string | null {
  if (!header) return null
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header)
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1])
    } catch {
      // fall through to the plain filename token
    }
  }
  const plain = /filename="([^"]+)"/i.exec(header)
  return plain?.[1] || null
}
