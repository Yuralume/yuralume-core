/**
 * IC3 — 玩家身分卡的「從既有角色回存」入口與設定頁管理面的接線。
 *
 * 這個 repo 沒有 jsdom / @vue/test-utils（見 `identityCardWizard.test.ts`
 * 檔頭），掛不了元件，所以元件層一律用原始碼掃描釘住：接了哪支 composable
 * ／哪個 API、掛在哪個父層、順序對不對。真正的分支邏輯已經在
 * `identityCardSaveFromCharacter.test.ts` / `identityCardManager.test.ts`
 * 用純函式測過。
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { removeIdentityCardById } from '@/utils/identityCardManager'
import type { IdentityCard } from '@/utils/api/identityCards'

/**
 * Line endings normalised on the way in: several markers below are written as
 * multi-line strings, and a working tree checked out with CRLF would never
 * match one — a failure that says "the wiring is gone" when the wiring is
 * right there. Same treatment as `chatTurnIsolation.test.ts`.
 */
function source(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8')
    .replace(/\r\n/g, '\n')
}

const SAVE_ENTRY = source('../src/components/IdentityCardSaveFromCharacter.vue')
const CHARACTER_SETTINGS = source('../src/components/CharacterSettingsSection.vue')
const MANAGER_PANEL = source('../src/components/IdentityCardManagerPanel.vue')
const PERSONAL_SETTINGS = source('../src/components/PersonalSettingsSection.vue')
const PREVIEW_DIALOG = source('../src/components/IdentityCardPreviewDialog.vue')

function card(overrides: Partial<IdentityCard> = {}): IdentityCard {
  return {
    id: 'card-1',
    operator_id: 'op-1',
    name: '上班族的我',
    created_at: '2026-08-27T00:00:00+00:00',
    updated_at: '2026-08-27T00:00:00+00:00',
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
    ...overrides,
  }
}

describe('「從既有角色回存」入口掛在關係 seed 與人設兩塊 CollapsibleSection 共同的層級', () => {
  it('CharacterSettingsSection 掛了入口元件，不是塞進任一邊的編輯器裡', () => {
    expect(CHARACTER_SETTINGS).toContain(
      "import IdentityCardSaveFromCharacter from './IdentityCardSaveFromCharacter.vue'",
    )
    expect(CHARACTER_SETTINGS).toContain('<IdentityCardSaveFromCharacter')
    // 兩個既有編輯器本身沒有被改動去認識身分卡——入口是外掛的第三個元件。
    const relEditor = source('../src/components/InitialRelationshipSettingsEditor.vue')
    const noteEditor = source('../src/components/PlayerPersonaNoteSetting.vue')
    expect(relEditor).not.toContain('identityCard')
    expect(noteEditor).not.toContain('identityCard')
  })

  it('入口出現在人設 CollapsibleSection 之後、其他區塊之前——不是嵌在裡面', () => {
    // `DispositionAdminEditor` 也出現在檔頭的 import 陳述句裡，所以要從
    // `entryAt` 之後才開始找它在**模板**裡的用法，不然會撈到 import 那行。
    const noteSectionAt = CHARACTER_SETTINGS.indexOf('<PlayerPersonaNoteSetting')
    const entryAt = CHARACTER_SETTINGS.indexOf('<IdentityCardSaveFromCharacter')
    const dispositionAt = CHARACTER_SETTINGS.indexOf('<DispositionAdminEditor', entryAt)
    expect(noteSectionAt).toBeGreaterThan(-1)
    expect(entryAt).toBeGreaterThan(noteSectionAt)
    expect(dispositionAt).toBeGreaterThan(entryAt)
  })
})

