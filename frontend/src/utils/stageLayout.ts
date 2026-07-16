// 桌面 landscape 版面偏好：stage-centric（預設）/ chat-centric。
// 仿 arcDiscovery.ts 的 localStorage-helper 模式：具名 key 常數、
// Storage-like 型別化、try/catch fail-soft、每個操作各自一個純函式。
//
// per-character key 是刻意設計：不同角色的「有沒有圖」狀態不同，使用者
// 對某個有豐富立繪的角色可能偏好 stage-centric、對另一個文字角色偏好
// chat-centric；若用單一全域 key，會讓「角色 A 顯式設定」污染「角色 B
// 的自動規則」判斷。

export const STAGE_LAYOUT_KEY_PREFIX = 'kokoro.stageLayout.'

export type StageLayoutMode = 'stage-centric' | 'chat-centric'

type StageLayoutStorage = Pick<Storage, 'getItem' | 'setItem'>

export function stageLayoutKey(characterId: string): string {
  return `${STAGE_LAYOUT_KEY_PREFIX}${characterId}`
}

/**
 * 讀出使用者對「這個角色」明確設定過的偏好；未設定過回傳 null。
 * null 用來跟已拍板的自動規則區分：null = 尚未顯式設定，交給呼叫端套
 * 自動規則（resolveStageLayout）。
 */
export function getExplicitStageLayout(
  storage: StageLayoutStorage | null | undefined,
  characterId: string | null | undefined,
): StageLayoutMode | null {
  if (!storage || !characterId) return null
  try {
    const raw = storage.getItem(stageLayoutKey(characterId))
    return raw === 'stage-centric' || raw === 'chat-centric' ? raw : null
  } catch {
    return null
  }
}

export function setExplicitStageLayout(
  storage: StageLayoutStorage | null | undefined,
  characterId: string | null | undefined,
  mode: StageLayoutMode,
): boolean {
  if (!storage || !characterId) return false
  try {
    storage.setItem(stageLayoutKey(characterId), mode)
    return true
  } catch {
    return false
  }
}

/**
 * 純函式：給定「使用者是否顯式設過偏好」與「角色是否有圖」，算出實際
 * 生效模式。呼叫端負責提供 explicit（來自 getExplicitStageLayout）與
 * hasImages。顯式偏好永遠優先於自動規則（角色無圖 -> chat-centric）。
 */
export function resolveStageLayout(input: {
  explicit: StageLayoutMode | null
  hasImages: boolean
}): StageLayoutMode {
  if (input.explicit) return input.explicit
  return input.hasImages ? 'stage-centric' : 'chat-centric'
}
