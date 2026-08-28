/**
 * IC 審查修復 FIX-A —— 從零手動建角這條路也要有身分卡三件套。
 *
 * `CharacterCreateModal`（側欄「＋ 新增角色」開的那個視窗）有一份**自己內嵌
 * 的** 11 欄 seed 表單，不是 `InitialRelationshipWizardModal`。IC2 只把
 * picker／「你的人設」／存卡勾選接上了精靈，於是產品文檔（`03-user-journeys`
 * J1）寫著「創建新角色時可先選一張身分卡帶入」的那條路——也就是新玩家最常
 * 走的主路徑——完全沒有這三件事。既有的接線掃描只掃四個檔，正好把它漏在
 * 外面，所以這個缺口一路綠燈。
 *
 * 這個 repo 沒有 jsdom / @vue/test-utils（見 `characterCardSource.test.ts`
 * 檔頭），掛不了元件，所以分兩層釘：純邏輯層直接呼叫共用模組，接線層用原
 * 始碼掃描釘住「這個檔真的接上了共用模組，而不是又刻了一份」。
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  applyIdentityCardToForm,
  buildIdentityCardContent,
  IDENTITY_CARD_SEED_FIELDS,
} from '@/utils/identityCard'
import { emptyIdentityCardContent } from '@/utils/characterCreationFollowUp'
import type { IdentityCardContent } from '@/utils/api/identityCards'
import { newCharacterInitialRelationshipForm } from '@/composables/useInitialRelationshipForm'

function source(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8')
}

const CREATE_MODAL = source('../src/components/CharacterCreateModal.vue')

function content(overrides: Partial<IdentityCardContent> = {}): IdentityCardContent {
  return { ...emptyIdentityCardContent(), ...overrides }
}

describe('手動建角表單套卡：共用模組，語意與精靈一致', () => {
  it('11 個 seed 欄位填進這份表單，套完之後每一欄照樣可改（快照，不建立連結）', () => {
    const form = newCharacterInitialRelationshipForm()
    const card = content({
      relationship_label: '青梅竹馬',
      known_context: '高中同班三年',
      user_address_name: '阿丹',
      schedule_involvement_policy: 'invite_required',
      proactive_permission: true,
      proactive_cadence_hint: '一天一次就好',
    })

    applyIdentityCardToForm(form, card)
    form.relationship_label = '同事'

    expect(form.known_context).toBe('高中同班三年')
    expect(form.user_address_name).toBe('阿丹')
    expect(form.schedule_involvement_policy).toBe('invite_required')
    expect(form.proactive_cadence_hint).toBe('一天一次就好')
    // 改表單不回寫卡片。
    expect(card.relationship_label).toBe('青梅竹馬')
  })

  it('「有值才覆寫」在這條路一樣成立——玩家在按 picker 之前打的字留得住', () => {
    // picker 掛在 seed 區塊頂部，但這份表單很長，「先往下打了幾欄才想起有卡
    // 可套」是完全正常的順序。空欄＝這張卡對那欄沒意見。
    const form = newCharacterInitialRelationshipForm()
    form.tone_distance = '講話客氣一點'
    form.user_profile_notes = '晚上比較有空'

    applyIdentityCardToForm(form, content({ relationship_label: '青梅竹馬' }))

    expect(form.tone_distance).toBe('講話客氣一點')
    expect(form.user_profile_notes).toBe('晚上比較有空')
  })

  it('卡片不碰 safe_user_profile 那三欄——它們只有這份表單有，卡片沒有', () => {
    // 手動建角表單比精靈多了興趣／作息／期待三欄（建立當下才組成
    // `safe_user_profile`）。卡片沒有這三欄，套卡不該把玩家填的清掉。
    const form = newCharacterInitialRelationshipForm()
    form.profile_interests = '登山, 咖啡'
    form.profile_routine = '早上跑步'
    form.profile_life_goals = '開一間店'

    applyIdentityCardToForm(form, content({ relationship_label: '青梅竹馬' }))

    expect(form.profile_interests).toBe('登山, 咖啡')
    expect(form.profile_routine).toBe('早上跑步')
    expect(form.profile_life_goals).toBe('開一間店')

    const saved = buildIdentityCardContent(form, '')
    expect(Object.keys(saved).sort()).toEqual(
      [...IDENTITY_CARD_SEED_FIELDS, 'persona_note'].sort(),
    )
  })
})

describe('CharacterCreateModal 接線（原始碼掃描）', () => {
  it('掛了 picker，並把選到的卡交給 applyIdentityCard', () => {
    expect(CREATE_MODAL).toContain("import IdentityCardPicker from '@/components/IdentityCardPicker.vue'")
    expect(CREATE_MODAL).toContain('<IdentityCardPicker')
    expect(CREATE_MODAL).toContain('@select="applyIdentityCard"')
    // modal 是 v-if 掛載的：mount ＝開窗，所以恆 true 就等於每次開窗重抓。
    expect(CREATE_MODAL).toContain(':active="true"')
  })

  it('套卡走共用的 applyIdentityCardToForm，不是這個檔自己再刻一份搬運', () => {
    expect(CREATE_MODAL).toContain("from '@/utils/identityCard'")
    expect(CREATE_MODAL).toContain(
      'const answered = applyIdentityCardToForm(initialRelationship.value, card)',
    )
    expect(CREATE_MODAL).toContain('for (const field of answered) removeAnsweredIntakeQuestion(field)')
  })

  it('卡片沒帶人設時不覆寫人設欄——玩家在套卡前打的那段留著', () => {
    expect(CREATE_MODAL).toContain("const personaFromCard = (card.persona_note ?? '').trim()")
    expect(CREATE_MODAL).toContain('if (personaFromCard) personaNote.value = personaFromCard')
  })

  it('有「你的人設」欄，上限鏡像 PP 的常數並顯示字數', () => {
    expect(CREATE_MODAL).toContain("import { PLAYER_PERSONA_NOTE_MAX_CHARS } from '@/utils/api/playerPersonaNote'")
    expect(CREATE_MODAL).toContain(':maxlength="personaNoteMaxChars"')
    expect(CREATE_MODAL).toContain("t('identityCard.personaNote.label')")
    expect(CREATE_MODAL).toContain('personaNoteCounter')
  })

  it('有「存成身分卡」勾選＋卡名欄，長度閘接的是同一個常數', () => {
    expect(CREATE_MODAL).toContain("t('identityCard.save.checkbox')")
    expect(CREATE_MODAL).toContain('v-if="saveAsIdentityCard"')
    expect(CREATE_MODAL).toContain('IDENTITY_CARD_NAME_MAX_CHARS')
    expect(CREATE_MODAL).toContain('return Boolean(name) && name.length <= identityCardNameMaxChars')
  })

  it('勾了存卡卻沒填卡名時擋住建立鍵——否則是一個角色建好了、卡沒存到的 422', () => {
    expect(CREATE_MODAL).toContain(
      ':disabled="!form.name.trim() || intakeLoading || !identityCardNameValid"',
    )
  })

  it('建角成功後走共用 composable，而不是自己再刻一份寫 note／存卡', () => {
    expect(CREATE_MODAL).toContain(
      "import { useCharacterCreationFollowUp } from '@/composables/useCharacterCreationFollowUp'",
    )
    expect(CREATE_MODAL).toContain('useCharacterCreationFollowUp()')
    expect(CREATE_MODAL).not.toContain('updatePlayerPersonaNote(')
    expect(CREATE_MODAL).not.toContain('createIdentityCard(')
  })

  it('後續工作跑在 emit(created) 之前——PP 首彈窗的條件在那之後才判斷', () => {
    const runAt = CREATE_MODAL.indexOf(
      'await characterCreationFollowUp.run(created.id, buildFollowUp())',
    )
    const emitAt = CREATE_MODAL.indexOf("emit('created', created)", runAt)
    expect(runAt).toBeGreaterThan(-1)
    expect(emitAt).toBeGreaterThan(runAt)
  })

  it('存卡內容是送出當下的表單快照，人設另走 PP 的寫入路徑', () => {
    expect(CREATE_MODAL).toContain(
      'cardContent: buildIdentityCardContent(initialRelationship.value, personaNote.value)',
    )
    expect(CREATE_MODAL).toContain('personaNote: personaNote.value.trim()')
    expect(CREATE_MODAL).toContain('saveCardName: saveAsIdentityCard.value')
  })

  it('建完與取消都清掉人設與存卡欄，不把上一位角色的內容留給下一位', () => {
    expect(CREATE_MODAL).toContain('function resetIdentityCardState()')
    // 建立成功後與取消各一次。
    expect(CREATE_MODAL.match(/^\s*resetIdentityCardState\(\)$/gm) ?? []).toHaveLength(2)
    const cancelAt = CREATE_MODAL.indexOf('function cancel()')
    expect(CREATE_MODAL.indexOf('resetIdentityCardState()', cancelAt)).toBeGreaterThan(cancelAt)
  })

  it('關係 seed 仍照原樣送進建角 API，沒有被後續工作改寫', () => {
    expect(CREATE_MODAL).toContain('initial_relationship: buildInitialRelationship(),')
  })
})
