/**
 * 聊天訊息的圖片附件 →放大浮窗的集合（LB3，計畫 `IMAGE_LIGHTBOX_PLAN.md`）。
 *
 * 只是一次 filter + map，卻獨立成純函式，理由是它把這張票唯一會**無聲**壞掉
 * 的地方變成型別擋得住的事：
 *
 * 氣泡裡的圖片捲出視窗時會被釋放（`releasedImageSrc` 把顯示用的 `src` 換成
 * 空字串以放掉解碼點陣）。浮窗的集合若是從那個**顯示值**長出來的，玩家往上
 * 捲遠一點再點開就是一片空白。而前端測試 harness 是 SSR（沒有 jsdom、沒有
 * `IntersectionObserver`），釋放這件事在測試裡一次都不會發生——那種壞法在
 * 所有測試裡永遠是綠的，只有真人捲過頭才看得到。
 *
 * 這支函式的輸入是 `MessageAttachment`，它身上只有原始 URL，釋放後的空字串
 * 沒有路徑進得來。分工照 `offscreenImageRelease.ts` 的既有成例：元件只負責
 * 把它接上畫面。
 */
import type { MessageAttachment } from '@/types/tool'
import type { LightboxItem } from '@/utils/lightbox'

/** 附件是圖片的判準，與 `ChatBubble` 的圖片／檔案分流同一條。 */
export function isChatImageAttachment(att: MessageAttachment): boolean {
  return att.kind === 'image'
}

/**
 * 一則訊息的圖片附件轉成浮窗項目，**順序即索引**。
 *
 * 自己再 filter 一次不是多餘：呼叫端的縮圖是對「已篩過的圖片附件」跑 `v-for`，
 * 索引要對得上就得用同一條判準。傳整份附件或傳已篩過的，出來的順序都相同。
 *
 * `url` 恆為原始 URL——`LightboxItem` 的契約要的就是沒有加工過的那個值，
 * 浮窗的「開原圖」與 `UiImage` 的尺寸變體都從它推導。
 */
export function chatImageLightboxItems(
  attachments: readonly MessageAttachment[] | null | undefined,
): LightboxItem[] {
  if (!attachments) return []
  return attachments
    .filter(isChatImageAttachment)
    .map(att => ({ url: att.url, caption: att.caption }))
}
