import axios from 'axios'

export interface PendingFollowUpMessage {
  content: string
  queued_at: string
}

export interface PendingFollowUp {
  id: string
  character_id: string
  conversation_id: string
  status: 'queued' | 'resolving' | 'resolved' | 'cancelled'
  brief_reply: string
  defer_reason: string
  scheduled_for: string
  queued_at: string
  updated_at: string
  resolved_at: string | null
  last_error: string | null
  messages: PendingFollowUpMessage[]
}

export interface PendingFollowUpTickResult {
  resolved: number
}

const BASE = '/api/v1'

export async function listOpenPendingFollowUps(
  characterId: string,
): Promise<PendingFollowUp[]> {
  const { data } = await axios.get<PendingFollowUp[]>(
    `${BASE}/characters/${characterId}/pending-follow-ups`,
  )
  return data
}

export async function listDuePendingFollowUps(): Promise<PendingFollowUp[]> {
  const { data } = await axios.get<PendingFollowUp[]>(
    `${BASE}/admin/pending-follow-ups`,
  )
  return data
}

export async function triggerPendingFollowUpTick(): Promise<PendingFollowUpTickResult> {
  const { data } = await axios.post<PendingFollowUpTickResult>(
    `${BASE}/admin/pending-follow-ups/tick`,
  )
  return data
}
