// 首輪示意 tip 氣泡（plan TR4）——指向輸入列「讓角色先開口」圖示按鈕
// （`StageNudgeControl`）的一次性提示，讓玩家知道這個功能之後常駐在哪。
//
// localStorage dismiss 慣例照抄 `playerPersonaNote.ts`：具名 key、
// per-character（對 A 角色關掉不該連帶讓 B 角色永遠不提示）、
// try/catch fail-soft（隱私模式下讀寫會 throw，一律當成「沒設定過」）。

export const STAGE_NUDGE_TIP_DISMISS_KEY_PREFIX = 'kokoro.stageNudgeTip.dismissed.'

type StageNudgeTipStorage = Pick<Storage, 'getItem' | 'setItem'>

export function stageNudgeTipDismissKey(characterId: string): string {
  return `${STAGE_NUDGE_TIP_DISMISS_KEY_PREFIX}${characterId}`
}

export function isStageNudgeTipDismissed(
  storage: StageNudgeTipStorage | null | undefined,
  characterId: string | null | undefined,
): boolean {
  if (!storage || !characterId) return false
  try {
    return storage.getItem(stageNudgeTipDismissKey(characterId)) === '1'
  } catch {
    return false
  }
}

export function rememberStageNudgeTipDismissed(
  storage: StageNudgeTipStorage | null | undefined,
  characterId: string | null | undefined,
): boolean {
  if (!storage || !characterId) return false
  try {
    storage.setItem(stageNudgeTipDismissKey(characterId), '1')
    return true
  } catch {
    return false
  }
}

export interface StageNudgeTipVisibilityInput {
  /** 目前的互動模式。D-TR4-1（owner 拍板）：僅同場模式顯示，DM 側的
   * 「角色先開口」由 TR2 首聯（proactive）補位，不在這裡重複做。 */
  mode: 'stage' | 'dm'
  /** 這個角色的歷史還在載入中——避免在確認「真的零訊息」之前先閃現。 */
  loadingHistory: boolean
  /**
   * 這個角色的歷史「讀失敗了」（同 `shouldPromptPlayerPersonaNote` 的
   * `historyFailed`）。
   *
   * 失敗時 `messageCount` 會跟「真的零訊息」長得一模一樣，而
   * `loadingHistory` 也已經回到 false——少了這一維，一次網路抖動就會對
   * 聊了幾百則的老玩家彈這顆 tip；玩家關掉它會順手把 dismiss 永久寫死在
   * 這個角色身上，之後 history 恢復正常也再也看不到這顆 tip 了。
   */
  historyFailed: boolean
  /** 目前載入到的訊息數。>0＝已經聊過，首輪時機已過。 */
  messageCount: number
  /**
   * 玩家人設補充（`usePlayerPersonaNote`）是否已經 GET 回來（載入中／
   * 失敗都是 false，同 `PlayerPersonaNotePromptInput.noteLoaded`）。
   *
   * D-TR4-2 真正要保的時序是「PP 彈窗要蓋這顆 tip 之前，tip 不該先閃一下
   * 再被蓋掉」。`personaNoteModalOpen` 只在 PP 真的決定要開的那一刻才變
   * true——在那之前（自述還沒讀回來、`shouldPromptPlayerPersonaNote`
   * 還沒下判斷）有一段窗口兩者都是 false，這顆 tip 會先閃現，PP 彈窗才
   * 接著蓋上去，等於玩家兩個引導各看到一半。等自述載入完成（不論最後
   * PP 開不開），這顆 tip 才有資格顯示。
   */
  noteLoaded: boolean
  /**
   * 玩家人設補充彈窗（`PlayerPersonaNoteModal`）目前是否開著。
   *
   * D-TR4-2（owner 拍板）：不論是首開條件自動彈出、還是玩家自己點開，
   * 只要它在畫面上，這顆 tip 就不該疊上去——同屏疊兩個引導本身就是
   * 噪音。等它關閉之後才輪到這顆 tip。
   */
  personaNoteModalOpen: boolean
  /** 這台裝置上，玩家對這個角色已經看過並關掉過這顆 tip。 */
  dismissed: boolean
}

/**
 * 首輪要不要顯示指向輸入列示意 icon 的 tip 氣泡。
 *
 * 抽成純函式而非塞進 `ChatPanel` 的 computed 本體：真正值得釘住的是這組
 * 條件的交集（尤其是「PP 彈窗在畫面上時讓位」這條——同屏兩個引導互相
 * 搶注意力，等於兩個都沒讀到）。
 */
export function shouldShowStageNudgeTip(input: StageNudgeTipVisibilityInput): boolean {
  if (input.mode !== 'stage') return false
  if (input.loadingHistory) return false
  if (input.historyFailed) return false
  if (input.messageCount !== 0) return false
  if (!input.noteLoaded) return false
  if (input.personaNoteModalOpen) return false
  if (input.dismissed) return false
  return true
}
