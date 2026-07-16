import axios from 'axios'

export type MemoirEntryKind = 'memory' | 'emotion' | 'milestone'

export interface MemoirChapter {
  period: 'week' | 'month'
  period_start: string
  period_end: string
  narrative: string
  dominant_themes: string[]
  evidence_quotes: string[]
}

export interface MemoirEntry {
  kind: MemoirEntryKind
  entry_id: string
  occurred_at: string
  summary: string
  score: number
  pinned: boolean
  extras: Record<string, string>
}

export interface MemoirView {
  chapters: MemoirChapter[]
  timeline: MemoirEntry[]
  pin_count: number
  pin_limit: number
}

export async function getMemoirView(characterId: string): Promise<MemoirView> {
  const { data } = await axios.get<MemoirView>(
    `/api/v1/characters/${characterId}/memoir`,
  )
  return data
}

export async function pinMemoirEntry(
  characterId: string,
  entryKind: MemoirEntryKind,
  entryId: string,
): Promise<void> {
  await axios.post(`/api/v1/characters/${characterId}/memoir/pin`, {
    entry_kind: entryKind,
    entry_id: entryId,
  })
}

export async function unpinMemoirEntry(
  characterId: string,
  entryKind: MemoirEntryKind,
  entryId: string,
): Promise<void> {
  await axios.delete(
    `/api/v1/characters/${characterId}/memoir/pin/${entryKind}/${encodeURIComponent(entryId)}`,
  )
}

export interface PinLimitExceeded {
  code: 'pin_limit_exceeded'
  current: number
  limit: number
}

export function isPinLimitExceededError(
  err: unknown,
): err is { response: { status: 409; data: { detail: PinLimitExceeded } } } {
  return (
    axios.isAxiosError(err)
    && err.response?.status === 409
    && err.response?.data?.detail?.code === 'pin_limit_exceeded'
  )
}
