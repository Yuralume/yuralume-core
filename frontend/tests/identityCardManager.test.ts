/**
 * IC3 — 設定頁「玩家身分卡」管理面：清單狀態（`usePlayerIdentityCards`）與
 * 唯讀預覽（`identityCardPreview.ts`）。
 *
 * 元件本身（`IdentityCardManagerPanel.vue` / `IdentityCardPreviewDialog.vue`）
 * 掛不了（這個 repo 沒有 jsdom / @vue/test-utils），所以清單載入／改名／
 * 刪除／預覽渲染全部落在這兩層純邏輯裡直接測；元件只做接線，用原始碼掃描
 * 釘住。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  identityCardPreviewCell,
  IDENTITY_CARD_PREVIEW_FIELDS,
} from '@/utils/identityCardPreview'
import {
  removeIdentityCardById,
  replaceIdentityCardInList,
  sortIdentityCardsByRecency,
} from '@/utils/identityCardManager'
import { IDENTITY_CARD_SEED_FIELDS } from '@/utils/identityCard'
import { usePlayerIdentityCards } from '@/composables/usePlayerIdentityCards'
import type { IdentityCard, IdentityCardContent } from '@/utils/api/identityCards'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

function emptyContent(): IdentityCardContent {
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

function card(overrides: Partial<IdentityCard> = {}): IdentityCard {
  return {
    id: 'card-1',
    operator_id: 'op-1',
    name: '上班族的我',
    created_at: '2026-08-27T00:00:00+00:00',
    updated_at: '2026-08-27T00:00:00+00:00',
    ...emptyContent(),
    ...overrides,
  }
}

describe('清單就地更新（不重新 GET 整份列表）', () => {
  it('改名成功：用回寫的卡片取代同 id 那筆，其他筆不動', () => {
    const cards = [card({ id: 'a', name: '甲' }), card({ id: 'b', name: '乙' })]
    const updated = card({ id: 'a', name: '甲改名' })

    const next = replaceIdentityCardInList(cards, updated)

    expect(next.find(c => c.id === 'a')?.name).toBe('甲改名')
    expect(next.find(c => c.id === 'b')?.name).toBe('乙')
    expect(next).not.toBe(cards)
  })

  it('刪除成功：從清單移除那筆，其他筆不動', () => {
    const cards = [card({ id: 'a' }), card({ id: 'b' })]

    const next = removeIdentityCardById(cards, 'a')

    expect(next.map(c => c.id)).toEqual(['b'])
  })
})

describe('sortIdentityCardsByRecency：與後端 order_by(updated_at.desc(), id.desc()) 同一份契約', () => {
  it('依 updated_at 新到舊排序', () => {
    const cards = [
      card({ id: 'old', updated_at: '2026-08-01T00:00:00+00:00' }),
      card({ id: 'new', updated_at: '2026-08-27T00:00:00+00:00' }),
      card({ id: 'mid', updated_at: '2026-08-15T00:00:00+00:00' }),
    ]

    const sorted = sortIdentityCardsByRecency(cards)

    expect(sorted.map(c => c.id)).toEqual(['new', 'mid', 'old'])
  })

  it('updated_at 相同時用 id 降冪打平，比照後端', () => {
    const cards = [
      card({ id: 'a', updated_at: '2026-08-27T00:00:00+00:00' }),
      card({ id: 'c', updated_at: '2026-08-27T00:00:00+00:00' }),
      card({ id: 'b', updated_at: '2026-08-27T00:00:00+00:00' }),
    ]

    const sorted = sortIdentityCardsByRecency(cards)

    expect(sorted.map(c => c.id)).toEqual(['c', 'b', 'a'])
  })

  it('不改動原陣列', () => {
    const cards = [card({ id: 'a', updated_at: '2026-08-01T00:00:00+00:00' }), card({ id: 'b', updated_at: '2026-08-27T00:00:00+00:00' })]
    const original = [...cards]

    sortIdentityCardsByRecency(cards)

    expect(cards).toEqual(original)
  })
})

describe('usePlayerIdentityCards：載入／改名／刪除狀態', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('載入成功後 cards／limit／loaded 都對', async () => {
    const data = { cards: [card({ id: 'a' }), card({ id: 'b' })], limit: 30 }
    mockedAxios.get.mockResolvedValueOnce({ data })

    const store = usePlayerIdentityCards()
    expect(store.loaded.value).toBe(false)
    await store.load()

    expect(store.cards.value).toEqual(data.cards)
    expect(store.limit.value).toBe(30)
    expect(store.loaded.value).toBe(true)
    expect(store.loading.value).toBe(false)
  })

  it('載入失敗——fail-soft 停在「不知道」，不是拿舊清單充數', async () => {
    mockedAxios.get.mockRejectedValueOnce(new Error('network'))

    const store = usePlayerIdentityCards()
    await store.load()

    expect(store.loaded.value).toBe(false)
    expect(store.loading.value).toBe(false)
  })

  it('晚回來的舊請求不准覆寫較新的一次載入', async () => {
    let resolveFirst: (value: { data: { cards: IdentityCard[]; limit: number } }) => void = () => {}
    mockedAxios.get
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce({ data: { cards: [card({ id: 'second' })], limit: 30 } })

    const store = usePlayerIdentityCards()
    const first = store.load()
    const second = store.load()
    await second
    resolveFirst({ data: { cards: [card({ id: 'first' })], limit: 30 } })
    await first

    expect(store.cards.value.map(c => c.id)).toEqual(['second'])
  })

  it('改名成功：呼叫 PATCH 並就地更新清單，回傳更新後的卡片', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: { cards: [card({ id: 'a', name: '舊名' })], limit: 30 } })
    mockedAxios.patch.mockResolvedValueOnce({ data: card({ id: 'a', name: '新名' }) })

    const store = usePlayerIdentityCards()
    await store.load()
    const result = await store.rename('a', ' 新名 ')

    expect(mockedAxios.patch).toHaveBeenCalledWith('/api/v1/identity-cards/a', { name: '新名' })
    expect(result.name).toBe('新名')
    expect(store.cards.value[0].name).toBe('新名')
  })

  it('刪除成功：呼叫 DELETE 並從清單移除', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: { cards: [card({ id: 'a' }), card({ id: 'b' })], limit: 30 } })
    mockedAxios.delete.mockResolvedValueOnce({ data: null })

    const store = usePlayerIdentityCards()
    await store.load()
    await store.remove('a')

    expect(mockedAxios.delete).toHaveBeenCalledWith('/api/v1/identity-cards/a')
    expect(store.cards.value.map(c => c.id)).toEqual(['b'])
  })

  it('改名成功後依 updated_at 重排：改到的卡片浮到最新，不是停在原本位置', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: {
        cards: [
          card({ id: 'a', name: '甲', updated_at: '2026-08-27T00:00:00+00:00' }),
          card({ id: 'b', name: '乙', updated_at: '2026-08-01T00:00:00+00:00' }),
        ],
        limit: 30,
      },
    })
    // PATCH 回來的 updated_at 被後端推到現在——晚於清單裡另一張卡。
    mockedAxios.patch.mockResolvedValueOnce({
      data: card({ id: 'b', name: '乙改名', updated_at: '2026-08-27T12:00:00+00:00' }),
    })

    const store = usePlayerIdentityCards()
    await store.load()
    expect(store.cards.value.map(c => c.id)).toEqual(['a', 'b'])

    await store.rename('b', '乙改名')

    expect(store.cards.value.map(c => c.id)).toEqual(['b', 'a'])
  })

  it('改名／刪除失敗會 throw 給呼叫端，清單維持原狀', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: { cards: [card({ id: 'a' })], limit: 30 } })
    mockedAxios.patch.mockRejectedValueOnce(new Error('409'))

    const store = usePlayerIdentityCards()
    await store.load()
    await expect(store.rename('a', '新名')).rejects.toThrow('409')
    expect(store.cards.value[0].id).toBe('a')
  })
})

describe('IDENTITY_CARD_PREVIEW_FIELDS：覆蓋全部 12 欄，一欄不漏', () => {
  it('欄位集合與 IDENTITY_CARD_SEED_FIELDS ＋ persona_note 完全一致', () => {
    const previewFields = IDENTITY_CARD_PREVIEW_FIELDS.map(f => f.field).sort()
    const expected = [...IDENTITY_CARD_SEED_FIELDS, 'persona_note'].sort()
    expect(previewFields).toEqual(expected)
  })

  it('每個欄位都有 labelKey，不是空字串', () => {
    for (const field of IDENTITY_CARD_PREVIEW_FIELDS) {
      expect(field.labelKey.length).toBeGreaterThan(0)
    }
  })
})

describe('identityCardPreviewCell：依 kind 決定渲染值', () => {
  function fieldOf(name: keyof IdentityCardContent) {
    const field = IDENTITY_CARD_PREVIEW_FIELDS.find(f => f.field === name)
    if (!field) throw new Error(`missing preview field: ${name}`)
    return field
  }

  it('text 欄：直接回原始字串值', () => {
    const cell = identityCardPreviewCell(fieldOf('relationship_label'), {
      ...emptyContent(),
      relationship_label: '青梅竹馬',
    })
    expect(cell).toEqual({ kind: 'text', value: '青梅竹馬' })
  })

  it('text 欄留白：回空字串，不是 i18nKey——呼叫端自己決定空值文案', () => {
    const cell = identityCardPreviewCell(fieldOf('known_context'), emptyContent())
    expect(cell).toEqual({ kind: 'text', value: '' })
  })

  it('boolean 欄 true → enabled key', () => {
    const cell = identityCardPreviewCell(fieldOf('proactive_permission'), {
      ...emptyContent(),
      proactive_permission: true,
    })
    expect(cell).toEqual({ kind: 'i18nKey', key: 'identityCard.manage.preview.enabled' })
  })

  it('boolean 欄 false → disabled key', () => {
    const cell = identityCardPreviewCell(fieldOf('proactive_permission'), emptyContent())
    expect(cell).toEqual({ kind: 'i18nKey', key: 'identityCard.manage.preview.disabled' })
  })

  it.each([
    ['none', 'characterCreate.initialRelationship.scheduleOptions.none'],
    ['mention_only', 'characterCreate.initialRelationship.scheduleOptions.mentionOnly'],
    ['invite_required', 'characterCreate.initialRelationship.scheduleOptions.inviteRequired'],
    ['shared_allowed', 'characterCreate.initialRelationship.scheduleOptions.sharedAllowed'],
  ] as const)('enum 欄 %s → %s', (policy, expectedKey) => {
    const cell = identityCardPreviewCell(fieldOf('schedule_involvement_policy'), {
      ...emptyContent(),
      schedule_involvement_policy: policy,
    })
    expect(cell).toEqual({ kind: 'i18nKey', key: expectedKey })
  })
})
