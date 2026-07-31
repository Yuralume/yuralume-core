import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import {
  OverageConsentError,
  fetchOverageSettings,
  updateOverageSettings,
} from '@/utils/api/overageSettings'

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('fetchOverageSettings', () => {
  it('reads both switches plus the tier context that bounds them', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      chat_image_enabled: true,
      feed_post_enabled: false,
      tier_overage_enabled: true,
      daily_limit: 5,
      chat_image_price_cr: 8,
      feed_post_price_cr: 6,
    }))

    await expect(fetchOverageSettings()).resolves.toEqual({
      kind: 'ok',
      settings: {
        chat_image_enabled: true,
        feed_post_enabled: false,
        tier_overage_enabled: true,
        daily_limit: 5,
        chat_image_price_cr: 8,
        feed_post_price_cr: 6,
      },
    })
    expect(mockedAuthedFetch).toHaveBeenCalledWith('/api/v1/operator/overage-settings')
  })

  it('never reads a missing or zero price as free', async () => {
    // A price of 0 would render as "free" on a switch that spends money.
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      tier_overage_enabled: true,
      daily_limit: 5,
      chat_image_price_cr: 0,
    }))

    const result = await fetchOverageSettings()

    expect(result).toMatchObject({
      kind: 'ok',
      settings: { chat_image_price_cr: null, feed_post_price_cr: null },
    })
  })

  it('reports 404 as unsupported — self-host has no wallet to spend from', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(404, { detail: 'nope' }))

    await expect(fetchOverageSettings()).resolves.toEqual({ kind: 'unsupported' })
  })

  it('reports 503 and transport failures as degraded', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(503, {}))
    await expect(fetchOverageSettings()).resolves.toEqual({ kind: 'degraded' })

    mockedAuthedFetch.mockRejectedValueOnce(new Error('offline'))
    await expect(fetchOverageSettings()).resolves.toEqual({ kind: 'degraded' })
  })

  it('treats a missing switch as off — consent is never inferred', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {}))

    await expect(fetchOverageSettings()).resolves.toEqual({
      kind: 'ok',
      settings: {
        chat_image_enabled: false,
        feed_post_enabled: false,
        tier_overage_enabled: false,
        daily_limit: 0,
        chat_image_price_cr: null,
        feed_post_price_cr: null,
      },
    })
  })
})

describe('updateOverageSettings', () => {
  it('sends only the switch that moved', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      chat_image_enabled: false,
      feed_post_enabled: true,
      tier_overage_enabled: true,
      daily_limit: 5,
    }))

    const settings = await updateOverageSettings({ feed_post_enabled: true })

    expect(settings.feed_post_enabled).toBe(true)
    const [, init] = mockedAuthedFetch.mock.calls[0]
    expect(init?.method).toBe('PUT')
    expect(JSON.parse(String(init?.body))).toEqual({ feed_post_enabled: true })
  })

  it('throws so the caller can roll the toggle back', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(503, {}))

    await expect(updateOverageSettings({ chat_image_enabled: true }))
      .rejects.toThrow()
  })

  it('surfaces a refusal to arm as a reason the pane can explain', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(400, {
      detail: {
        code: 'overage_consent_rejected',
        reason: 'price_unavailable',
        item: 'chat_image',
        message: 'no price',
      },
    }))

    const error = await updateOverageSettings({ chat_image_enabled: true })
      .catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(OverageConsentError)
    expect((error as OverageConsentError).reason).toBe('price_unavailable')
    expect((error as OverageConsentError).item).toBe('chat_image')
  })

  it('still throws a refusal when the body says nothing useful', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(400, { detail: 'nope' }))

    const error = await updateOverageSettings({ feed_post_enabled: true })
      .catch((thrown: unknown) => thrown)

    expect(error).toBeInstanceOf(OverageConsentError)
    expect((error as OverageConsentError).reason).toBe('unknown')
  })
})
