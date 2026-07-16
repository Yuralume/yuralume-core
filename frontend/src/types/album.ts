/**
 * 角色相簿 — 長期圖片收藏（工具自動收集 + 從舞台轉移）。
 * 跟 `Character.image_urls`（舞台輪播、上限 12 張）分開管理。
 */

export type AlbumSource = 'tool' | 'stage' | 'upload'

export interface AlbumItem {
  id: string
  character_id: string
  url: string
  source: AlbumSource
  caption: string | null
  byte_size: number | null
  created_at: string
}

export interface AlbumListResponse {
  items: AlbumItem[]
  total: number
}
