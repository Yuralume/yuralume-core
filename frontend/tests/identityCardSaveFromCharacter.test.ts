/**
 * IC3 — 設定頁「把目前這組存成身分卡」入口的回存邏輯。
 *
 * 用的是**已儲存**的值：兩支既有 GET（關係 seed／玩家人設）的回應，不是
 * 編輯中的表單草稿。這裡把「讀值 → 建內容 → 撞名才問覆蓋」的順序拆成純
 * 函式測，元件那層（`IdentityCardSaveFromCharacter.vue`）只做 HTTP／i18n
 * 接線，用原始碼掃描釘住（見 `identityCardWizard.test.ts` 的既有做法，這
 * 個 repo 沒有 jsdom / @vue/test-utils 掛不了元件）。
 */

import { describe, expect, it, vi } from 'vitest'
import {
  buildIdentityCardContentFromSaved,
  saveIdentityCardFromCharacter,
  type SaveIdentityCardFromCharacterDeps,
} from '@/utils/identityCardSaveFromCharacter'
import { IDENTITY_CARD_SEED_FIELDS } from '@/utils/identityCard'
import { IDENTITY_CARD_NAME_CONFLICT_CODE, type IdentityCard } from '@/utils/api/identityCards'
import type { InitialRelationshipSeed } from '@/utils/api/initialRelationship'

function seed(overrides: Partial<InitialRelationshipSeed> = {}): InitialRelationshipSeed {
  return {
    character_id: 'char-1',
    operator_id: 'op-1',
    has_seed: true,
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
    confirmed_by_user: true,
    ...overrides,
  }
}

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

function conflictError() {
  return {
    response: {
      status: 409,
      data: { detail: { code: IDENTITY_CARD_NAME_CONFLICT_CODE, card_id: 'card-1', name: '上班族的我' } },
    },
  }
}

function deps(overrides: Partial<SaveIdentityCardFromCharacterDeps> = {}): SaveIdentityCardFromCharacterDeps {
  return {
    loadSeed: vi.fn().mockResolvedValue(seed()),
    // 預設帶一則非空人設，讓不是在測「空角色」這條分支的既有案例（撞名／
    // 覆蓋／上限已達……）不會意外先撞到 empty 閘。真的要測 empty 的案例會
    // 自己覆寫成空字串。
    loadPersonaNote: vi.fn().mockResolvedValue({ note: '有寫東西' }),
    createCard: vi.fn().mockResolvedValue(card()),
    confirmOverwrite: vi.fn().mockResolvedValue(true),
    ...overrides,
  }
}

describe('buildIdentityCardContentFromSaved：把已儲存的 seed ＋人設現值逐欄搬進卡片內容', () => {
  it('11 欄 + persona_note 全部搬過去，文字欄 trim', () => {
    const content = buildIdentityCardContentFromSaved(seed({
      relationship_label: ' 青梅竹馬 ',
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
    }), '  我看得見情緒的顏色  ')

    expect(content).toEqual({
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
      persona_note: '我看得見情緒的顏色',
    })
    expect(Object.keys(content).sort()).toEqual([...IDENTITY_CARD_SEED_FIELDS, 'persona_note'].sort())
  })

  it('人設留空就是空字串，不是 undefined／null', () => {
    const content = buildIdentityCardContentFromSaved(seed(), '   ')
    expect(content.persona_note).toBe('')
  })
})

