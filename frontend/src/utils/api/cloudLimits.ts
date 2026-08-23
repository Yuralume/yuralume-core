/**
 * Hosted runtime limits read — cloud mode only, display-only.
 *
 * The hosted account profile (`AccountRuntimeProfile`) caps how much a player
 * may create: character slots, creations per rolling 24h, scenes per rolling
 * 24h, messages per session, plus a few feature switches. The server has
 * always enforced these; this read exists so the surfaces can *say so before*
 * the player presses the button instead of after.
 *
 * Same two failure modes as `cloudCredits`, kept distinguishable for the same
 * reason — the caller reacts differently to each:
 *
 *   - `404` — self-host (the route is not mounted at all), or a cloud operator
 *     with no projected tenant. There are no hosted limits, so every hint
 *     stays unrendered forever.
 *   - anything else — momentarily unknown. Nothing is claimed; an advisory
 *     that cannot be substantiated is simply not shown.
 *
 * Nothing here writes. A limit hint that mutated state would be a worse bug
 * than the missing hint it replaces.
 */

import { authedFetch } from '@/utils/authedFetch'

/**
 * One "N of M" counter.
 *
 * `used` is nullable on purpose: the backend serves the limit even when the
 * usage count could not be read, so the player still learns the ceiling
 * exists. A fabricated `0` would read as "plenty left" and is worse than no
 * number at all.
 */
export interface RuntimeLimitUsage {
  used: number | null
  limit: number
}

/**
 * How background scheduling behaves for this account when the player is
 * away (NF4/NF5). Not a ceiling like the fields above — it is the one entry
 * on this route that exists to be said out loud *before* it is noticed as an
 * unexplained silence, not to gate a button.
 */
export interface BackgroundPolicy {
  /**
   * Days without a foreground interaction after which characters stop
   * scheduling background work (proactive messages, schedule, world
   * events). `null` = this tier never goes dormant.
   */
  dormancyDays: number | null
}

export interface RuntimeLimitsSnapshot {
  /** Concurrent characters. `null` = uncapped. */
  character_slots: RuntimeLimitUsage | null
  /** Character creations in a rolling 24h window. `null` = uncapped. */
  character_daily_creates: RuntimeLimitUsage | null
  /** Story scenes opened in a rolling 24h window. `null` = uncapped. */
  story_scenes_daily: RuntimeLimitUsage | null
  /** Messages kept per chat session, or `null` when unbounded. */
  session_message_limit: number | null
  album_generation_enabled: boolean
  tts_enabled: boolean
  video_generation_enabled: boolean
  /**
   * `null` when the payload did not include a `background` object at all
   * (an older backend, or a malformed body) — never fabricated, so a
   * consumer that only checks `dormancyDays` still can't render on a guess.
   */
  background: BackgroundPolicy | null
}

export type RuntimeLimitsResult =
  | { kind: 'ok'; snapshot: RuntimeLimitsSnapshot }
  /** No hosted profile for this deployment / operator — never hint again. */
  | { kind: 'unsupported' }
  /** Temporarily unknown — say nothing rather than guess. */
  | { kind: 'degraded' }

const ENDPOINT = '/api/v1/cloud/runtime-limits'

export async function fetchRuntimeLimits(): Promise<RuntimeLimitsResult> {
  let res: Response
  try {
    res = await authedFetch(ENDPOINT)
  } catch {
    return { kind: 'degraded' }
  }
  if (res.status === 404) return { kind: 'unsupported' }
  if (!res.ok) return { kind: 'degraded' }
  try {
    return { kind: 'ok', snapshot: normalize(await res.json()) }
  } catch {
    return { kind: 'degraded' }
  }
}

function normalize(payload: unknown): RuntimeLimitsSnapshot {
  const body = (payload ?? {}) as Record<string, unknown>
  return {
    character_slots: usageField(body.character_slots),
    character_daily_creates: usageField(body.character_daily_creates),
    story_scenes_daily: usageField(body.story_scenes_daily),
    session_message_limit: positiveField(body.session_message_limit),
    album_generation_enabled: enabledField(body.album_generation_enabled),
    tts_enabled: enabledField(body.tts_enabled),
    video_generation_enabled: enabledField(body.video_generation_enabled),
    background: backgroundField(body.background),
  }
}

/**
 * A counter is only a counter when the ceiling is a real, non-negative
 * number. Anything else (absent, `null`, a string, a negative) means "no
 * limit to talk about" and collapses to `null` so no surface renders it.
 */
function usageField(value: unknown): RuntimeLimitUsage | null {
  if (!value || typeof value !== 'object') return null
  const entry = value as Record<string, unknown>
  const limit = entry.limit
  if (typeof limit !== 'number' || !Number.isFinite(limit) || limit < 0) {
    return null
  }
  const used = entry.used
  return {
    used: typeof used === 'number' && Number.isFinite(used) && used >= 0
      ? used
      : null,
    limit,
  }
}

function positiveField(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? value
    : null
}

/**
 * Tolerant like every other field on this route: a missing or malformed
 * `background` object (self-host on an older bundle, an unexpected shape)
 * collapses to `null` rather than throwing, so the advisory simply never
 * renders instead of crashing the read.
 */
function backgroundField(value: unknown): BackgroundPolicy | null {
  if (!value || typeof value !== 'object') return null
  const entry = value as Record<string, unknown>
  return { dormancyDays: positiveField(entry.dormancy_days) }
}

/**
 * Feature switches fail *open*: only an explicit `false` disables a feature.
 * A missing field means the backend does not model that switch, and telling
 * a player their album is off because a payload changed shape would remove a
 * feature they are entitled to. The server still refuses what it must.
 */
function enabledField(value: unknown): boolean {
  return value !== false
}
