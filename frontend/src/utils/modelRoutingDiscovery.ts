export const MODEL_ROUTING_DISCOVERED_KEY = 'yuralume.modelRouting.discovered'

type ModelRoutingDiscoveryStorage = Pick<Storage, 'getItem' | 'setItem'>

/**
 * 一次性 coachmark 狀態：使用者是否已經「發現」admin nav 的 LLM 路由項目
 * （進過 `/admin/models` 一次）。結構比照 `chatAssistDiscovery.ts`，但只有
 * 「discovered」一個狀態——nav 旁的小圓點沒有「之後再說」的必要，因為
 * 它不是卡片，只是低調的常駐提示，點過一次就永久隱藏。
 */
export function isModelRoutingDiscovered(
  storage: ModelRoutingDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    return storage.getItem(MODEL_ROUTING_DISCOVERED_KEY) === '1'
  } catch {
    return false
  }
}

export function rememberModelRoutingDiscovered(
  storage: ModelRoutingDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(MODEL_ROUTING_DISCOVERED_KEY, '1')
    return true
  } catch {
    return false
  }
}
