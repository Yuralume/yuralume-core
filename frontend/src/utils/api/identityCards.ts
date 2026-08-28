import axios from 'axios'
import type { ScheduleInvolvementPolicy } from '@/types/character'

/**
 * 玩家身分卡（IC 系列）：把創角精靈的整組欄位存成具名範本，建新角色時帶入。
 *
 * 卡片是**快照**：帶入後欄位照常可改、改了不回寫卡片，卡片事後更新也不影響
 * 已建角色（IC 計畫 §1「慣例默認」）。所以這裡沒有、也不該有「套用」端點——
 * 套卡完全是前端行為（GET 卡 → 填精靈 → 建角 → 再 PUT persona note）。
 *
 * 上限與錯誤碼鏡像後端（`domain/entities/player_identity_card.py` 與
 * `api/routes/player_identity_card.py`）。前端只用它們擋輸入長度與分辨衝突
 * 種類，真正的把關在後端。
 */

/** 鏡像 `PLAYER_IDENTITY_CARD_NAME_MAX_CHARS`。 */
export const IDENTITY_CARD_NAME_MAX_CHARS = 80

/** 鏡像 `PLAYER_IDENTITY_CARDS_PER_OPERATOR`；`GET` 也會回一份 `limit`。 */
export const IDENTITY_CARDS_PER_OPERATOR = 30

/** 同 operator 同名已存在——前端問「覆蓋既有？」後帶 `overwrite` 重送。 */
export const IDENTITY_CARD_NAME_CONFLICT_CODE = 'identity_card_name_conflict'

/** 新增會超過每帳號上限；覆蓋不算新增，滿額仍可覆蓋。 */
export const IDENTITY_CARD_LIMIT_REACHED_CODE = 'identity_card_limit_reached'

/** 非本人卡或不存在。 */
export const IDENTITY_CARD_NOT_FOUND_CODE = 'identity_card_not_found'

/**
 * 卡片內容：11 個初始關係 seed 欄位 ＋ persona note。
 *
 * 欄名與 `GET/PATCH /characters/{id}/initial-relationship`、
 * `PUT /characters/{id}/player-persona-note` 完全同名同義——套卡因此是逐欄
 * 直搬，中間沒有任何轉換表可以漂移。
 */
export interface IdentityCardContent {
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
  persona_note: string
}

export interface IdentityCard extends IdentityCardContent {
  id: string
  operator_id: string
  name: string
  created_at: string
  updated_at: string
}

export interface IdentityCardListResponse {
  cards: IdentityCard[]
  limit: number
}

export type IdentityCardCreateRequest = IdentityCardContent & {
  name: string
  overwrite?: boolean
}

const BASE = '/api/v1/identity-cards'

function cardUrl(cardId: string): string {
  return `${BASE}/${encodeURIComponent(cardId)}`
}

/** 這個帳號的全部卡片，updated_at 新→舊。 */
export async function listIdentityCards(): Promise<IdentityCardListResponse> {
  const { data } = await axios.get<IdentityCardListResponse>(BASE)
  return data
}

/** 建卡；`overwrite: true` 覆蓋同名卡的內容（保留原 id 與 created_at）。 */
export async function createIdentityCard(
  body: IdentityCardCreateRequest,
): Promise<IdentityCard> {
  const { data } = await axios.post<IdentityCard>(BASE, body)
  return data
}

/** 只改名；沒有 overwrite 語意（撞名照樣 409）。 */
export async function renameIdentityCard(
  cardId: string,
  name: string,
): Promise<IdentityCard> {
  const { data } = await axios.patch<IdentityCard>(cardUrl(cardId), { name })
  return data
}

export async function deleteIdentityCard(cardId: string): Promise<void> {
  await axios.delete(cardUrl(cardId))
}

interface IdentityCardErrorDetail {
  code?: unknown
  card_id?: unknown
  name?: unknown
  current?: unknown
  limit?: unknown
}

/**
 * 從一個失敗的請求裡讀出結構化的 `detail`。
 *
 * 刻意 duck-type 而不用 `axios.isAxiosError`：這層只關心「有沒有一包帶 code
 * 的 detail」，而測試裡的 axios 是被 mock 掉的模組（沒有 isAxiosError）。
 */
export function identityCardErrorDetail(error: unknown): IdentityCardErrorDetail | null {
  const detail = (error as {
    response?: { data?: { detail?: unknown } }
  } | null)?.response?.data?.detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null
  const code = (detail as IdentityCardErrorDetail).code
  return typeof code === 'string' ? (detail as IdentityCardErrorDetail) : null
}

/** 這個錯誤的 `detail.code`（不是身分卡的結構化錯誤時回 `null`）。 */
export function identityCardErrorCode(error: unknown): string | null {
  const detail = identityCardErrorDetail(error)
  return typeof detail?.code === 'string' ? detail.code : null
}

export function isIdentityCardNameConflict(error: unknown): boolean {
  return identityCardErrorCode(error) === IDENTITY_CARD_NAME_CONFLICT_CODE
}

export function isIdentityCardLimitReached(error: unknown): boolean {
  return identityCardErrorCode(error) === IDENTITY_CARD_LIMIT_REACHED_CODE
}
