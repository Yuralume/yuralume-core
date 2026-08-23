import axios from 'axios'

/**
 * 玩家人設補充（PP 系列）：玩家自述的身分／世界觀設定，per-character。
 *
 * 上限鏡像後端 `PLAYER_PERSONA_NOTE_MAX_CHARS`。前端只用它來擋輸入長度與
 * 顯示字數提示——真正的把關在後端（entity + route 各一道），這裡多打幾個
 * 字最多換來一次 422，不會寫進 prompt。
 */
export const PLAYER_PERSONA_NOTE_MAX_CHARS = 500

export interface PlayerPersonaNote {
  character_id: string
  operator_id: string
  note: string
  updated_at?: string | null
}

const BASE = '/api/v1'

function url(characterId: string): string {
  return `${BASE}/characters/${encodeURIComponent(characterId)}/player-persona-note`
}

/** 讀出這對「玩家 × 角色」目前的自述；沒設定過時 `note` 是空字串。 */
export async function getPlayerPersonaNote(
  characterId: string,
): Promise<PlayerPersonaNote> {
  const { data } = await axios.get<PlayerPersonaNote>(url(characterId))
  return data
}

/** 整份取代。空字串＝清除，不是「不動」——與後端契約一致。 */
export async function updatePlayerPersonaNote(
  characterId: string,
  note: string,
): Promise<PlayerPersonaNote> {
  const { data } = await axios.put<PlayerPersonaNote>(url(characterId), { note })
  return data
}
