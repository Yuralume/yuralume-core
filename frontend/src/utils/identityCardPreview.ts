import type { ScheduleInvolvementPolicy } from '@/types/character'
import type { IdentityCardContent } from '@/utils/api/identityCards'

/**
 * 設定頁「玩家身分卡」管理面的唯讀預覽（IC3）——欄位標籤沿用精靈既有的
 * i18n 鍵，不另造一套。這裡只描述「12 欄怎麼渲染」，不碰 DOM／i18n 的
 * `t()` 呼叫本身，因此可以在沒有 Vue context 的 vitest 環境直接測。
 */

export type IdentityCardPreviewFieldKind = 'text' | 'boolean' | 'enum'

export interface IdentityCardPreviewField {
  field: keyof IdentityCardContent
  /** 沿用 `characterCreate.initialRelationship.*` / `playerSidebar.*` 既有標籤鍵。 */
  labelKey: string
  kind: IdentityCardPreviewFieldKind
}

/** 12 欄，順序即預覽渲染順序——與精靈表單的填寫順序一致。 */
export const IDENTITY_CARD_PREVIEW_FIELDS: readonly IdentityCardPreviewField[] = [
  { field: 'relationship_label', labelKey: 'characterCreate.initialRelationship.relationshipLabel', kind: 'text' },
  { field: 'user_address_name', labelKey: 'characterCreate.initialRelationship.userAddress', kind: 'text' },
  { field: 'character_address_name', labelKey: 'characterCreate.initialRelationship.characterAddress', kind: 'text' },
  { field: 'known_context', labelKey: 'characterCreate.initialRelationship.knownContext', kind: 'text' },
  { field: 'living_arrangement', labelKey: 'characterCreate.initialRelationship.livingArrangement', kind: 'text' },
  { field: 'tone_distance', labelKey: 'characterCreate.initialRelationship.toneDistance', kind: 'text' },
  { field: 'familiarity_boundary', labelKey: 'characterCreate.initialRelationship.boundary', kind: 'text' },
  { field: 'schedule_involvement_policy', labelKey: 'characterCreate.initialRelationship.scheduleLabel', kind: 'enum' },
  { field: 'proactive_permission', labelKey: 'characterCreate.initialRelationship.proactivePermission', kind: 'boolean' },
  { field: 'proactive_cadence_hint', labelKey: 'characterCreate.initialRelationship.proactiveCadenceLabel', kind: 'text' },
  { field: 'user_profile_notes', labelKey: 'playerSidebar.relationshipSeed.notesLabel', kind: 'text' },
  { field: 'persona_note', labelKey: 'playerPersonaNote.fieldLabel', kind: 'text' },
] as const

const SCHEDULE_POLICY_LABEL_KEY: Record<ScheduleInvolvementPolicy, string> = {
  none: 'characterCreate.initialRelationship.scheduleOptions.none',
  mention_only: 'characterCreate.initialRelationship.scheduleOptions.mentionOnly',
  invite_required: 'characterCreate.initialRelationship.scheduleOptions.inviteRequired',
  shared_allowed: 'characterCreate.initialRelationship.scheduleOptions.sharedAllowed',
}

export type IdentityCardPreviewCell =
  | { kind: 'text'; value: string }
  /** 呼叫端自己 `t(key)`——這裡不碰 i18n。 */
  | { kind: 'i18nKey'; key: string }

/** 一個欄位在一張卡上的渲染值。布林／enum 回一個要呼叫端自己翻譯的 key。 */
export function identityCardPreviewCell(
  field: IdentityCardPreviewField,
  card: IdentityCardContent,
): IdentityCardPreviewCell {
  if (field.kind === 'boolean') {
    return {
      kind: 'i18nKey',
      key: card[field.field]
        ? 'identityCard.manage.preview.enabled'
        : 'identityCard.manage.preview.disabled',
    }
  }
  if (field.kind === 'enum') {
    const policy = card.schedule_involvement_policy
    return { kind: 'i18nKey', key: SCHEDULE_POLICY_LABEL_KEY[policy] ?? SCHEDULE_POLICY_LABEL_KEY.none }
  }
  return { kind: 'text', value: String(card[field.field] ?? '') }
}
