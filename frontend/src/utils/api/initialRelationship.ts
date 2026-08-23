import axios from 'axios'
import type { ScheduleInvolvementPolicy } from '@/types/character'

/**
 * 玩家編輯自己與角色的完整初始關係 seed（IR2）。
 *
 * 對應後端 `GET`/`PATCH /characters/{id}/initial-relationship`（IR1）：
 * PATCH 是 tri-state——欄位缺席＝不動，空字串＝清空，有值＝設定。
 * `confirmed_by_user` 不在可寫欄位裡：它只在建立當下由玩家確認一次，
 * 之後的編輯沒有資格重新聲稱那個時間點的確認。
 */

export interface InitialRelationshipSeed {
  character_id: string
  operator_id: string
  /** false＝這對玩家 × 角色還沒有任何 seed，其餘欄位都是空的預設值。 */
  has_seed: boolean
  relationship_label: string
  known_context: string
  living_arrangement: string
  user_address_name: string
  character_address_name: string
  tone_distance: string
  familiarity_boundary: string
  schedule_involvement_policy: ScheduleInvolvementPolicy
  proactive_permission: boolean
  proactive_cadence_hint: string
  user_profile_notes: string
  confirmed_by_user: boolean
}

/** PATCH payload：每個欄位缺席＝不動，空字串＝清空。由呼叫端只放「真的改過」的欄位。 */
export interface InitialRelationshipEditPatch {
  relationship_label?: string
  known_context?: string
  living_arrangement?: string
  user_address_name?: string
  character_address_name?: string
  tone_distance?: string
  familiarity_boundary?: string
  schedule_involvement_policy?: ScheduleInvolvementPolicy
  proactive_permission?: boolean
  proactive_cadence_hint?: string
  user_profile_notes?: string
}

const BASE = '/api/v1'

function url(characterId: string): string {
  return `${BASE}/characters/${encodeURIComponent(characterId)}/initial-relationship`
}

/** 讀出這對「玩家 × 角色」目前的整份關係 seed；沒設定過時回傳空白預設值（`has_seed: false`）。 */
export async function getInitialRelationship(
  characterId: string,
): Promise<InitialRelationshipSeed> {
  const { data } = await axios.get<InitialRelationshipSeed>(url(characterId))
  return data
}

/**
 * 只送真的改過的欄位（tri-state）。稱呼兩欄位也走這裡——後端會內部轉呼
 * `relationship-names` 的邏輯，rename-log 與已學到的人設投影一樣會同步，
 * 前端不需要另外呼叫 `/relationship-names`。
 */
export async function updateInitialRelationship(
  characterId: string,
  patch: InitialRelationshipEditPatch,
): Promise<InitialRelationshipSeed> {
  const { data } = await axios.patch<InitialRelationshipSeed>(url(characterId), patch)
  return data
}
