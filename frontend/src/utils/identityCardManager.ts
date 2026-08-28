import type { IdentityCard } from '@/utils/api/identityCards'

/**
 * 設定頁「玩家身分卡」管理面（IC3）的清單狀態轉換——純函式，供
 * `usePlayerIdentityCards` 在改名／刪除成功後就地更新清單，不必為了一次
 * 改動重新 GET 整份列表。
 */

/** 改名成功：用後端回寫的卡片取代清單裡同 id 的那筆。 */
export function replaceIdentityCardInList(
  cards: IdentityCard[],
  updated: IdentityCard,
): IdentityCard[] {
  return cards.map(card => (card.id === updated.id ? updated : card))
}

/** 刪除成功：從清單移除那筆卡片。 */
export function removeIdentityCardById(
  cards: IdentityCard[],
  cardId: string,
): IdentityCard[] {
  return cards.filter(card => card.id !== cardId)
}

/**
 * 依 `updated_at` 新到舊排序，時間相同時用 `id` 降冪打平——與後端
 * `SAPlayerIdentityCardRepository.list_for_operator` 的
 * `order_by(updated_at.desc(), id.desc())` 是同一份契約。
 *
 * 改名會讓後端把 `updated_at` 推到現在，若清單只就地替換不重排，改到的
 * 那張卡會停在原本的位置而不是浮到最新——跟玩家從精靈那邊看到「最近存
 * 過的卡在最前面」的順序矛盾。
 */
export function sortIdentityCardsByRecency(cards: IdentityCard[]): IdentityCard[] {
  return [...cards].sort((a, b) => {
    const byTime = Date.parse(b.updated_at) - Date.parse(a.updated_at)
    if (byTime !== 0) return byTime
    return a.id < b.id ? 1 : a.id > b.id ? -1 : 0
  })
}
