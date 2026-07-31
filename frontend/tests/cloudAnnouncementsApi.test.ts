import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import { fetchCloudAnnouncements } from '@/utils/api/cloudAnnouncements'

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('fetchCloudAnnouncements', () => {
  it('reads the unread standing behind the dot', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      has_unread: true,
      unread_count: 2,
      latest_published_at: '2026-07-29T00:00:00Z',
      stale: false,
    }))

    await expect(fetchCloudAnnouncements()).resolves.toEqual({
      kind: 'ok',
      snapshot: {
        has_unread: true,
        unread_count: 2,
        latest_published_at: '2026-07-29T00:00:00Z',
        stale: false,
      },
    })
    expect(mockedAuthedFetch).toHaveBeenCalledWith('/api/v1/cloud/announcements/unread')
  })

  it('asks for a fresh read when the player comes back', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      has_unread: false,
      unread_count: 0,
      latest_published_at: null,
      stale: false,
    }))

    await fetchCloudAnnouncements({ refresh: true })

    expect(mockedAuthedFetch).toHaveBeenCalledWith(
      '/api/v1/cloud/announcements/unread?refresh=1',
    )
  })

  it.each([['not_cloud_mode'], ['no_cloud_tenant']])(
    'treats a 404 naming %s as "no board here" so the dot hides for good',
    async (reason) => {
      mockedAuthedFetch.mockResolvedValueOnce(
        jsonResponse(404, { detail: { reason, message: 'nope' } }),
      )

      await expect(fetchCloudAnnouncements()).resolves.toEqual({ kind: 'unsupported' })
    },
  )

  it('treats a bare 404 as retryable, not as a missing board', async () => {
    // During a rolling deploy a new SPA reaches an older replica that does not
    // serve this route yet. Reading that as permanent would switch the dot off
    // for the rest of the session over a transient topology fact.
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(404, { detail: 'Not Found' }))

    await expect(fetchCloudAnnouncements()).resolves.toEqual({ kind: 'degraded' })
  })

  it('treats a 404 with an unrecognised reason as retryable', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(
      jsonResponse(404, { detail: { reason: 'something_new' } }),
    )

    await expect(fetchCloudAnnouncements()).resolves.toEqual({ kind: 'degraded' })
  })

  it('treats an outage as unknown, not as "nothing new"', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(503, {}))

    // 'degraded' keeps whatever was last known on screen; a cleared dot would
    // hide a notice the operator was required to publish.
    await expect(fetchCloudAnnouncements()).resolves.toEqual({ kind: 'degraded' })
  })

  it('treats a transport failure the same way', async () => {
    mockedAuthedFetch.mockRejectedValueOnce(new Error('offline'))

    await expect(fetchCloudAnnouncements()).resolves.toEqual({ kind: 'degraded' })
  })

  it('treats an unreadable body the same way', async () => {
    mockedAuthedFetch.mockResolvedValueOnce({
      status: 200,
      ok: true,
      json: async () => {
        throw new Error('not json')
      },
    } as unknown as Response)

    await expect(fetchCloudAnnouncements()).resolves.toEqual({ kind: 'degraded' })
  })

  it.each([
    ['a missing flag', { unread_count: 0 }],
    ['a non-boolean flag', { has_unread: 'yes', unread_count: 1 }],
    ['a missing count', { has_unread: true }],
    ['a negative count', { has_unread: true, unread_count: -3 }],
    ['a non-integer count', { has_unread: true, unread_count: 1.5 }],
    ['a non-string timestamp', { has_unread: true, unread_count: 1, latest_published_at: 42 }],
    ['a non-object body', ['nope']],
  ])('refuses %s rather than inventing a cleared dot', async (_label, body) => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, body))

    // Coercing junk into `has_unread: false` would look like a successful read,
    // get stored as current, and silently turn off a notice the operator was
    // required to give. A stuck-on dot merely costs a glance.
    await expect(fetchCloudAnnouncements()).resolves.toEqual({ kind: 'degraded' })
  })

  it('refuses a flag that contradicts its own count', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(200, {
      has_unread: false,
      unread_count: 3,
      latest_published_at: null,
    }))

    // Both fields state the same fact; disagreement means this is not one
    // coherent answer, so neither half can be trusted.
    await expect(fetchCloudAnnouncements()).resolves.toEqual({ kind: 'degraded' })
  })
})

function jsonResponse(status: number, body: unknown): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  } as unknown as Response
}
