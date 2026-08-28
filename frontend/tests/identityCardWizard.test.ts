/**
 * IC2 — 玩家身分卡的創角精靈整合。
 *
 * 這個 repo 沒有 jsdom / @vue/test-utils（見 `characterCardSource.test.ts`
 * 檔頭），掛不了元件。所以這裡分兩層釘：
 *   - **純邏輯層**（`utils/identityCard.ts`、`utils/characterCreationFollowUp.ts`
 *     ）直接呼叫，把「選卡填表」「空人設不寫」「撞名覆蓋」「失敗不回滾」這
 *     幾條規則測成真的行為。
 *   - **接線層**用原始碼掃描，比照 `adminCharacterCardRelationshipWizard.test.ts`
 *     的既有做法，釘住精靈與三個呼叫端有沒有真的接上。
 */

import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  applyIdentityCardToForm,
  buildIdentityCardContent,
  IDENTITY_CARD_SEED_FIELDS,
} from '@/utils/identityCard'
import {
  emptyCharacterCreationFollowUp,
  emptyIdentityCardContent,
  hasCharacterCreationFollowUpWork,
  runCharacterCreationFollowUp,
  type CharacterCreationFollowUpDeps,
} from '@/utils/characterCreationFollowUp'
import { IDENTITY_CARD_NAME_CONFLICT_CODE } from '@/utils/api/identityCards'
import type { IdentityCard } from '@/utils/api/identityCards'
import {
  buildInitialRelationshipPayload,
  newCharacterInitialRelationshipForm,
} from '@/composables/useInitialRelationshipForm'

function source(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8')
}

const WIZARD = source('../src/components/InitialRelationshipWizardModal.vue')
const PICKER = source('../src/components/IdentityCardPicker.vue')
const PLAYER_PANEL = source('../src/components/PlayerCharacterCardPanel.vue')
const ADMIN_PAGE = source('../src/pages/admin/CharactersAdminPage.vue')
const MARKETPLACE = source('../src/components/admin/CharacterCardMarketplace.vue')
// 第四個入口：側欄「＋ 新增角色」開的手動建角視窗。它有自己內嵌的 seed 表
// 單（不是精靈），IC2 當初漏掉它，而這份掃描清單只列到三個呼叫端，正是那個
// 缺口一路綠燈的原因。細部接線在 `characterCreateIdentityCard.test.ts`。
const CREATE_MODAL = source('../src/components/CharacterCreateModal.vue')

function card(overrides: Partial<IdentityCard> = {}): IdentityCard {
  return {
    id: 'card-1',
    operator_id: 'op-1',
    name: '上班族的我',
    created_at: '2026-08-27T00:00:00+00:00',
    updated_at: '2026-08-27T00:00:00+00:00',
    ...emptyIdentityCardContent(),
    ...overrides,
  }
}

function conflictError(name: string) {
  return {
    response: {
      status: 409,
      data: { detail: { code: IDENTITY_CARD_NAME_CONFLICT_CODE, card_id: 'card-1', name } },
    },
  }
}

function deps(overrides: Partial<CharacterCreationFollowUpDeps> = {}): CharacterCreationFollowUpDeps {
  return {
    writeNote: vi.fn().mockResolvedValue({}),
    createCard: vi.fn().mockResolvedValue({}),
    confirmOverwrite: vi.fn().mockResolvedValue(true),
    ...overrides,
  }
}

