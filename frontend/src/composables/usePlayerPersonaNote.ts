import { getCurrentScope, onScopeDispose, ref } from 'vue'

import { getPlayerPersonaNote } from '@/utils/api/playerPersonaNote'

/**
 * 一份「這個角色的玩家自述」讀取狀態，給需要知道它填了沒的 surface 用
 * （聊天 header 的 chip、設定頁欄位、首次彈窗規則）。
 *
 * 寫入不在這裡——只有 `PlayerPersonaNoteModal` 會 PUT，存完把結果餵回
 * `apply()`。讓寫入路徑只有一條，chip 與設定頁就不可能各自算出不同答案。
 */

/**
 * 「某個角色的自述剛被寫過」的跨實例廣播。
 *
 * 這是個工廠函式（每個 surface 各持一份 state），而 StagePage 會同時掛
 * 聊天面板與側欄設定頁兩個 surface。少了這條廣播，A 存完之後 B 手上那份
 * 就是過期值，而 B 的編輯框以過期值預填、按儲存＝整份取代＝無聲蓋掉剛存
 * 的內容。
 *
 * 用 CustomEvent 而不是把 state 提到 module 層，是照 repo 既有慣例：
 * operator profile 的跨面板同步走 `kokoro:operator-profile-updated`
 * （`PersonalSettingsSection.vue` / `usePlayerLocaleSettings.ts`）。同一種
 * 問題用同一種解法，下一個人不必先搞清楚這裡為什麼特別。共享 module state
 * 另有一個實際壞處：這份資料是 per-character 的，跨實例共用一份就得自己
 * 管「誰現在看的是哪個角色」的引用計數與清除，而廣播天然是 per-character
 * 過濾。
 */
export const PLAYER_PERSONA_NOTE_UPDATED_EVENT = 'kokoro:player-persona-note-updated'

export interface PlayerPersonaNoteUpdatedDetail {
  characterId: string
  note: string
}

/** 存檔成功後廣播出去；由唯一的寫入路徑（modal）呼叫。 */
export function publishPlayerPersonaNoteUpdated(
  characterId: string,
  note: string,
): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent<PlayerPersonaNoteUpdatedDetail>(
    PLAYER_PERSONA_NOTE_UPDATED_EVENT,
    { detail: { characterId, note } },
  ))
}

export function usePlayerPersonaNote() {
  const note = ref('')
  /** GET 成功回來過。載入中、失敗都是 false。 */
  const loaded = ref(false)
  const loading = ref(false)
  // 切角色時舊請求可能還在路上；回來得晚的那一個不准覆寫新角色的答案。
  let requestSeq = 0
  // 廣播過濾用：這份 state 現在講的是哪個角色。
  let currentCharacterId: string | null = null

  async function load(characterId: string | null | undefined): Promise<void> {
    const seq = ++requestSeq
    currentCharacterId = characterId ?? null
    note.value = ''
    loaded.value = false
    if (!characterId) {
      loading.value = false
      return
    }
    loading.value = true
    try {
      const data = await getPlayerPersonaNote(characterId)
      if (seq !== requestSeq) return
      note.value = data.note ?? ''
      loaded.value = true
    } catch {
      // Fail-soft：讀不到就停在「不知道」。`loaded` 留 false，首次彈窗規則
      // 因此不會觸發——寧可少問一次，也不要對早就填過的人再彈一次窗。
      // 編輯入口也看這個旗標：不知道現況就不給空白編輯框（見 modal）。
      if (seq !== requestSeq) return
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  /** 重讀目前這個角色（讀取失敗後的重試）。 */
  async function reload(): Promise<void> {
    await load(currentCharacterId)
  }

  /** 存檔成功後把新值灌回來（含清空）。 */
  function apply(value: string): void {
    // 這個值現在就是事實，比任何還在路上的 GET 都新——把它們全部作廢，
    // 免得等一下才 resolve 的舊回應把這裡剛灌進去的新值蓋回去。
    // （見檔頭：這正是「在途 GET 蓋掉較新廣播值」那個窄化殘洞的收口。）
    requestSeq += 1
    note.value = value
    loaded.value = true
  }

  function handleUpdated(event: Event): void {
    const detail = (event as CustomEvent<PlayerPersonaNoteUpdatedDetail>).detail
    if (!detail || !currentCharacterId) return
    if (detail.characterId !== currentCharacterId) return
    // 別的 surface 剛存過同一個角色——這份 state 立刻同步，下一次開編輯框
    // 才不會拿過期值預填。同時把 `loaded` 拉起來：內容現在是已知事實。
    apply(detail.note)
  }

  if (typeof window !== 'undefined') {
    window.addEventListener(PLAYER_PERSONA_NOTE_UPDATED_EVENT, handleUpdated)
    if (getCurrentScope()) {
      onScopeDispose(() => {
        window.removeEventListener(PLAYER_PERSONA_NOTE_UPDATED_EVENT, handleUpdated)
      })
    }
  }

  return { note, loaded, loading, load, reload, apply }
}
