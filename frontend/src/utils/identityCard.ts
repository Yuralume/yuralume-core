import type { InitialRelationshipForm } from '@/composables/useInitialRelationshipForm'
import type { IdentityCardContent } from '@/utils/api/identityCards'

/**
 * 身分卡 ↔ 創角精靈表單的逐欄搬運（IC2）。
 *
 * 兩邊欄名同名同義，所以這裡只做「搬」與「trim」，沒有轉換表——有轉換表就
 * 有漂移的地方。純函式，不碰 API、不碰 i18n，因此可以在沒有 DOM 的 vitest
 * 環境直接測。
 */

/**
 * 卡片帶著的 11 個 seed 欄位。
 *
 * 刻意**不含** `profile_interests` / `profile_routine` / `profile_life_goals`
 * ——那三個只在建立當下組成 `safe_user_profile`，卡片（與 `PATCH
 * /initial-relationship` 的可寫欄位）都沒有它們。
 */
export const IDENTITY_CARD_SEED_FIELDS = [
  'relationship_label',
  'known_context',
  'living_arrangement',
  'user_address_name',
  'character_address_name',
  'tone_distance',
  'familiarity_boundary',
  'schedule_involvement_policy',
  'proactive_permission',
  'proactive_cadence_hint',
  'user_profile_notes',
] as const satisfies readonly (keyof InitialRelationshipForm)[]

const TEXT_SEED_FIELDS = [
  'relationship_label',
  'known_context',
  'living_arrangement',
  'user_address_name',
  'character_address_name',
  'tone_distance',
  'familiarity_boundary',
  'user_profile_notes',
] as const satisfies readonly (keyof InitialRelationshipForm)[]

/**
 * 把一張卡帶進精靈表單，回傳「這次真的被卡填出值來」的欄位名。
 *
 * 回傳值餵給精靈的 `removeAnsweredIntakeQuestion`：卡片已經回答過的欄位，
 * 不該再留著一則追問要玩家回答第二次。空白欄不算回答——卡片沒填的欄位，
 * 追問照樣該留著。
 *
 * ## 覆寫語意：有值才覆寫
 *
 * 一張卡是一份**局部**快照，不是一整張要蓋掉表單的底片。空欄的意思是「這張
 * 卡對這一欄沒意見」，不是「請把這一欄清空」——所以表單裡已經有的值留得住：
 * SillyTavern 轉檔預填的 `known_context`、玩家在按下 picker 之前自己打的
 * 字、上一張卡填過而這張沒填的欄位。整組無條件覆寫的舊語意會把這些無聲清
 * 掉，而 picker 就掛在表單上方，「先打字再想起有卡可套」是完全正常的順序。
 *
 * 兩個刻意的例外，理由都是「這個欄位的 false／none 是明確狀態，不是空值」：
 *
 * - `schedule_involvement_policy` 的 `none` **是**這個列舉的「沒意見」值
 *   （＝表單預設），所以它跟空字串同義，不覆寫。
 * - `proactive_permission` ＋ `proactive_cadence_hint` 維持**整組無條件帶
 *   入**。checkbox 沒有空值：存卡當下不勾就是「不要主動找我」的表態，套卡
 *   時必須蓋掉表單預設的 true，否則玩家會拿到一個他明確關掉過的行為。節奏
 *   提示綁在同一組一起換，免得留下一句屬於上一張卡的描述。
 *
 * 直接改傳入的 form（呼叫端持有的 reactive 物件），與既有
 * `applyInitialRelationshipSuggestion` 同型。
 */
export function applyIdentityCardToForm(
  form: InitialRelationshipForm,
  card: IdentityCardContent,
): string[] {
  const answered: string[] = []

  for (const field of TEXT_SEED_FIELDS) {
    const value = (card[field] ?? '').trim()
    if (!value) continue
    form[field] = value
    answered.push(field)
  }

  const policy = card.schedule_involvement_policy ?? 'none'
  if (policy !== 'none') {
    form.schedule_involvement_policy = policy
    answered.push('schedule_involvement_policy')
  }

  form.proactive_permission = card.proactive_permission === true
  form.proactive_cadence_hint = (card.proactive_cadence_hint ?? '').trim()
  if (form.proactive_permission) answered.push('proactive_permission')
  if (form.proactive_cadence_hint) answered.push('proactive_cadence_hint')

  return answered
}

/** 目前表單 ＋ 人設欄的現值 → 一張卡的內容（存卡用）。 */
export function buildIdentityCardContent(
  form: InitialRelationshipForm,
  personaNote: string,
): IdentityCardContent {
  return {
    relationship_label: form.relationship_label.trim(),
    known_context: form.known_context.trim(),
    living_arrangement: form.living_arrangement.trim(),
    user_address_name: form.user_address_name.trim(),
    character_address_name: form.character_address_name.trim(),
    tone_distance: form.tone_distance.trim(),
    familiarity_boundary: form.familiarity_boundary.trim(),
    schedule_involvement_policy: form.schedule_involvement_policy,
    proactive_permission: form.proactive_permission,
    proactive_cadence_hint: form.proactive_cadence_hint.trim(),
    user_profile_notes: form.user_profile_notes.trim(),
    persona_note: personaNote.trim(),
  }
}
