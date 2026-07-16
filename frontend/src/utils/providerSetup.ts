import { listProviders } from '@/utils/api/system'
import type { ActiveModelPreference, FeatureModelGroupSummary } from '@/utils/api/system'

/**
 * `fake` 是後端 model registry 在「還沒接上任何真實 provider」時回傳的佔位後備，
 * 不算真的設定好。其餘 id 視為玩家自己接上的真實 provider。
 */
const FALLBACK_PROVIDER_ID = 'fake'

/**
 * 數出真正可用的 provider 數量（排除佔位用的 `fake` 後備）。
 */
export function countRealProviders(providerIds: readonly string[]): number {
  return providerIds.filter(id => id !== FALLBACK_PROVIDER_ID).length
}

/**
 * 判斷是否需要引導使用者去設定 LLM 服務供應商。
 *
 * 自架（self-host）首次部署完成後，玩家會直接落在玩家頁；若還沒接上任何真實
 * provider，聊天與角色語意任務都跑不起來。cloud 模式由控制面板代管路由，玩家端
 * 不需要、也無權處理這個引導，因此一律回 false。
 */
export function shouldGuideProviderSetup(input: {
  cloudMode: boolean
  providerIds: readonly string[]
}): boolean {
  if (input.cloudMode) return false
  return countRealProviders(input.providerIds) === 0
}

/**
 * 查詢執行期已註冊的 provider，回傳是否需要顯示「先設定 LLM provider」引導。
 *
 * cloud 模式直接略過查詢。查詢失敗時保守回 false：寧可不顯示引導，也不要在 API
 * 短暫失敗時硬把玩家導去後台。
 */
export async function resolveNeedsProviderSetup(cloudMode: boolean): Promise<boolean> {
  if (cloudMode) return false
  try {
    const providerIds = await listProviders()
    return shouldGuideProviderSetup({ cloudMode, providerIds })
  } catch {
    return false
  }
}

/**
 * 判斷是否需要顯示「幫不同任務配不同模型」的次要引導卡片。
 *
 * 與 `shouldGuideProviderSetup()` 互斥：只有在已經有至少一個真實 provider、
 * 但使用者還沒顯式設定過任何 feature-model 路由偏好時才需要顯示。
 *
 * 後端目前沒有明確的「是否為預設值」欄位可用，因此這裡採用近似判斷：
 * 只要有任一語意群組被顯式覆寫（`group.model` 非 null）、或全域 fallback
 * model 已被顯式設定（`activeModel.provider_id`/`model_id` 皆非 null），
 * 就視為「已設定」。這是近似邏輯，不是精確語意判斷——按 plan 的風險評估，
 * 兩種誤判方向的代價不對稱（漏顯示的代價 > 多顯示的代價），所以刻意偏向
 * 「找不到明確證據時，就當作未設定」。
 */
export function resolveNeedsRoutingSetup(input: {
  cloudMode: boolean
  providerIds: readonly string[]
  groups: readonly Pick<FeatureModelGroupSummary, 'model'>[]
  activeModel: Pick<ActiveModelPreference, 'provider_id' | 'model_id'> | null
}): boolean {
  if (input.cloudMode) return false
  if (countRealProviders(input.providerIds) === 0) return false

  const hasGroupOverride = input.groups.some(group => group.model !== null)
  const hasActiveModelOverride = Boolean(
    input.activeModel?.provider_id && input.activeModel?.model_id,
  )
  const routingConfigured = hasGroupOverride || hasActiveModelOverride
  return !routingConfigured
}

/**
 * 查詢 provider 清單、群組路由偏好與全域 fallback 偏好，回傳是否需要顯示
 * 「路由設定」次要卡片。cloud 模式與查詢失敗時都回 false——這是非阻斷式
 * 次要卡片，查詢失敗時寧可不顯示，也不要在首頁噴錯。
 */
export async function resolveNeedsRoutingSetupFromApi(input: {
  cloudMode: boolean
  providerIds: readonly string[]
  loadRoutingState: () => Promise<{
    groups: readonly Pick<FeatureModelGroupSummary, 'model'>[]
    activeModel: Pick<ActiveModelPreference, 'provider_id' | 'model_id'> | null
  }>
}): Promise<boolean> {
  if (input.cloudMode) return false
  if (countRealProviders(input.providerIds) === 0) return false
  try {
    const { groups, activeModel } = await input.loadRoutingState()
    return resolveNeedsRoutingSetup({
      cloudMode: input.cloudMode,
      providerIds: input.providerIds,
      groups,
      activeModel,
    })
  } catch {
    return false
  }
}