describe('套卡＝把卡的內容複製進表單（快照，不建立連結）', () => {
  it('11 個 seed 欄位逐欄填進表單', () => {
    const form = newCharacterInitialRelationshipForm()
    applyIdentityCardToForm(form, card({
      relationship_label: '青梅竹馬',
      known_context: '高中同班三年',
      living_arrangement: '住在隔壁棟',
      user_address_name: '阿丹',
      character_address_name: '澪',
      tone_distance: '不用敬語',
      familiarity_boundary: '不聊工作',
      schedule_involvement_policy: 'invite_required',
      proactive_permission: true,
      proactive_cadence_hint: '一天一次就好',
      user_profile_notes: '晚上比較有空',
    }))

    expect(form.relationship_label).toBe('青梅竹馬')
    expect(form.known_context).toBe('高中同班三年')
    expect(form.living_arrangement).toBe('住在隔壁棟')
    expect(form.user_address_name).toBe('阿丹')
    expect(form.character_address_name).toBe('澪')
    expect(form.tone_distance).toBe('不用敬語')
    expect(form.familiarity_boundary).toBe('不聊工作')
    expect(form.schedule_involvement_policy).toBe('invite_required')
    expect(form.proactive_permission).toBe(true)
    expect(form.proactive_cadence_hint).toBe('一天一次就好')
    expect(form.user_profile_notes).toBe('晚上比較有空')
  })

  it('回傳「卡片真的填出值來」的欄位，供精靈撤掉對應的 intake 追問', () => {
    const form = newCharacterInitialRelationshipForm()
    const answered = applyIdentityCardToForm(form, card({
      relationship_label: '青梅竹馬',
      schedule_involvement_policy: 'shared_allowed',
      proactive_permission: true,
    }))

    expect(answered).toContain('relationship_label')
    expect(answered).toContain('schedule_involvement_policy')
    expect(answered).toContain('proactive_permission')
    // 卡片沒填的欄位不算被回答——那則追問該留著。
    expect(answered).not.toContain('known_context')
    expect(answered).not.toContain('tone_distance')
  })

  it('卡片是空的就不會謊報回答過——policy 是 none、permission 是 false 都不算', () => {
    const form = newCharacterInitialRelationshipForm()
    expect(applyIdentityCardToForm(form, card())).toEqual([])
  })

  it('填進來之後改表單不會回寫卡片（快照語意）', () => {
    const form = newCharacterInitialRelationshipForm()
    const original = card({ relationship_label: '青梅竹馬' })
    applyIdentityCardToForm(form, original)
    form.relationship_label = '同事'
    expect(original.relationship_label).toBe('青梅竹馬')
  })

  it('套卡把 proactive_permission 覆寫成卡片的值——包含創角預設 true 要被卡片的 false 蓋掉', () => {
    const form = newCharacterInitialRelationshipForm()
    expect(form.proactive_permission).toBe(true)
    applyIdentityCardToForm(form, card({ proactive_permission: false }))
    expect(form.proactive_permission).toBe(false)
  })

  it('proactive 那一組整組帶入：cadence hint 跟著換掉，不留上一張卡的節奏描述', () => {
    const form = newCharacterInitialRelationshipForm()
    form.proactive_cadence_hint = '一天一次就好'
    applyIdentityCardToForm(form, card({ proactive_permission: false }))
    expect(form.proactive_cadence_hint).toBe('')
  })

  it('卡片沒填的文字欄不覆寫——表單裡既有的值留著（空欄＝沒意見，不是「清空」）', () => {
    const form = newCharacterInitialRelationshipForm()
    form.relationship_label = '同事'
    form.tone_distance = '講話客氣一點'

    applyIdentityCardToForm(form, card({ known_context: '高中同班三年' }))

    expect(form.known_context).toBe('高中同班三年')
    expect(form.relationship_label).toBe('同事')
    expect(form.tone_distance).toBe('講話客氣一點')
  })

  it('SillyTavern 轉檔預填的 known_context 不會被一張沒填該欄的卡洗掉', () => {
    // 精靈開窗時把 `suggestedKnownContext` 填進 known_context（D5），玩家接
    // 著在同一個表單上套卡——卡片沒帶這欄時預填必須留著。
    const form = newCharacterInitialRelationshipForm()
    form.known_context = '轉檔過來的中性場景描述'

    applyIdentityCardToForm(form, card({ relationship_label: '青梅竹馬' }))

    expect(form.known_context).toBe('轉檔過來的中性場景描述')
  })

  it('卡片的 schedule 是 none 就不覆寫——none 是這個列舉的「沒意見」值', () => {
    const form = newCharacterInitialRelationshipForm()
    form.schedule_involvement_policy = 'shared_allowed'
    applyIdentityCardToForm(form, card({ schedule_involvement_policy: 'none' }))
    expect(form.schedule_involvement_policy).toBe('shared_allowed')
  })

  it('answered 只列真的被卡覆寫出值的欄，沒填的欄不會謊報成已回答', () => {
    const form = newCharacterInitialRelationshipForm()
    form.tone_distance = '講話客氣一點'

    const answered = applyIdentityCardToForm(form, card({ known_context: '高中同班三年' }))

    expect(answered).toEqual(['known_context'])
  })

  it('存卡收的是 11 欄＋人設，不含只在建立當下用的 safe_user_profile 三欄', () => {
    const form = newCharacterInitialRelationshipForm()
    form.relationship_label = ' 青梅竹馬 '
    form.profile_interests = '登山, 咖啡'
    form.profile_routine = '早上跑步'
    form.profile_life_goals = '開一間店'

    const content = buildIdentityCardContent(form, '  我看得見情緒的顏色  ')

    expect(content.relationship_label).toBe('青梅竹馬')
    expect(content.persona_note).toBe('我看得見情緒的顏色')
    expect(Object.keys(content).sort()).toEqual(
      [...IDENTITY_CARD_SEED_FIELDS, 'persona_note'].sort(),
    )
  })
})

