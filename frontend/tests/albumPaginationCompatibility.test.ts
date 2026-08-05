import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'

import { ALBUM_PAGE_SIZE, listAlbum } from '@/utils/api/album'

vi.mock('axios', () => ({
  default: { get: vi.fn() },
}))

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('album pagination rolling compatibility', () => {
  it('sends an explicit first-page limit', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: { items: [], total: 0, has_more: false, next_before: null },
    })

    await listAlbum('char-1')

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/char-1/album',
      { params: { limit: ALBUM_PAGE_SIZE, before: undefined } },
    )
  })

  it('normalizes the additive fields missing from a pre-pagination backend', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: { items: [], total: 0 } })

    await expect(listAlbum('char-1')).resolves.toEqual({
      items: [], total: 0, has_more: false, next_before: null,
    })
  })
})
