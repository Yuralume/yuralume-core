import type { InitialRelationshipSeed } from '@/utils/api/initialRelationship'
import type {
  IdentityCard,
  IdentityCardContent,
  IdentityCardCreateRequest,
} from '@/utils/api/identityCards'
import { isIdentityCardNameConflict } from '@/utils/api/identityCards'

/**
 * 「從既有角色回存」入口（IC3）：把一位角色**目前已儲存**的關係 seed ＋
 * 玩家人設補充，存成一張身分卡。
 *
 * 用的是已儲存的值，不是編輯中的草稿——語意是「把這個角色現在的設定存成
 * 卡」。所以這裡的輸入是兩支既有 GET 的回應（`InitialRelationshipSeed`／
 * `PlayerPersonaNote.note`），不是任何表單 ref。呼叫端（設定頁的入口元件）
 * 在按下「存成身分卡」的當下才重新 GET 一次，確保帶進卡片的是這一刻的
 * 事實，不是頁面剛開啟時可能已經過期的值。
 *
 * 純邏輯、依賴注入，同 `characterCreationFollowUp.ts` 的既有先例：真正的
 * HTTP 與 i18n 提示在呼叫端元件，這裡只管「讀值 → 建內容 → 撞名才問覆蓋」
 * 這條順序，於是每一種分支都測得到，不必掛元件。
 */

export interface SaveIdentityCardFromCharacterDeps {
  loadSeed(characterId: string): Promise<InitialRelationshipSeed>
  loadPersonaNote(characterId: string): Promise<{ note: string }>
  createCard(body: IdentityCardCreateRequest): Promise<IdentityCard>
  /** 同名卡已存在時問玩家要不要覆蓋；`false`＝放棄存卡。 */
  confirmOverwrite(name: string): Promise<boolean>
}

export type SaveIdentityCardFromCharacterOutcome =
  | { status: 'done'; card: IdentityCard }
  /** 玩家在覆蓋確認裡按了取消——不是錯誤。 */
  | { status: 'declined' }
  /** 讀取現況（seed／人設）失敗，還沒送出 POST。 */
  | { status: 'load_failed'; error: unknown }
  /** 沒有 seed（`has_seed: false`）或逐欄皆空白＋人設也空——不送 POST，不生一張空卡。 */
  | { status: 'empty' }
  | { status: 'save_failed'; error: unknown }

const SEED_TEXT_FIELDS = [
  'relationship_label',
  'known_context',
  'living_arrangement',
  'user_address_name',
  'character_address_name',
  'tone_distance',
  'familiarity_boundary',
  'proactive_cadence_hint',
  'user_profile_notes',
] as const satisfies readonly (keyof InitialRelationshipSeed)[]

/** 一份已儲存的 seed ＋人設現值 → 一張卡的內容。逐欄搬、trim，沒有轉換表。 */
export function buildIdentityCardContentFromSaved(
  seed: InitialRelationshipSeed,
  personaNote: string,
): IdentityCardContent {
  const content = {} as Record<(typeof SEED_TEXT_FIELDS)[number], string>
  for (const field of SEED_TEXT_FIELDS) {
    content[field] = (seed[field] ?? '').trim()
  }
  return {
    ...content,
    schedule_involvement_policy: seed.schedule_involvement_policy,
    proactive_permission: seed.proactive_permission,
    persona_note: personaNote.trim(),
  }
}

/**
 * 這個角色現在有沒有「可存的設定」。
 *
 * `has_seed: false`＝這個角色從沒填過關係 seed（欄位都是後端給的空白預設
 * 值，不是玩家真的填了空字串）；即使 `has_seed` 是 true，逐欄 trim 後也可能
 * 全部空白（例如曾經填過又清空）。這兩種情況＋人設也空，回存出來的會是一
 * 張除了名字什麼都沒有的卡——不是玩家想要的「把現在的設定存起來」，所以
 * 這裡先擋下來，不送 POST。
 */
function isSeedAndNoteEmpty(seed: InitialRelationshipSeed, personaNote: string): boolean {
  if (!seed.has_seed) return true
  const allSeedTextFieldsEmpty = SEED_TEXT_FIELDS.every(field => !(seed[field] ?? '').trim())
  return allSeedTextFieldsEmpty && !personaNote.trim()
}

export async function saveIdentityCardFromCharacter(
  characterId: string,
  name: string,
  deps: SaveIdentityCardFromCharacterDeps,
): Promise<SaveIdentityCardFromCharacterOutcome> {
  let content: IdentityCardContent
  try {
    const [seed, note] = await Promise.all([
      deps.loadSeed(characterId),
      deps.loadPersonaNote(characterId),
    ])
    if (isSeedAndNoteEmpty(seed, note.note)) return { status: 'empty' }
    content = buildIdentityCardContentFromSaved(seed, note.note)
  } catch (error) {
    return { status: 'load_failed', error }
  }

  const trimmedName = name.trim()
  try {
    const card = await deps.createCard({ ...content, name: trimmedName })
    return { status: 'done', card }
  } catch (error) {
    if (!isIdentityCardNameConflict(error)) return { status: 'save_failed', error }
    // 同名卡已存在：問過玩家才覆蓋。第一次請求刻意不帶 overwrite，所以在
    // 玩家點頭之前，既有那張卡的內容一定還在（同 IC2 的既有做法）。
    const confirmed = await deps.confirmOverwrite(trimmedName)
    if (!confirmed) return { status: 'declined' }
    try {
      const card = await deps.createCard({ ...content, name: trimmedName, overwrite: true })
      return { status: 'done', card }
    } catch (overwriteError) {
      return { status: 'save_failed', error: overwriteError }
    }
  }
}
