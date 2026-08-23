// 玩家人設補充（PP 系列）的首次彈窗規則與「之後再說」記憶。
//
// 仿 chatAssistDiscovery.ts / stageLayout.ts 的 localStorage-helper 模式：
// 具名 key 常數、Storage-like 型別化、try/catch fail-soft（隱私模式下讀寫
// 會 throw，一律當成「沒設定過」而不是讓整個聊天面板炸掉），每個操作各自
// 一個純函式。
//
// per-character key 是刻意設計：這份自述本來就是對某一個角色說的，玩家對
// A 角色按了「之後再說」，不該連帶讓 B 角色永遠不問。

export const PLAYER_PERSONA_NOTE_DISMISS_KEY_PREFIX = 'kokoro.playerPersonaNote.dismissed.'

type PlayerPersonaNoteStorage = Pick<Storage, 'getItem' | 'setItem'>

export function playerPersonaNoteDismissKey(characterId: string): string {
  return `${PLAYER_PERSONA_NOTE_DISMISS_KEY_PREFIX}${characterId}`
}

export function isPlayerPersonaNoteDismissed(
  storage: PlayerPersonaNoteStorage | null | undefined,
  characterId: string | null | undefined,
): boolean {
  if (!storage || !characterId) return false
  try {
    return storage.getItem(playerPersonaNoteDismissKey(characterId)) === '1'
  } catch {
    return false
  }
}

export function rememberPlayerPersonaNoteDismissed(
  storage: PlayerPersonaNoteStorage | null | undefined,
  characterId: string | null | undefined,
): boolean {
  if (!storage || !characterId) return false
  try {
    storage.setItem(playerPersonaNoteDismissKey(characterId), '1')
    return true
  } catch {
    return false
  }
}

export interface PlayerPersonaNotePromptInput {
  /** 目前開著的角色；null＝還沒選角色，什麼都不該彈。 */
  characterId: string | null
  /** 這個角色的歷史還在載入中。 */
  loadingHistory: boolean
  /**
   * 這個角色的歷史「讀失敗了」。
   *
   * 失敗與「真的零訊息」在 `messageCount` 上長得一模一樣（兩者都是 0），
   * 而載入已經結束所以 `loadingHistory` 也回到 false。少了這一維，一次
   * 網路抖動就會讓聊了幾百則的人被當成新玩家彈窗——按下「之後再說」還會
   * 替他寫下一個永久的 dismiss。
   */
  historyFailed: boolean
  /** 這個角色的自述已經 GET 回來了（載入中／失敗都是 false）。 */
  noteLoaded: boolean
  /** 已存在的自述內容。非空＝玩家早就填過，不再打擾。 */
  note: string
  /** 這個角色目前載入到的訊息數。>0＝聊過了，時機已過。 */
  messageCount: number
  /** 這台裝置上，玩家對這個角色按過「之後再說」。 */
  dismissed: boolean
  /** 這一輪進場已經彈過（關掉之後不該立刻被同一條規則再彈開）。 */
  alreadyPrompted: boolean
}

/**
 * 首次進入某個角色的聊天時，要不要主動彈出人設補充。
 *
 * 抽成純函式而非塞進 ChatPanel 的 watcher：真正值得釘住的是這組條件的
 * 交集（尤其是「訊息數為 0 不等於沒聊過」這兩條——歷史還沒回來、或根本
 * 沒讀回來時 messageCount 恆為 0，少了 loadingHistory / historyFailed
 * 這兩道閘就會對老玩家彈窗）。
 */
export function shouldPromptPlayerPersonaNote(
  input: PlayerPersonaNotePromptInput,
): boolean {
  if (!input.characterId) return false
  if (input.loadingHistory) return false
  if (input.historyFailed) return false
  if (!input.noteLoaded) return false
  if (input.alreadyPrompted) return false
  if (input.dismissed) return false
  if (input.note.trim().length > 0) return false
  return input.messageCount === 0
}
