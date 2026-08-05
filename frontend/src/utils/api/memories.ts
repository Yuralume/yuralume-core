import axios from 'axios'

export interface Memory {
  id: string
  character_id: string
  conversation_id: string | null
  kind: string
  content: string
  salience: number
  tags: string[]
  created_at: string
  last_accessed_at: string | null
  access_count: number
  has_embedding: boolean
}

export interface MemoryScored {
  item: Memory
  similarity: number
}

export interface MemoryUpdate {
  content?: string
  salience?: number
  tags?: string[]
}

/**
 * 一頁記憶 —— 與動態牆同一套 keyset 形狀（limit + before）。
 *
 * 這支端點以前回的是整個陣列：`?limit=` 沒有被後端宣告，FastAPI 靜默
 * 丟掉，於是累積久的角色一次吐 1567 筆、要等兩秒。沒有 `page` /
 * `page_size`——整個 codebase 都不用 offset 分頁。
 */
export interface MemoryPage {
  items: Memory[]
  has_more: boolean
  /** 本頁最舊一筆的 created_at；下一頁把它當 `before` 傳回。 */
  next_before: string | null
}

export interface ListMemoriesOptions {
  kind?: string
  limit?: number
  before?: string | null
}

export const MEMORY_PAGE_SIZE = 50

export async function listMemories(
  characterId: string,
  options: ListMemoriesOptions = {},
): Promise<MemoryPage> {
  const params: Record<string, string | number> = {}
  if (options.kind) params.kind = options.kind
  params.limit = options.limit ?? MEMORY_PAGE_SIZE
  if (options.before) params.before = options.before
  const { data } = await axios.get<MemoryPage | Memory[]>(
    `/api/v1/characters/${characterId}/memories`,
    { params },
  )
  if (Array.isArray(data)) {
    return { items: data, has_more: false, next_before: null }
  }
  return data
}

export async function searchMemories(
  characterId: string,
  query: string,
  topK: number = 8,
): Promise<MemoryScored[]> {
  const { data } = await axios.post<MemoryScored[]>(
    `/api/v1/characters/${characterId}/memories/search`,
    { query, top_k: topK },
  )
  return data
}

export async function updateMemory(
  memoryId: string,
  req: MemoryUpdate,
): Promise<Memory> {
  const { data } = await axios.patch<Memory>(
    `/api/v1/memories/${memoryId}`,
    req,
  )
  return data
}

export async function deleteMemory(memoryId: string): Promise<void> {
  await axios.delete(`/api/v1/memories/${memoryId}`)
}
