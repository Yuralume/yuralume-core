import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  listMemories,
  type MemoryPage,
} from '@/utils/api/memories'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
})

function page(overrides: Partial<MemoryPage> = {}): MemoryPage {
  return {
    items: [
      {
        id: 'm-1',
        character_id: 'char-1',
        conversation_id: null,
        kind: 'semantic',
        content: '記得下雨那天',
        salience: 0.5,
        tags: ['weather'],
        created_at: '2026-07-01T12:00:00Z',
        last_accessed_at: null,
        access_count: 0,
        has_embedding: true,
      },
    ],
    has_more: false,
    next_before: null,
    ...overrides,
  }
}

describe('memories list API', () => {
  // 這支端點以前只宣告 kind，`?limit=` 被 FastAPI 靜默丟掉，累積久的
  // 角色一次回 1567 筆。參數有沒有真的送出去是這裡唯一值得釘的事。
  it('sends limit so the request is actually bounded', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: page() })

    await listMemories('char-1', { limit: 50 })

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/char-1/memories',
      { params: { limit: 50 } },
    )
  })

  it('carries the keyset cursor and the kind filter together', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: page() })

    await listMemories('char-1', {
      kind: 'semantic',
      limit: 50,
      before: '2026-07-01T12:00:00Z',
    })

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/char-1/memories',
      {
        params: {
          kind: 'semantic',
          limit: 50,
          before: '2026-07-01T12:00:00Z',
        },
      },
    )
  })

  it('omits an absent cursor rather than sending null', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: page() })

    await listMemories('char-1', { limit: 50, before: null })

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/char-1/memories',
      { params: { limit: 50 } },
    )
  })

  it('returns the page envelope, not a bare array', async () => {
    const body = page({ has_more: true, next_before: '2026-07-01T12:00:00Z' })
    mockedAxios.get.mockResolvedValueOnce({ data: body })

    await expect(listMemories('char-1')).resolves.toEqual(body)
  })

  it('normalizes a pre-pagination backend array during rolling deploys', async () => {
    const body = page().items
    mockedAxios.get.mockResolvedValueOnce({ data: body })

    await expect(listMemories('char-1')).resolves.toEqual({
      items: body,
      has_more: false,
      next_before: null,
    })
  })
})
