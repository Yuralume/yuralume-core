import type {
  CreateFusionStoryPayload,
  FusionStory,
  FusionToArcDraftPayload,
  FusionStorySummary,
  IterateBeatPayload,
  IterateOutlinePayload,
  PolishFusionStoryPayload,
  QuotedActionPrice,
} from '@/types/fusionStory'
import type { TemplateDraftPayload } from '@/types/arcTemplateIntake'
import {
  ACTION_FUSION_STORY,
  ACTION_FUSION_STORY_ITERATE,
  useActionPricing,
} from '@/composables/useActionPricing'
import { authedFetch } from '@/utils/authedFetch'
import { readErrorResponse } from '@/utils/api/httpError'
import { apiErrorFromResponse } from '@/utils/api/insufficientCredits'

const BASE = '/api/v1'

/**
 * Attach the fixed price this client is quoting for a fusion action (plan R9).
 *
 * Same contract as the chat transport's `withQuotedPrices`: the server binds
 * the charge to the number that was on the player's screen and refuses with
 * `409 price_changed` if the published price moved, rather than silently
 * charging an amount nobody was shown. Dropped entirely when nothing is
 * quotable (self-host, a token-billed tier, a price list that has not loaded),
 * in which case the server falls back to its own cache exactly as before.
 *
 * Applied on the transport rather than at each call site so every entry point
 * into a fusion generation carries the same binding.
 */
function withQuotedPrice<T extends QuotedActionPrice>(
  payload: T,
  actionKey: string,
): T {
  const price = useActionPricing().priceOf(actionKey)
  if (price === null) return payload
  return { ...payload, quoted_price_cr: price.price_cr }
}

async function _req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await authedFetch(`${BASE}${path}`, {
    headers: init.body
      ? { 'Content-Type': 'application/json', ...(init.headers || {}) }
      : init.headers,
    ...init,
  })
  // Adapt-to-arc (and the iterate / polish generators) can hit the hosted
  // credit ceiling; the shared reader turns that into the typed error.
  if (!res.ok) throw await apiErrorFromResponse(res)
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
    body: JSON.stringify(withQuotedPrice(payload, ACTION_FUSION_STORY)),
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
    body: JSON.stringify(
      withQuotedPrice(payload, ACTION_FUSION_STORY_ITERATE),
    ),
  })
}

export async function iterateFusionBeat(
  storyId: string,
  payload: IterateBeatPayload,
): Promise<FusionStory> {
  return _req(`/fusion-stories/${encodeURIComponent(storyId)}/iterate/beat`, {
    method: 'POST',
    body: JSON.stringify(
      withQuotedPrice(payload, ACTION_FUSION_STORY_ITERATE),
    ),
  })
}

export async function polishFusionStory(
  storyId: string,
  payload: PolishFusionStoryPayload = {},
): Promise<FusionStory> {
  return _req(`/fusion-stories/${encodeURIComponent(storyId)}/polish`, {
    method: 'POST',
    body: JSON.stringify(
      withQuotedPrice(payload, ACTION_FUSION_STORY_ITERATE),
    ),
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
