import type {
  IdentityCardContent,
  IdentityCardCreateRequest,
} from '@/utils/api/identityCards'
import { isIdentityCardNameConflict } from '@/utils/api/identityCards'

/**
 * 建角成功「之後」才做得到的兩件事（IC2）。
 *
 * 精靈送出時角色還不存在，所以「寫玩家人設補充」需要角色 id、只能等建角回
 * 來才做；「存成身分卡」則是刻意排在成功之後——建卡失敗不該讓玩家以為角色
 * 沒建成。
 *
 * **兩步都不回滾角色**：角色已經建好了，這裡任何一步失敗都只是少寫了一份
 * 附帶資料，語意上是「知情接受的非交易性」（IC 計畫 §2.3）。所以這支函式
 * 永遠不 throw，只把每一步的結果回報給呼叫端去提示重試。
 *
 * 純邏輯、依賴注入：真正的 HTTP 與提示在
 * `composables/useCharacterCreationFollowUp.ts`，這裡只管順序與分支，於是
 * 「空人設不打 API」「409 才問覆蓋」「拒絕覆蓋就不寫」這些規則測得到。
 */

export interface CharacterCreationFollowUp {
  /** 精靈「你的人設」欄的現值；空字串＝不寫 note。 */
  personaNote: string
  /** 勾了「存成身分卡」時的卡名；`null`＝不存卡。 */
  saveCardName: string | null
  /** 送出當下的表單快照（存卡用）。 */
  cardContent: IdentityCardContent
}

export type FollowUpStepStatus =
  /** 這一步本來就沒有要做（欄位空白／沒勾存卡）。 */
  | 'skipped'
  | 'done'
  /** 玩家在覆蓋確認裡按了取消——不是錯誤，不必提示重試。 */
  | 'declined'
  | 'failed'

export interface CharacterCreationFollowUpOutcome {
  note: FollowUpStepStatus
  noteError: unknown
  card: FollowUpStepStatus
  cardError: unknown
}

export interface CharacterCreationFollowUpDeps {
  writeNote(characterId: string, note: string): Promise<unknown>
  createCard(body: IdentityCardCreateRequest): Promise<unknown>
  /** 同名卡已存在時問玩家要不要覆蓋；`false`＝放棄存卡。 */
  confirmOverwrite(name: string): Promise<boolean>
}

export function emptyIdentityCardContent(): IdentityCardContent {
  return {
    relationship_label: '',
    known_context: '',
    living_arrangement: '',
    user_address_name: '',
    character_address_name: '',
    tone_distance: '',
    familiarity_boundary: '',
    schedule_involvement_policy: 'none',
    proactive_permission: false,
    proactive_cadence_hint: '',
    user_profile_notes: '',
    persona_note: '',
  }
}

/** 什麼都不做的後續工作——精靈的「略過」走這條。 */
export function emptyCharacterCreationFollowUp(): CharacterCreationFollowUp {
  return {
    personaNote: '',
    saveCardName: null,
    cardContent: emptyIdentityCardContent(),
  }
}

/**
 * 這包後續工作有沒有事情要做。
 *
 * 呼叫端用它在建角流程上維持「完全不碰人設欄與存卡勾選時，送出路徑與加這
 * 個功能之前逐字等價」——零額外請求，不只是「請求送了但沒作用」。
 */
export function hasCharacterCreationFollowUpWork(
  followUp: CharacterCreationFollowUp,
): boolean {
  return Boolean(followUp.personaNote.trim() || followUp.saveCardName?.trim())
}

export async function runCharacterCreationFollowUp(
  characterId: string,
  followUp: CharacterCreationFollowUp,
  deps: CharacterCreationFollowUpDeps,
): Promise<CharacterCreationFollowUpOutcome> {
  const outcome: CharacterCreationFollowUpOutcome = {
    note: 'skipped',
    noteError: null,
    card: 'skipped',
    cardError: null,
  }

  const note = followUp.personaNote.trim()
  if (note) {
    try {
      await deps.writeNote(characterId, note)
      outcome.note = 'done'
    } catch (error) {
      // 不回滾角色——只記下來讓呼叫端提示重試。
      outcome.note = 'failed'
      outcome.noteError = error
    }
  }

  const cardName = followUp.saveCardName?.trim() ?? ''
  if (cardName) {
    Object.assign(outcome, await saveCard(cardName, followUp.cardContent, deps))
  }

  return outcome
}

async function saveCard(
  name: string,
  content: IdentityCardContent,
  deps: CharacterCreationFollowUpDeps,
): Promise<Pick<CharacterCreationFollowUpOutcome, 'card' | 'cardError'>> {
  try {
    await deps.createCard({ ...content, name })
    return { card: 'done', cardError: null }
  } catch (error) {
    if (!isIdentityCardNameConflict(error)) {
      return { card: 'failed', cardError: error }
    }
    // 同名卡已存在：問過玩家才覆蓋。第一次請求刻意不帶 overwrite，所以在
    // 玩家點頭之前，既有那張卡的內容一定還在。
    const confirmed = await deps.confirmOverwrite(name)
    if (!confirmed) return { card: 'declined', cardError: null }
    try {
      await deps.createCard({ ...content, name, overwrite: true })
      return { card: 'done', cardError: null }
    } catch (overwriteError) {
      return { card: 'failed', cardError: overwriteError }
    }
  }
}
