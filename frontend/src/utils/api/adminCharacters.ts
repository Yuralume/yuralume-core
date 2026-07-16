import axios from 'axios'

/**
 * Site-wide character freeze admin API (Character Freeze plan). Freezing
 * a character keeps its state but stops all background activity (proactive
 * ticks, schedule advancement, etc.) to cut LLM cost; the backend
 * auto-unfreezes on the next player chat turn. This client only covers the
 * admin-facing overview + manual freeze/unfreeze actions — the auto-freeze
 * idle threshold lives under the generic app-settings group
 * `character_freeze` (see `CharacterFreezeRuntimeConfig` in `appSettings.ts`).
 */

/**
 * Freeze provenance. `idle` = auto-sweep reaper (chat auto-thaws);
 * `manual` = admin console (sticky); `subscription_lapse` = Cloud tenant
 * tier downgrade (billing hard-lock, blocks chat). `null` when not frozen
 * or on legacy rows.
 */
export type FreezeReason = 'idle' | 'manual' | 'subscription_lapse'

export interface AdminCharacterOverviewRow {
  id: string
  name: string
  owner_user_id: string
  frozen: boolean
  frozen_at: string | null
  frozen_reason: FreezeReason | null
  last_active_at: string | null
  created_at: string | null
  proactive_enabled: boolean
}

export interface AdminCharacterOverview {
  characters: AdminCharacterOverviewRow[]
  total: number
}

export interface AdminCharacterFreezeResult {
  id: string
  frozen: boolean
  frozen_at: string | null
  frozen_reason: FreezeReason | null
}

export async function getCharacterFreezeOverview(): Promise<AdminCharacterOverview> {
  const { data } = await axios.get<AdminCharacterOverview>(
    '/api/v1/admin/characters/overview',
  )
  return data
}

export async function freezeCharacter(id: string): Promise<AdminCharacterFreezeResult> {
  const { data } = await axios.post<AdminCharacterFreezeResult>(
    `/api/v1/admin/characters/${id}/freeze`,
  )
  return data
}

export async function unfreezeCharacter(id: string): Promise<AdminCharacterFreezeResult> {
  const { data } = await axios.post<AdminCharacterFreezeResult>(
    `/api/v1/admin/characters/${id}/unfreeze`,
  )
  return data
}
