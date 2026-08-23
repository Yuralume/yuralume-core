import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import { fetchRuntimeLimits } from '@/utils/api/cloudLimits'

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

const UNLIMITED = {
  character_slots: null,
  character_daily_creates: null,
  story_scenes_daily: null,
  session_message_limit: null,
  album_generation_enabled: true,
  tts_enabled: true,
  video_generation_enabled: true,
  background: null,
}

describe('fetchRuntimeLimits', () => {
  it('reads the hosted limits snapshot', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      character_slots: { used: 2, limit: 3 },
      character_daily_creates: { used: 0, limit: 1 },
      story_scenes_daily: { used: 1, limit: 5 },
      session_message_limit: 80,
      album_generation_enabled: true,
      tts_enabled: true,
      video_generation_enabled: false,
      background: { dormancy_days: 14 },
    }))

    await expect(fetchRuntimeLimits()).resolves.toEqual({
      kind: 'ok',
      snapshot: {
        character_slots: { used: 2, limit: 3 },
        character_daily_creates: { used: 0, limit: 1 },
        story_scenes_daily: { used: 1, limit: 5 },
        session_message_limit: 80,
        album_generation_enabled: true,
        tts_enabled: true,
        video_generation_enabled: false,
        background: { dormancyDays: 14 },
      },
    })
    expect(mockedAuthedFetch).toHaveBeenCalledWith('/api/v1/cloud/runtime-limits')
  })

  it('reports 404 as unsupported — self-host has no hosted caps', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(404, { detail: 'nope' }))

    await expect(fetchRuntimeLimits()).resolves.toEqual({ kind: 'unsupported' })
  })

  it('reports 503 as degraded so the caller claims nothing', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(503, { detail: 'nope' }))

    await expect(fetchRuntimeLimits()).resolves.toEqual({ kind: 'degraded' })
  })

  it('degrades rather than throwing when the request itself fails', async () => {
    mockedAuthedFetch.mockRejectedValueOnce(new Error('offline'))

    await expect(fetchRuntimeLimits()).resolves.toEqual({ kind: 'degraded' })
  })

  it('keeps the ceiling when the backend could not count the usage', async () => {
    // The documented fail-soft: `used` may be null while `limit` is real.
    // Coercing it to 0 would read as "plenty left"; dropping the whole entry
    // would hide a ceiling the player is about to hit.
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      ...UNLIMITED,
      character_slots: { used: null, limit: 3 },
    }))

    await expect(fetchRuntimeLimits()).resolves.toEqual({
      kind: 'ok',
      snapshot: { ...UNLIMITED, character_slots: { used: null, limit: 3 } },
    })
  })

  it('treats a nonsense ceiling as no ceiling at all', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      character_slots: { used: 2, limit: '3' },
      character_daily_creates: { used: 2 },
      story_scenes_daily: 5,
      session_message_limit: 0,
    }))

    // Everything unparseable collapses to "nothing to say", never to a
    // fabricated number a surface would then render.
    await expect(fetchRuntimeLimits()).resolves.toEqual({
      kind: 'ok',
      snapshot: UNLIMITED,
    })
  })

  it('leaves every feature switch on when the payload omits them', async () => {
    // Unknown must never present as "this feature is not yours".
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {}))

    await expect(fetchRuntimeLimits()).resolves.toEqual({
      kind: 'ok',
      snapshot: UNLIMITED,
    })
  })

  describe('background (NF4/NF5)', () => {
    it('reads a real dormancy window', async () => {
      mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
        ...UNLIMITED,
        background: { dormancy_days: 7 },
      }))

      await expect(fetchRuntimeLimits()).resolves.toEqual({
        kind: 'ok',
        snapshot: { ...UNLIMITED, background: { dormancyDays: 7 } },
      })
    })

    it('reports "never dormant" as a real object with a null day count', async () => {
      // A tier with no dormancy still answers with an object — `null` here
      // is a stated fact ("this tier never sleeps"), not an absent field.
      mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
        ...UNLIMITED,
        background: { dormancy_days: null },
      }))

      await expect(fetchRuntimeLimits()).resolves.toEqual({
        kind: 'ok',
        snapshot: { ...UNLIMITED, background: { dormancyDays: null } },
      })
    })

    it('collapses a missing background object to null rather than guessing', async () => {
      mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
        character_slots: null,
        character_daily_creates: null,
        story_scenes_daily: null,
        session_message_limit: null,
        album_generation_enabled: true,
        tts_enabled: true,
        video_generation_enabled: true,
        // `background` deliberately omitted — an older backend body.
      }))

      await expect(fetchRuntimeLimits()).resolves.toEqual({
        kind: 'ok',
        snapshot: UNLIMITED,
      })
    })

    it('treats a nonsense day count the same as no ceiling at all', async () => {
      mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
        ...UNLIMITED,
        background: { dormancy_days: '7' },
      }))

      await expect(fetchRuntimeLimits()).resolves.toEqual({
        kind: 'ok',
        snapshot: { ...UNLIMITED, background: { dormancyDays: null } },
      })
    })

    it('treats a malformed background field as absent', async () => {
      mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
        ...UNLIMITED,
        background: 'not an object',
      }))

      await expect(fetchRuntimeLimits()).resolves.toEqual({
        kind: 'ok',
        snapshot: UNLIMITED,
      })
    })
  })
})

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    json: async () => body,
  } as Response
}