describe('入口用的是已儲存的值——按下當下重新 GET，不是讀編輯中的表單草稿', () => {
  it('讀值走既有的兩支 GET（關係 seed／玩家人設），不是任何 form ref', () => {
    expect(SAVE_ENTRY).toContain("import { getInitialRelationship } from '@/utils/api/initialRelationship'")
    expect(SAVE_ENTRY).toContain("import { getPlayerPersonaNote } from '@/utils/api/playerPersonaNote'")
    expect(SAVE_ENTRY).toContain('loadSeed: characterId => getInitialRelationship(characterId)')
    expect(SAVE_ENTRY).toContain('loadPersonaNote: characterId => getPlayerPersonaNote(characterId)')
    // 不依賴另外兩個編輯器的 composable——那兩份是「編輯中」狀態，不是「已存檔」。
    expect(SAVE_ENTRY).not.toContain('useInitialRelationshipSettings')
    expect(SAVE_ENTRY).not.toContain('usePlayerPersonaNote')
  })

  it('回存的順序（讀值 → 建內容 → 撞名才問覆蓋）走共用的純函式，不是自己重刻一份', () => {
    expect(SAVE_ENTRY).toContain(
      "import {\n  saveIdentityCardFromCharacter,\n  type SaveIdentityCardFromCharacterDeps,\n} from '@/utils/identityCardSaveFromCharacter'",
    )
    expect(SAVE_ENTRY).toContain('await saveIdentityCardFromCharacter(props.character.id, trimmed, deps)')
  })

  it('撞名覆蓋走既有的 identityCard.overwrite.* 文案，不另造一套確認對話框', () => {
    expect(SAVE_ENTRY).toContain("t('identityCard.overwrite.title')")
    expect(SAVE_ENTRY).toContain("t('identityCard.overwrite.content'")
    expect(SAVE_ENTRY).toContain("t('identityCard.overwrite.ok')")
  })

  it('上限已達顯示既有的 followUp.limitReached 文案，不另造一套', () => {
    expect(SAVE_ENTRY).toContain("t('identityCard.followUp.limitReached'")
    expect(SAVE_ENTRY).toContain('isIdentityCardLimitReached(outcome.error)')
  })
})

describe('管理面掛在設定頁的個人分頁，因為身分卡是 operator 層級（跨角色）的資料', () => {
  it('PersonalSettingsSection 掛了管理面元件，包在自己的 CollapsibleSection', () => {
    expect(PERSONAL_SETTINGS).toContain(
      "import IdentityCardManagerPanel from './IdentityCardManagerPanel.vue'",
    )
    expect(PERSONAL_SETTINGS).toContain('<IdentityCardManagerPanel')
    expect(PERSONAL_SETTINGS).toContain("t('identityCard.manage.title')")
  })

  it('管理面不是掛在角色分頁——CharacterSettingsSection 沒有它', () => {
    expect(CHARACTER_SETTINGS).not.toContain('IdentityCardManagerPanel')
  })
})

describe('管理面接線：清單 / 改名 / 刪除 / 預覽', () => {
  it('用共用的 usePlayerIdentityCards，掛載時就載入清單', () => {
    expect(MANAGER_PANEL).toContain(
      "import { usePlayerIdentityCards } from '@/composables/usePlayerIdentityCards'",
    )
    expect(MANAGER_PANEL).toContain('onMounted(() => {\n  void load()\n})')
  })

  it('改名撞名時用共用的 isIdentityCardNameConflict 分辨錯誤種類', () => {
    expect(MANAGER_PANEL).toContain('isIdentityCardNameConflict(err)')
  })

  it('刪除前一定先跑確認對話框，且標成危險動作', () => {
    const deleteFnAt = MANAGER_PANEL.indexOf('async function confirmDeleteCard')
    const confirmDialogAt = MANAGER_PANEL.indexOf('await confirmDialog(', deleteFnAt)
    const removeCallAt = MANAGER_PANEL.indexOf('await remove(card.id)', deleteFnAt)
    expect(deleteFnAt).toBeGreaterThan(-1)
    expect(confirmDialogAt).toBeGreaterThan(deleteFnAt)
    expect(removeCallAt).toBeGreaterThan(confirmDialogAt)
    expect(MANAGER_PANEL).toContain('danger: true')
  })

  it('預覽走共用的唯讀 dialog，不是另刻一份表單', () => {
    expect(MANAGER_PANEL).toContain(
      "import IdentityCardPreviewDialog from './IdentityCardPreviewDialog.vue'",
    )
    expect(MANAGER_PANEL).toContain('<IdentityCardPreviewDialog')
  })

  it('第一版沒有內容編輯：管理面不含任何寫入 12 欄內容的表單欄位', () => {
    expect(MANAGER_PANEL).not.toContain('field-textarea')
    expect(MANAGER_PANEL).not.toContain('UiTextarea')
    expect(MANAGER_PANEL).not.toContain('UiSelect')
  })

  it('上限有明確文案：達到上限時顯示 limitNote', () => {
    expect(MANAGER_PANEL).toContain('atLimit')
    expect(MANAGER_PANEL).toContain("t('identityCard.manage.limitNote')")
  })

  it('預覽 dialog 的欄位順序與標籤鍵沿用精靈既有的 12 欄，不另造一套', () => {
    expect(PREVIEW_DIALOG).toContain(
      "import { IDENTITY_CARD_PREVIEW_FIELDS, identityCardPreviewCell } from '@/utils/identityCardPreview'",
    )
    // 欄位空值時用既有的 common.fallback.notSet，不是自己发明一個「未填」字串。
    expect(PREVIEW_DIALOG).toContain("t('common.fallback.notSet')")
  })
})

