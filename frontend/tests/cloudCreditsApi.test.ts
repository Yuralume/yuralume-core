import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import { fetchCloudCredits } from '@/utils/api/cloudCredits'

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('fetchCloudCredits', () => {
  it('reads the badge snapshot', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      gift_cr: 800,
      purchased_cr: 4540,
      total_cr: 5340,
      stale: false,
    }))

    await expect(fetchCloudCredits()).resolves.toEqual({
      kind: 'ok',
      snapshot: {
        gift_cr: 800,
        purchased_cr: 4540,
        total_cr: 5340,
        stale: false,
      },
    })
    expect(mockedAuthedFetch).toHaveBeenCalledWith('/api/v1/cloud/credits')
  })

  it('asks the backend to bypass its cache on refresh', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      gift_cr: 0, purchased_cr: 10, total_cr: 10, stale: false,
    }))

    await fetchCloudCredits({ refresh: true })

    expect(mockedAuthedFetch).toHaveBeenCalledWith('/api/v1/cloud/credits?refresh=1')
  })

  it('reports 404 as unsupported — no ledger exists, hide the badge', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(404, { detail: 'nope' }))

    await expect(fetchCloudCredits()).resolves.toEqual({ kind: 'unsupported' })
  })

  it('reports 503 as degraded so the caller keeps its last-known-good value', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(503, { detail: 'nope' }))

    await expect(fetchCloudCredits()).resolves.toEqual({ kind: 'degraded' })
  })

  it('degrades rather than throwing when the request itself fails', async () => {
    // A balance read must never bubble an error into a player action.
    mockedAuthedFetch.mockRejectedValueOnce(new Error('offline'))

    await expect(fetchCloudCredits()).resolves.toEqual({ kind: 'degraded' })
  })

  it('coerces a malformed payload instead of rendering undefined', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, { total_cr: '12' }))

    await expect(fetchCloudCredits()).resolves.toEqual({
      kind: 'ok',
      snapshot: { gift_cr: 0, purchased_cr: 0, total_cr: 0, stale: false },
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
