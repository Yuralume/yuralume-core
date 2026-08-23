/**
 * 浮窗開著的時候，底下那份清單被換掉了要怎麼辦（LB5，計畫
 * `IMAGE_LIGHTBOX_PLAN.md` §5）。
 *
 * `UiLightbox` 只吃「清單 ＋ 索引」——而**索引是位置，不是身分**。
 * `CharacterImagesPanel` 的兩份集合都不是靜態的：舞台圖會被排序（◀▶ 直接換
 * 位置）、封存與刪除（整筆消失），候選圖會被整批 commit 或捨棄，換角色更是
 * 整份換人。清單一動而索引不動，浮窗就會無聲地變成在看另一張圖——最糟的一種
 * 錯，因為畫面上什麼異常都沒有。
 *
 * 這裡是那個判斷。之所以做成純函式而不是元件裡的幾行 `if`：前端測試 harness
 * 是 SSR（`createSSRApp` + `renderToString`，沒有 jsdom），元件裡的分支沒有閘
 * 可守。分工照 `utils/lightbox.ts` 與 `utils/offscreenImageRelease.ts` 的既有
 * 成例：有決定的那一半是被測的那一半。
 */
import { wrapLightboxIndex } from './lightbox'

/** 清單換掉之後，浮窗該怎麼辦。 */
export type ZoomSyncOutcome =
  /** 什麼都不用做：沒開窗，或看的還是同一張、位置也沒變。 */
  | { outcome: 'stay' }
  /** 同一張圖換了位置（排序），索引跟著走。 */
  | { outcome: 'move'; index: number }
  /** 正在看的那張已經不在了（刪除／封存／換角色／整批收掉）——關窗。 */
  | { outcome: 'close' }

export interface ZoomSyncInput {
  /** 浮窗目前是不是開著。關著就沒有什麼要同步的。 */
  open: boolean
  /** 浮窗目前停留的索引。 */
  index: number
  /** 變更**前**的 URL 清單——「玩家正在看哪一張」只能從這份推出來。 */
  before: readonly string[] | null | undefined
  /** 變更**後**的 URL 清單。 */
  after: readonly string[] | null | undefined
}

/**
 * 用「變更前後兩份清單」判斷浮窗要不要跟、要不要關。
 *
 * 身分取的是 URL 而不是索引：URL 是這兩份集合裡唯一穩定的識別（舞台圖以
 * `image_urls` 的字串本身當 key，候選圖同理）。
 *
 * 三條規則：
 *
 * 1. **沒開窗就不動索引。** 關著的浮窗沒有「正在看的那張」，這時去改索引只會
 *    讓下次開窗停在一個玩家沒點過的位置。
 * 2. **同位置還是同一張就停在原地。** 這是絕大多數情況（父層把 character 物件
 *    換成內容相同的新物件），不該產生任何多餘的 `update:index`。
 * 3. **找得到就跟過去，找不到就關窗。** 找不到＝那張圖被刪了／被封存進相簿了／
 *    整批候選被收掉了／換人了。此時繼續留在同一個索引上，畫面會變成「同一格
 *    位置的另一張圖」，而玩家不會知道自己看的已經不是剛剛那張。
 *
 * 空清單一律 `close`：`after` 空是集合沒了；`before` 空則代表根本推不出玩家在
 * 看哪一張，關窗是唯一誠實的選擇。
 */
export function syncZoomAfterUrlsChange(input: ZoomSyncInput): ZoomSyncOutcome {
  if (!input.open) return { outcome: 'stay' }

  const before = input.before ?? []
  const after = input.after ?? []
  if (before.length === 0 || after.length === 0) return { outcome: 'close' }

  // 索引可能越界（清單剛被上游換過、或呼叫端傳進來的是上一輪 render 的值），
  // 沿用浮窗自己的收斂方式，兩邊對同一個索引的解讀才會一致。
  const at = wrapLightboxIndex(input.index, before.length)
  const url = before[at]
  if (!url) return { outcome: 'close' }

  // 先比同一格：清單裡出現重複 URL 時，「原地那張」才是玩家真正在看的那張。
  if (after[at] === url) return { outcome: 'stay' }

  const moved = after.indexOf(url)
  if (moved < 0) return { outcome: 'close' }
  return { outcome: 'move', index: moved }
}