describe('建角後的補寫：人設 note', () => {
  it('人設留空就完全不打 PUT——不是送一個空字串把既有內容清掉', async () => {
    const d = deps()
    const outcome = await runCharacterCreationFollowUp('c1', {
      personaNote: '   ',
      saveCardName: null,
      cardContent: emptyIdentityCardContent(),
    }, d)

    expect(d.writeNote).not.toHaveBeenCalled()
    expect(outcome.note).toBe('skipped')
  })

  it('人設有值就以既有 PP PUT 寫入', async () => {
    const d = deps()
    const outcome = await runCharacterCreationFollowUp('c1', {
      personaNote: '  我看得見情緒的顏色  ',
      saveCardName: null,
      cardContent: emptyIdentityCardContent(),
    }, d)

    expect(d.writeNote).toHaveBeenCalledWith('c1', '我看得見情緒的顏色')
    expect(outcome.note).toBe('done')
  })

  it('寫入失敗不 throw、不回滾角色——只回報失敗讓呼叫端提示重試', async () => {
    const failure = new Error('boom')
    const d = deps({ writeNote: vi.fn().mockRejectedValue(failure) })

    const outcome = await runCharacterCreationFollowUp('c1', {
      personaNote: '我看得見情緒的顏色',
      saveCardName: '上班族的我',
      cardContent: emptyIdentityCardContent(),
    }, d)

    expect(outcome.note).toBe('failed')
    expect(outcome.noteError).toBe(failure)
    // 角色與存卡都不受 note 失敗影響：存卡照樣進行。
    expect(d.createCard).toHaveBeenCalled()
    expect(outcome.card).toBe('done')
  })
})

describe('建角後的補寫：存成身分卡', () => {
  it('沒勾存卡就完全不打 POST', async () => {
    const d = deps()
    const outcome = await runCharacterCreationFollowUp('c1', {
      personaNote: '我看得見情緒的顏色',
      saveCardName: null,
      cardContent: emptyIdentityCardContent(),
    }, d)

    expect(d.createCard).not.toHaveBeenCalled()
    expect(outcome.card).toBe('skipped')
  })

  it('第一次送出刻意不帶 overwrite——玩家點頭之前既有那張卡不會被動到', async () => {
    const d = deps()
    await runCharacterCreationFollowUp('c1', {
      personaNote: '',
      saveCardName: ' 上班族的我 ',
      cardContent: { ...emptyIdentityCardContent(), relationship_label: '青梅竹馬' },
    }, d)

    expect(d.createCard).toHaveBeenCalledTimes(1)
    expect(d.createCard).toHaveBeenCalledWith({
      ...emptyIdentityCardContent(),
      relationship_label: '青梅竹馬',
      name: '上班族的我',
    })
    expect(d.confirmOverwrite).not.toHaveBeenCalled()
  })

  it('撞名 409 → 問覆蓋 → 同意就帶 overwrite 重送', async () => {
    const createCard = vi.fn()
      .mockRejectedValueOnce(conflictError('上班族的我'))
      .mockResolvedValueOnce({})
    const d = deps({ createCard })

    const outcome = await runCharacterCreationFollowUp('c1', {
      personaNote: '',
      saveCardName: '上班族的我',
      cardContent: emptyIdentityCardContent(),
    }, d)

    expect(d.confirmOverwrite).toHaveBeenCalledWith('上班族的我')
    expect(createCard).toHaveBeenCalledTimes(2)
    expect(createCard.mock.calls[1][0]).toMatchObject({ name: '上班族的我', overwrite: true })
    expect(outcome.card).toBe('done')
  })

  it('撞名但玩家按取消 → 不重送、不算失敗（不該再彈一顆重試鍵）', async () => {
    const createCard = vi.fn().mockRejectedValueOnce(conflictError('上班族的我'))
    const d = deps({ createCard, confirmOverwrite: vi.fn().mockResolvedValue(false) })

    const outcome = await runCharacterCreationFollowUp('c1', {
      personaNote: '',
      saveCardName: '上班族的我',
      cardContent: emptyIdentityCardContent(),
    }, d)

    expect(createCard).toHaveBeenCalledTimes(1)
    expect(outcome.card).toBe('declined')
    expect(outcome.cardError).toBeNull()
  })

  it('非撞名的失敗直接回報，不會誤問覆蓋', async () => {
    const failure = new Error('500')
    const d = deps({ createCard: vi.fn().mockRejectedValue(failure) })

    const outcome = await runCharacterCreationFollowUp('c1', {
      personaNote: '',
      saveCardName: '上班族的我',
      cardContent: emptyIdentityCardContent(),
    }, d)

    expect(d.confirmOverwrite).not.toHaveBeenCalled()
    expect(outcome.card).toBe('failed')
    expect(outcome.cardError).toBe(failure)
  })
})

