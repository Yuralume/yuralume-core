/**
 * 聊天輸入框自動長高的高度計算。
 *
 * 抽出來的理由是兩個瀏覽器差異，都只能用數字釘住、看程式碼看不出來：
 *
 * 1. Blink 的 `textarea.scrollHeight` **會把換行後的 placeholder 一起算進去**，
 *    Gecko 不會。我們的 placeholder 是長句（「跟 X 說點什麼…（Enter 送出、
 *    Shift+Enter 換行、Ctrl/⌘+V 貼圖）」），在手機寬度下會折成五六行——實測
 *    360px 視窗量到 154px、320px 量到 244px、280px 量到 602px。送出後
 *    `inputText` 被清空會再跑一次 resize，於是輸入框就永久卡在 max-height 的
 *    天花板上，明明是空的卻佔掉半個畫面。空內容一律回 CSS 的單行地板。
 *
 * 2. `scrollHeight` 不含 border，但全域是 `box-sizing: border-box`，直接寫回去
 *    每次都短 2px（單行就開始出捲軸）。用 offsetHeight - clientHeight 補回來。
 */

/** `HTMLTextAreaElement` 中本函式真正用到的部分——測試不必造整顆 DOM。 */
export interface ComposerMetrics {
  value: string
  scrollHeight: number
  /** 含 border 的外框高度 */
  offsetHeight: number
  /** 不含 border 的內框高度 */
  clientHeight: number
}

/**
 * 算出要寫進 `style.height` 的值；回傳 `null` 代表「交還給 CSS」
 * （清掉 inline height，讓 min-height 決定單行高度）。
 */
export function composerHeightFor(el: ComposerMetrics): string | null {
  if (el.value === '') return null

  const borders = Math.max(0, el.offsetHeight - el.clientHeight)
  return `${el.scrollHeight + borders}px`
}
