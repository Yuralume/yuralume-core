import {
  getCurrentPushSubscription,
  resolvePushSupportState,
  type PushSupportState,
} from '@/utils/pushNotifications'

/**
 * 決定是否要在玩家略過「綁定通道」引導後，把他帶去設定並提醒可開啟系統推播。
 *
 * Web Push 訂閱依瀏覽器安全模型，必須由使用者手勢觸發權限授權，無法預設打開；
 * 因此退而求其次：只有在瀏覽器「真的能訂閱（supported）且目前尚未訂閱」時才提醒。
 * 不支援、未設定 VAPID、已被封鎖、或已經訂閱，都不該再打擾玩家。
 */
export function shouldNudgeWebPush(
  state: PushSupportState,
  subscription: PushSubscription | null,
): boolean {
  return state === 'supported' && subscription === null
}

/**
 * 解析目前瀏覽器的 Web Push 狀態，回傳是否值得提醒玩家開啟。
 * 任一查詢失敗都視為「不提醒」，避免在不確定狀態下硬把玩家帶離當前畫面。
 */
export async function resolveWebPushNudge(): Promise<boolean> {
  try {
    const [state, subscription] = await Promise.all([
      resolvePushSupportState(),
      getCurrentPushSubscription(),
    ])
    return shouldNudgeWebPush(state, subscription)
  } catch {
    return false
  }
}
