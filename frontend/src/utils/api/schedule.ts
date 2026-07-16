import type {
  CurrentActivitySnapshot,
  DailySchedule,
} from '@/types/schedule'
import { authedFetch } from '@/utils/authedFetch'
import { readErrorResponse } from '@/utils/api/httpError'

const BASE = '/api/v1'

export async function getSchedule(
  characterId: string,
  date?: string,
): Promise<DailySchedule | null> {
  const params = date ? `?date=${encodeURIComponent(date)}` : ''
  const res = await authedFetch(`${BASE}/characters/${characterId}/schedule${params}`)
  if (res.status === 404) return null
  if (!res.ok) {
    throw new Error(`Failed to load schedule: ${res.status}`)
  }
  return await res.json()
}

export async function regenerateSchedule(
  characterId: string,
  date?: string,
): Promise<DailySchedule> {
  const params = date ? `?date=${encodeURIComponent(date)}` : ''
  const res = await authedFetch(
    `${BASE}/characters/${characterId}/schedule/regenerate${params}`,
    { method: 'POST' },
  )
  if (!res.ok) {
    throw new Error(`Failed to regenerate schedule: ${res.status}`)
  }
  return await res.json()
}

export async function getCurrentActivity(
  characterId: string,
): Promise<CurrentActivitySnapshot> {
  const res = await authedFetch(
    `${BASE}/characters/${characterId}/schedule/current`,
  )
  if (!res.ok) {
    throw new Error(`Failed to load current activity: ${res.status}`)
  }
  return await res.json()
}

export interface AddScheduleActivityPayload {
  start: string // HH:MM local
  end: string
  description: string
  category: string
  location?: string | null
  busy_score?: number | null
}

export interface UpdateScheduleActivityPayload {
  start?: string
  end?: string
  description?: string
  category?: string
  location?: string | null
  busy_score?: number | null
}

export async function addScheduleActivity(
  characterId: string,
  date: string,
  payload: AddScheduleActivityPayload,
): Promise<DailySchedule> {
  const res = await authedFetch(
    `${BASE}/characters/${characterId}/schedule/activities?date=${encodeURIComponent(date)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  )
  if (!res.ok) {
    throw new Error(await readErrorResponse(res))
  }
  return await res.json()
}

export async function updateScheduleActivity(
  characterId: string,
  date: string,
  activityId: string,
  payload: UpdateScheduleActivityPayload,
): Promise<DailySchedule> {
  const res = await authedFetch(
    `${BASE}/characters/${characterId}/schedule/activities/${activityId}?date=${encodeURIComponent(date)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  )
  if (!res.ok) {
    throw new Error(await readErrorResponse(res))
  }
  return await res.json()
}

export async function deleteScheduleActivity(
  characterId: string,
  date: string,
  activityId: string,
): Promise<DailySchedule> {
  const res = await authedFetch(
    `${BASE}/characters/${characterId}/schedule/activities/${activityId}?date=${encodeURIComponent(date)}`,
    { method: 'DELETE' },
  )
  if (!res.ok) {
    throw new Error(await readErrorResponse(res))
  }
  return await res.json()
}