describe('saveIdentityCardFromCharacter：讀值 → 建卡 → 撞名才問覆蓋', () => {
  it('讀值與建卡都成功——內容是兩支 GET 回應組出來的，不是呼叫端自己塞的', async () => {
    const loadSeed = vi.fn().mockResolvedValue(seed({ relationship_label: '同事' }))
    const loadPersonaNote = vi.fn().mockResolvedValue({ note: '我是工程師' })
    const createCard = vi.fn().mockResolvedValue(card())
    const d = deps({ loadSeed, loadPersonaNote, createCard })

    const outcome = await saveIdentityCardFromCharacter('char-1', ' 上班族的我 ', d)

    expect(loadSeed).toHaveBeenCalledWith('char-1')
    expect(loadPersonaNote).toHaveBeenCalledWith('char-1')
    expect(createCard).toHaveBeenCalledWith(expect.objectContaining({
      relationship_label: '同事',
      persona_note: '我是工程師',
      name: '上班族的我',
    }))
    expect(createCard.mock.calls[0][0]).not.toHaveProperty('overwrite')
    expect(outcome).toEqual({ status: 'done', card: card() })
  })

  it('讀 seed 失敗——不送 POST，回報 load_failed', async () => {
    const failure = new Error('boom')
    const createCard = vi.fn()
    const d = deps({ loadSeed: vi.fn().mockRejectedValue(failure), createCard })

    const outcome = await saveIdentityCardFromCharacter('char-1', '上班族的我', d)

    expect(createCard).not.toHaveBeenCalled()
    expect(outcome).toEqual({ status: 'load_failed', error: failure })
  })

  it('讀人設失敗——同樣不送 POST，回報 load_failed', async () => {
    const failure = new Error('boom')
    const createCard = vi.fn()
    const d = deps({ loadPersonaNote: vi.fn().mockRejectedValue(failure), createCard })

    const outcome = await saveIdentityCardFromCharacter('char-1', '上班族的我', d)

    expect(createCard).not.toHaveBeenCalled()
    expect(outcome).toEqual({ status: 'load_failed', error: failure })
  })

  it('撞名 409 → 問覆蓋 → 同意就帶 overwrite 重送', async () => {
    const createCard = vi.fn()
      .mockRejectedValueOnce(conflictError())
      .mockResolvedValueOnce(card())
    const d = deps({ createCard })

    const outcome = await saveIdentityCardFromCharacter('char-1', '上班族的我', d)

    expect(d.confirmOverwrite).toHaveBeenCalledWith('上班族的我')
    expect(createCard).toHaveBeenCalledTimes(2)
    expect(createCard.mock.calls[1][0]).toMatchObject({ name: '上班族的我', overwrite: true })
    expect(outcome).toEqual({ status: 'done', card: card() })
  })

  it('撞名但玩家按取消——不重送、回報 declined', async () => {
    const createCard = vi.fn().mockRejectedValueOnce(conflictError())
    const d = deps({ createCard, confirmOverwrite: vi.fn().mockResolvedValue(false) })

    const outcome = await saveIdentityCardFromCharacter('char-1', '上班族的我', d)

    expect(createCard).toHaveBeenCalledTimes(1)
    expect(outcome).toEqual({ status: 'declined' })
  })

  it('覆蓋重送也失敗——回報 save_failed，帶第二次的錯誤', async () => {
    const overwriteFailure = new Error('still 409 somehow')
    const createCard = vi.fn()
      .mockRejectedValueOnce(conflictError())
      .mockRejectedValueOnce(overwriteFailure)
    const d = deps({ createCard })

    const outcome = await saveIdentityCardFromCharacter('char-1', '上班族的我', d)

    expect(outcome).toEqual({ status: 'save_failed', error: overwriteFailure })
  })

  it('非撞名的失敗（例如上限已達）直接回報，不會誤問覆蓋', async () => {
    const failure = new Error('limit reached')
    const d = deps({ createCard: vi.fn().mockRejectedValue(failure) })

    const outcome = await saveIdentityCardFromCharacter('char-1', '上班族的我', d)

    expect(d.confirmOverwrite).not.toHaveBeenCalled()
    expect(outcome).toEqual({ status: 'save_failed', error: failure })
  })

  it('has_seed:false 且人設也空——不送 POST，回報 empty', async () => {
    const createCard = vi.fn()
    const d = deps({
      loadSeed: vi.fn().mockResolvedValue(seed({ has_seed: false })),
      loadPersonaNote: vi.fn().mockResolvedValue({ note: '' }),
      createCard,
    })

    const outcome = await saveIdentityCardFromCharacter('char-1', '上班族的我', d)

    expect(createCard).not.toHaveBeenCalled()
    expect(outcome).toEqual({ status: 'empty' })
  })

  it('has_seed:true 但逐欄 trim 後皆空、人設也空——同樣不送 POST，回報 empty', async () => {
    const createCard = vi.fn()
    const d = deps({
      loadSeed: vi.fn().mockResolvedValue(seed({
        has_seed: true,
        relationship_label: '   ',
      })),
      loadPersonaNote: vi.fn().mockResolvedValue({ note: '   ' }),
      createCard,
    })

    const outcome = await saveIdentityCardFromCharacter('char-1', '上班族的我', d)

    expect(createCard).not.toHaveBeenCalled()
    expect(outcome).toEqual({ status: 'empty' })
  })

  it('seed 欄位全空但人設有內容——不算 empty，照常送出', async () => {
    const createCard = vi.fn().mockResolvedValue(card())
    const d = deps({
      loadSeed: vi.fn().mockResolvedValue(seed({ has_seed: true })),
      loadPersonaNote: vi.fn().mockResolvedValue({ note: '我是工程師' }),
      createCard,
    })

    const outcome = await saveIdentityCardFromCharacter('char-1', '上班族的我', d)

    expect(createCard).toHaveBeenCalled()
    expect(outcome).toEqual({ status: 'done', card: card() })
  })
})