describe('負向：完全不碰身分卡與人設欄時，建角送出與現況等價', () => {
  it('空的後續工作＝零請求（不是「請求送了但沒作用」）', async () => {
    const d = deps()
    const empty = emptyCharacterCreationFollowUp()

    expect(hasCharacterCreationFollowUpWork(empty)).toBe(false)
    const outcome = await runCharacterCreationFollowUp('c1', empty, d)

    expect(d.writeNote).not.toHaveBeenCalled()
    expect(d.createCard).not.toHaveBeenCalled()
    expect(d.confirmOverwrite).not.toHaveBeenCalled()
    expect(outcome).toEqual({
      note: 'skipped',
      noteError: null,
      card: 'skipped',
      cardError: null,
    })
  })

  it('沒選卡、沒填人設的表單，關係 seed payload 與加這個功能之前逐字相同', () => {
    const form = newCharacterInitialRelationshipForm()
    form.relationship_label = '同事'
    form.user_address_name = '阿丹'

    expect(buildInitialRelationshipPayload(form)).toEqual({
      relationship_label: '同事',
      known_context: '',
      living_arrangement: '',
      user_address_name: '阿丹',
      character_address_name: '',
      tone_distance: '',
      familiarity_boundary: '',
      schedule_involvement_policy: 'none',
      proactive_permission: true,
      proactive_cadence_hint: '',
      user_profile_notes: '',
      confirmed_by_user: true,
      safe_user_profile: undefined,
    })
  })

  it('「略過」且什麼都沒填時，後續工作是空的——零請求，與加這個功能之前等價', async () => {
    // 精靈的 skip() 現在送 buildFollowUp()：人設空、沒勾存卡時它等價於一包
    // 空的（cardContent 只是快照，沒有人要它就不會被送出去）。
    const form = newCharacterInitialRelationshipForm()
    const followUp = {
      personaNote: '',
      saveCardName: null,
      cardContent: buildIdentityCardContent(form, ''),
    }

    expect(hasCharacterCreationFollowUpWork(followUp)).toBe(false)
    const d = deps()
    await runCharacterCreationFollowUp('c1', followUp, d)
    expect(d.writeNote).not.toHaveBeenCalled()
    expect(d.createCard).not.toHaveBeenCalled()
  })

  it('「略過」但人設已填 → note 照樣寫進去（略過的是關係，不是玩家填過的東西）', async () => {
    const form = newCharacterInitialRelationshipForm()
    const followUp = {
      personaNote: '我看得見情緒的顏色',
      saveCardName: null,
      cardContent: buildIdentityCardContent(form, '我看得見情緒的顏色'),
    }

    const d = deps()
    const outcome = await runCharacterCreationFollowUp('c1', followUp, d)

    expect(d.writeNote).toHaveBeenCalledWith('c1', '我看得見情緒的顏色')
    expect(outcome.note).toBe('done')
  })

  it('「略過」但勾了存卡 → 卡照樣存，內容是送出當下的表單快照', async () => {
    const form = newCharacterInitialRelationshipForm()
    form.relationship_label = '同事'
    const followUp = {
      personaNote: '',
      saveCardName: '上班族的我',
      cardContent: buildIdentityCardContent(form, ''),
    }

    const d = deps()
    const outcome = await runCharacterCreationFollowUp('c1', followUp, d)

    expect(d.createCard).toHaveBeenCalledWith(
      expect.objectContaining({ name: '上班族的我', relationship_label: '同事' }),
    )
    expect(outcome.card).toBe('done')
  })

  it('composable 在沒有工作時提早 return，一個請求都不發', () => {
    const composable = source('../src/composables/useCharacterCreationFollowUp.ts')
    expect(composable).toContain('if (!hasCharacterCreationFollowUpWork(followUp)) return')
  })
})

