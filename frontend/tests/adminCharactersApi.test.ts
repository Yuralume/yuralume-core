import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  freezeCharacter,
  getCharacterFreezeOverview,
  unfreezeCharacter,
  type AdminCharacterOverview,
} from '@/utils/api/adminCharacters'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    put: vi.fn(),
    post: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
})

function overview(): AdminCharacterOverview {
  return {
    characters: [
      {
        id: 'char-1',
        name: 'Aria',
        owner_user_id: 'user-1',
        frozen: false,
        frozen_at: null,
        frozen_reason: null,
        last_active_at: '2026-06-01T00:00:00Z',
        created_at: '2026-01-01T00:00:00Z',
        proactive_enabled: true,
      },
      {
        id: 'char-2',
        name: 'Nyx',
        owner_user_id: 'user-2',
        frozen: true,
        frozen_at: '2026-06-15T00:00:00Z',
        frozen_reason: 'manual',
        last_active_at: null,
        created_at: '2026-02-01T00:00:00Z',
        proactive_enabled: false,
      },
    ],
    total: 2,
  }
}

describe('admin characters freeze API', () => {
  it('loads the site-wide character freeze overview', async () => {
    const response = overview()
    mockedAxios.get.mockResolvedValueOnce({ data: response })

    await expect(getCharacterFreezeOverview()).resolves.toEqual(response)

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/admin/characters/overview',
    )
  })

  it('freezes a character by id', async () => {
    const result = { id: 'char-1', frozen: true, frozen_at: '2026-07-08T00:00:00Z' }
    mockedAxios.post.mockResolvedValueOnce({ data: result })

    await expect(freezeCharacter('char-1')).resolves.toEqual(result)

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/admin/characters/char-1/freeze',
    )
  })

  it('unfreezes a character by id', async () => {
    const result = { id: 'char-2', frozen: false, frozen_at: null }
    mockedAxios.post.mockResolvedValueOnce({ data: result })

    await expect(unfreezeCharacter('char-2')).resolves.toEqual(result)

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/admin/characters/char-2/unfreeze',
    )
  })
})
