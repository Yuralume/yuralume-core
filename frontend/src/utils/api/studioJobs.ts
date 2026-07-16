import { authedFetch } from '@/utils/authedFetch'
import { readErrorResponse } from '@/utils/api/httpError'

const BASE = '/api/v1'

export interface ActiveStudioJobs {
  running: number
}

export async function getActiveStudioJobs(): Promise<ActiveStudioJobs> {
  const res = await authedFetch(`${BASE}/studio/jobs/active`)
  if (!res.ok) throw new Error(await readErrorResponse(res))
  return (await res.json()) as ActiveStudioJobs
}

/**
 * Fusion material-richness (Creator Studio C1-P1). Deterministic
 * bookkeeping over the salience-ranked memory slice a fusion story would
 * pull, used to badge each character in the picker and to nudge chatting
 * more when material is thin. Ownership-scoped server-side.
 */
export type FusionMaterialTier = 'rich' | 'ok' | 'sparse'

export interface FusionMaterialStat {
  character_id: string
  memory_count: number
  total_chars: number
  tier: FusionMaterialTier
}

interface FusionMaterialStatsResponse {
  stats: FusionMaterialStat[]
}

/** Server caps a single request at 20 ids; batch to stay under it. */
const MATERIAL_STATS_BATCH = 20

/**
 * Fetch richness stats for many characters, batching to the server's
 * per-request id cap. Returns a map keyed by character id (ids the caller
 * doesn't own are simply absent).
 */
export async function fetchFusionMaterialStats(
  characterIds: string[],
): Promise<Record<string, FusionMaterialStat>> {
  const out: Record<string, FusionMaterialStat> = {}
  for (let i = 0; i < characterIds.length; i += MATERIAL_STATS_BATCH) {
    const batch = characterIds.slice(i, i + MATERIAL_STATS_BATCH)
    if (!batch.length) continue
    const query = encodeURIComponent(batch.join(','))
    const res = await authedFetch(
      `${BASE}/studio/fusion-material-stats?character_ids=${query}`,
    )
    if (!res.ok) throw new Error(await readErrorResponse(res))
    const body = (await res.json()) as FusionMaterialStatsResponse
    for (const stat of body.stats) out[stat.character_id] = stat
  }
  return out
}