describe('精靈接線（原始碼掃描）', () => {
  it('掛了 picker，並把選到的卡交給 applyIdentityCard', () => {
    expect(WIZARD).toContain("import IdentityCardPicker from '@/components/IdentityCardPicker.vue'")
    expect(WIZARD).toContain('<IdentityCardPicker')
    expect(WIZARD).toContain('@select="applyIdentityCard"')
    expect(WIZARD).toContain(':active="visible"')
  })

  it('選卡走共用的 applyIdentityCardToForm，並比照既有邏輯撤掉已被回答的 intake 追問', () => {
    expect(WIZARD).toContain('const answered = applyIdentityCardToForm(form.value, card)')
    expect(WIZARD).toContain('for (const field of answered) removeAnsweredIntakeQuestion(field)')
  })

  it('卡片沒帶人設時不覆寫人設欄——玩家在套卡前打的那段留著', () => {
    expect(WIZARD).toContain("const personaFromCard = (card.persona_note ?? '').trim()")
    expect(WIZARD).toContain('if (personaFromCard) personaNote.value = personaFromCard')
  })

  it('picker 選完就退回 placeholder——否則重選同一張卡不會有 change 事件', () => {
    expect(PICKER).toContain('if (card) emit(\'select\', card)')
    const emitAt = PICKER.indexOf('if (card) emit(\'select\', card)')
    const resetAt = PICKER.indexOf('selectedId.value = \'\'', emitAt)
    expect(resetAt).toBeGreaterThan(emitAt)
    // 舊行為：把選到的 id 留在框裡當狀態。
    expect(PICKER).not.toContain('selectedId.value = value')
  })

  it('picker 沒有卡就整段不出現（空下拉是一個要玩家自己看懂的謎題）', () => {
    expect(PICKER).toContain('v-if="cards.length"')
  })

  it('picker 每次開精靈重抓，且讀不到清單時 fail-soft 當作沒有卡', () => {
    expect(PICKER).toContain('watch(() => props.active')
    expect(PICKER).toContain('void reload()')
    expect(PICKER).toContain('cards.value = []')
  })

  it('精靈有「你的人設」欄，上限鏡像 PP 的 500 並顯示字數', () => {
    expect(WIZARD).toContain("import { PLAYER_PERSONA_NOTE_MAX_CHARS } from '@/utils/api/playerPersonaNote'")
    expect(WIZARD).toContain(':maxlength="personaNoteMaxChars"')
    expect(WIZARD).toContain("t('identityCard.personaNote.label')")
    expect(WIZARD).toContain('personaNoteCounter')
  })

  it('人設留空也送得出去——送出閘不再只看關係 seed', () => {
    expect(WIZARD).toContain(':disabled="intakeLoading || !canSubmit"')
    expect(WIZARD).toContain("if (!canSubmitSeed.value && !personaNote.value.trim()) return false")
  })

  it('勾了存卡就必須有卡名，否則按下去是必然的 422', () => {
    expect(WIZARD).toContain('if (!saveAsIdentityCard.value) return true')
    expect(WIZARD).toContain('return Boolean(name) && name.length <= identityCardNameMaxChars')
    expect(WIZARD).toContain('v-if="saveAsIdentityCard"')
  })

  it('confirm 的第一個參數仍是原本的關係 seed payload，後續工作只是加在第二個', () => {
    expect(WIZARD).toContain("emit('confirm', payload.value, buildFollowUp())")
  })

  it('「略過」只略過關係 seed——已填的人設／已勾的存卡照常隨後續工作走', () => {
    expect(WIZARD).toContain("emit('confirm', null, buildFollowUp())")
    // 舊行為：略過時把玩家填的人設與存卡勾選一起無聲丟掉。
    expect(WIZARD).not.toContain('emptyCharacterCreationFollowUp')
  })

  it('每次開窗都清掉上一次的人設與存卡欄，不把上一位角色的內容留給下一位', () => {
    expect(WIZARD).toContain('personaNote.value = \'\'')
    expect(WIZARD).toContain('saveAsIdentityCard.value = false')
    expect(WIZARD).toContain('identityCardName.value = \'\'')
  })
})

