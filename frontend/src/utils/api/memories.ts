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

export async function listMemories(
  characterId: string,
  options: { kind?: string } = {},
): Promise<Memory[]> {
  const params: Record<string, string> = {}
  if (options.kind) params.kind = options.kind
  const { data } = await axios.get<Memory[]>(
    `/api/v1/characters/${characterId}/memories`,
    { params },
  )
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
