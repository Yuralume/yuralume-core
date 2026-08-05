import axios from 'axios'

import type { AlbumListResponse } from '@/types/album'
import type { Character } from '@/types/character'

export interface ListAlbumParams {
  limit?: number
  /** Keyset cursor — pass the previous page's `next_before` to page forward. */
  before?: string | null
}

export const ALBUM_PAGE_SIZE = 40

export async function listAlbum(
  characterId: string,
  params: ListAlbumParams = {},
): Promise<AlbumListResponse> {
  const res = await axios.get<AlbumListResponse>(
    `/api/v1/characters/${characterId}/album`,
    {
      params: {
        limit: params.limit ?? ALBUM_PAGE_SIZE,
        before: params.before ?? undefined,
      },
    },
  )
  return {
    ...res.data,
    has_more: res.data.has_more ?? false,
    next_before: res.data.next_before ?? null,
  }
}

/**
 * 把舞台上某張圖移進相簿 — 回傳更新過的角色（image_urls 少一項）。
 */
export async function transferStageToAlbum(
  characterId: string,
  url: string,
): Promise<Character> {
  const res = await axios.post<Character>(
    `/api/v1/characters/${characterId}/album/transfer`,
    { url },
  )
  return res.data
}

/**
 * 把相簿某張圖晉升回舞台（image_urls 末尾追加；舞台滿 12 張會 409）。
 */
export async function promoteAlbumToStage(itemId: string): Promise<Character> {
  const res = await axios.post<Character>(
    `/api/v1/album/${itemId}/promote`,
  )
  return res.data
}

export async function deleteAlbumItem(itemId: string): Promise<void> {
  await axios.delete(`/api/v1/album/${itemId}`)
}