describe('刪卡不影響已用該卡建立的角色（快照語意）', () => {
  it('本地移除卡片只動卡片清單，角色端的 seed／note 資料結構原封不動', () => {
    const characterSeedSnapshot = { relationship_label: '青梅竹馬', character_address_name: '澪' }
    const characterNoteSnapshot = { note: '我是看得見情緒顏色的超能力者' }

    const cards = [card({ id: 'card-1' }), card({ id: 'card-2' })]
    const next = removeIdentityCardById(cards, 'card-1')

    expect(next.map(c => c.id)).toEqual(['card-2'])
    // 刪卡的操作對象只有卡片清單本身——角色端這兩份快照完全沒被碰過。
    expect(characterSeedSnapshot).toEqual({ relationship_label: '青梅竹馬', character_address_name: '澪' })
    expect(characterNoteSnapshot).toEqual({ note: '我是看得見情緒顏色的超能力者' })
  })

  it('管理面的刪除流程只呼叫身分卡 API，不 import 任何角色 seed／note 端點', () => {
    expect(MANAGER_PANEL).not.toContain('initialRelationship')
    expect(MANAGER_PANEL).not.toContain('playerPersonaNote')
    expect(MANAGER_PANEL).not.toContain('updateInitialRelationship')
  })

  it('後端 migration 已經把這條紅線釘死在資料層——卡片表零 character 參照欄', () => {
    const migration = source('../../alembic/versions/c7v3n1k10052_player_identity_cards.py')
    // 檔頭的說明文字本身會提到「沒有 character_id 欄」，所以這裡只認真的
    // 欄位定義／外鍵，不是隨口一句 docstring。
    expect(migration).not.toContain('sa.Column("character_id"')
    expect(migration).not.toContain('"characters.id"')
  })
})

describe('回存入口：空角色不送出空卡', () => {
  it('讀值成功但沒有可存的設定時顯示 empty 文案，不是通用的 cardSaveFailed', () => {
    expect(SAVE_ENTRY).toContain("outcome.status === 'empty'")
    expect(SAVE_ENTRY).toContain("t('identityCard.saveFromCharacter.empty')")
  })
})

describe('名稱長度閘：回存入口與管理面改名都比照精靈的 canSubmit，不等後端 422', () => {
  it('回存入口的送出鍵用 nameInvalid（空白或超過上限）', () => {
    expect(SAVE_ENTRY).toContain('IDENTITY_CARD_NAME_MAX_CHARS')
    expect(SAVE_ENTRY).toContain(':disabled="nameInvalid"')
    expect(SAVE_ENTRY).not.toContain(':disabled="!name.trim()"')
  })

  it('管理面改名的送出鍵用 renameNameInvalid，且顯示上限 hint', () => {
    expect(MANAGER_PANEL).toContain('IDENTITY_CARD_NAME_MAX_CHARS')
    expect(MANAGER_PANEL).toContain(':disabled="renameNameInvalid"')
    expect(MANAGER_PANEL).not.toContain(':disabled="!renameDraft.trim()"')
    expect(MANAGER_PANEL).toContain("t('identityCard.manage.rename.hint'")
  })
})

describe('hint 只渲染一次：CollapsibleSection 的 :hint 與面板內部不重複', () => {
  it('PersonalSettingsSection 對 CollapsibleSection 傳了 identityCard.manage.hint', () => {
    expect(PERSONAL_SETTINGS).toContain(':hint="t(\'identityCard.manage.hint\')"')
  })

  it('IdentityCardManagerPanel 內部不再重複渲染同一把 hint', () => {
    const hintOccurrences = (MANAGER_PANEL.match(/identityCard\.manage\.hint/g) ?? []).length
    expect(hintOccurrences).toBe(0)
  })
})

describe('預覽浮窗比照 PlayerPersonaNoteModal 補 Escape 關閉', () => {
  it('掛了 bindEscape／unbindEscape，含 onBeforeUnmount 解綁', () => {
    expect(PREVIEW_DIALOG).toContain('function bindEscape()')
    expect(PREVIEW_DIALOG).toContain('function unbindEscape()')
    expect(PREVIEW_DIALOG).toContain('onBeforeUnmount(unbindEscape)')
    expect(PREVIEW_DIALOG).toContain("event.key === 'Escape'")
    expect(PREVIEW_DIALOG).toContain("emit('close')")
  })
})
