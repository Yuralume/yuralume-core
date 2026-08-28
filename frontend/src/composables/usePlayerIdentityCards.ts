import { ref } from 'vue'
import {
  deleteIdentityCard,
  listIdentityCards,
  renameIdentityCard,
  IDENTITY_CARDS_PER_OPERATOR,
  type IdentityCard,
} from '@/utils/api/identityCards'
import {
  removeIdentityCardById,
  replaceIdentityCardInList,
  sortIdentityCardsByRecency,
} from '@/utils/identityCardManager'

/**
 * 設定頁「玩家身分卡」管理面的清單載入／改名／刪除狀態（IC3）。
 *
 * 刻意不含 `useI18n()` / `useConfirmDialog()`——同 `useInitialRelationshipSettings`
 * / `usePlayerPersonaNote` 的既有先例，只管 HTTP 與本地清單狀態，因此在沒有
 * Vue context 的 vitest 環境也能直接測（只有 mock axios）。i18n 提示與刪除
 * 前的確認對話框留給呼叫端元件。
 *
 * 改名／刪除成功後**就地更新清單**（見 `identityCardManager.ts`），不重新
 * GET 整份列表——列表本來就是這裡唯一的事實來源，沒有理由為了一筆改動
 * 再打一次可能把使用者剛做的操作以外的東西也重新排序過的請求。
 */
export function usePlayerIdentityCards() {
  const cards = ref<IdentityCard[]>([])
  const limit = ref(IDENTITY_CARDS_PER_OPERATOR)
  const loading = ref(false)
  /** GET 成功回來過一次。載入中、失敗都是 false。 */
  const loaded = ref(false)

  // 舊請求可能還在路上；回來得晚的那一個不准覆寫較新的一次載入。
  let requestSeq = 0

  async function load(): Promise<void> {
    const seq = ++requestSeq
    loading.value = true
    try {
      const result = await listIdentityCards()
      if (seq !== requestSeq) return
      cards.value = result.cards
      limit.value = result.limit
      loaded.value = true
    } catch {
      // Fail-soft，同 `usePlayerPersonaNote`：讀不到就停在「不知道」。
      if (seq !== requestSeq) return
      loaded.value = false
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  async function rename(cardId: string, name: string): Promise<IdentityCard> {
    const updated = await renameIdentityCard(cardId, name.trim())
    // 改名會把後端的 updated_at 推到現在——重排本地清單，讓改到的那張卡浮到
    // 跟後端 GET 出來的順序一致（updated_at desc、id 打平），不是停在原位。
    cards.value = sortIdentityCardsByRecency(replaceIdentityCardInList(cards.value, updated))
    return updated
  }

  async function remove(cardId: string): Promise<void> {
    await deleteIdentityCard(cardId)
    cards.value = removeIdentityCardById(cards.value, cardId)
  }

  return { cards, limit, loading, loaded, load, rename, remove }
}