describe('四個建角入口都在建角成功後跑同一套後續工作', () => {
  const hosts: Array<[string, string]> = [
    ['PlayerCharacterCardPanel', PLAYER_PANEL],
    ['CharactersAdminPage', ADMIN_PAGE],
    ['CharacterCardMarketplace', MARKETPLACE],
    ['CharacterCreateModal', CREATE_MODAL],
  ]

  for (const [name, host] of hosts) {
    it(`${name} 用共用 composable，而不是自己再刻一份寫 note／存卡`, () => {
      expect(host).toContain(
        "import { useCharacterCreationFollowUp } from '@/composables/useCharacterCreationFollowUp'",
      )
      expect(host).toContain('useCharacterCreationFollowUp()')
      expect(host).toContain('characterCreationFollowUp.run(')
      // 後續工作只走 composable：呼叫端不該直接碰這兩支 API。
      expect(host).not.toContain('updatePlayerPersonaNote(')
      expect(host).not.toContain('createIdentityCard(')
    })
  }

  it('玩家側在 notifyCharacterCreated 之前就把人設寫完——PP 首彈窗的條件在那之後才判斷', () => {
    const runAt = PLAYER_PANEL.indexOf('await characterCreationFollowUp.run(result.character.id, followUp)')
    const notifyAt = PLAYER_PANEL.indexOf('notifyCharacterCreated(result.character', runAt)
    expect(runAt).toBeGreaterThan(-1)
    expect(notifyAt).toBeGreaterThan(runAt)
  })

  it('關係 seed 仍照原樣送進建角 API，沒有被後續工作改寫', () => {
    expect(PLAYER_PANEL).toContain('initialRelationship,')
    expect(ADMIN_PAGE).toContain('{ initialRelationship },')
    expect(MARKETPLACE).toContain('initialRelationship,')
  })
})

describe('每個內嵌關係 seed 表單的建角入口都有身分卡三件套（IC 審查 FIX-A）', () => {
  // 「哪些檔案自己內嵌了一份 11 欄 seed 表單」才是這個功能的真正邊界——不是
  // 「哪些檔案掛了精靈」。漏掉一個的症狀不會是紅燈，而是玩家在某條建角路徑
  // 上看不到 picker，卻在另一條看得到。
  const seedForms: Array<[string, string]> = [
    ['InitialRelationshipWizardModal（從角色卡建角）', WIZARD],
    ['CharacterCreateModal（從零手動建角）', CREATE_MODAL],
  ]

  for (const [name, src] of seedForms) {
    it(`${name} 有 picker、「你的人設」欄與存卡勾選`, () => {
      expect(src).toContain('<IdentityCardPicker')
      expect(src).toContain("t('identityCard.personaNote.label')")
      expect(src).toContain("t('identityCard.save.checkbox')")
      expect(src).toContain('applyIdentityCardToForm(')
      expect(src).toContain('buildIdentityCardContent(')
    })
  }
})

describe('身分卡不動 PP 首彈窗的條件本身', () => {
  it('沒有人為了讓彈窗不彈而去改條件——note 寫進去之後它自然不成立', () => {
    const setting = source('../src/components/PlayerPersonaNoteSetting.vue')
    const modal = source('../src/components/PlayerPersonaNoteModal.vue')
    expect(setting).not.toContain('identityCard')
    expect(modal).not.toContain('identityCard')
  })
})
