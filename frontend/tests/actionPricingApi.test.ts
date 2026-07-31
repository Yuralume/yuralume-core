import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import { fetchCloudPricing } from '@/utils/api/cloudPricing'

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

describe('fetchCloudPricing', () => {
  it('reads the published price list', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      stale: false,
      tiers: [{
        tier_name: 'internal_test',
        billing_shape: 'action_fixed',
        actions: [
          { action_key: 'chat', unit: 'per_message', price_cr: 3, overage: false },
          { action_key: 'image_portrait', unit: 'per_portrait', price_cr: 12.5, overage: false },
        ],
      }],
    }))

    await expect(fetchCloudPricing()).resolves.toEqual({
      kind: 'ok',
      snapshot: {
        stale: false,
        tiers: [{
          tier_name: 'internal_test',
          billing_shape: 'action_fixed',
          actions: [
            { action_key: 'chat', unit: 'per_message', price_cr: 3, overage: false },
            { action_key: 'image_portrait', unit: 'per_portrait', price_cr: 12.5, overage: false },
          ],
        }],
      },
    })
    expect(mockedAuthedFetch).toHaveBeenCalledWith('/api/v1/cloud/pricing')
  })

  it('asks the proxy to bypass its cache on refresh', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, { tiers: [], stale: false }))

    await fetchCloudPricing({ refresh: true })

    expect(mockedAuthedFetch).toHaveBeenCalledWith('/api/v1/cloud/pricing?refresh=1')
  })

  it('reports 404 as unsupported — self-host has no price list', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(404, { detail: 'nope' }))

    await expect(fetchCloudPricing()).resolves.toEqual({ kind: 'unsupported' })
  })

  it('reports 503 as degraded so the caller keeps its last-known-good list', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(503, { detail: 'nope' }))

    await expect(fetchCloudPricing()).resolves.toEqual({ kind: 'degraded' })
  })

  it('degrades rather than throwing when the request itself fails', async () => {
    mockedAuthedFetch.mockRejectedValueOnce(new Error('offline'))

    await expect(fetchCloudPricing()).resolves.toEqual({ kind: 'degraded' })
  })

  it('drops rows that cannot be quoted honestly', async () => {
    // A zero / negative / unparseable price must never reach the UI: the
    // player would read "free" or "0 螢火" where the truth is "unknown".
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      stale: true,
      tiers: [
        { tier_name: '', billing_shape: 'action_fixed', actions: [] },
        {
          tier_name: 'standard',
          billing_shape: 'action_fixed',
          actions: [
            { action_key: 'chat', unit: 'per_message', price_cr: 0, overage: false },
            { action_key: 'image_portrait', unit: 'per_portrait', price_cr: -1, overage: false },
            { action_key: '', unit: 'per_message', price_cr: 4, overage: false },
            { action_key: 'image_chat_tool', unit: 'per_image', price_cr: '7', overage: 'yes' },
            { action_key: 'character_draft', unit: 'per_draft', price_cr: 5, overage: false },
          ],
        },
      ],
    }))

    await expect(fetchCloudPricing()).resolves.toEqual({
      kind: 'ok',
      snapshot: {
        stale: true,
        tiers: [{
          tier_name: 'standard',
          billing_shape: 'action_fixed',
          actions: [
            { action_key: 'character_draft', unit: 'per_draft', price_cr: 5, overage: false },
          ],
        }],
      },
    })
  })
})
