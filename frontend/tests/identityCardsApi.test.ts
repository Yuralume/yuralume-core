/**
 * IC2 — 玩家身分卡 API client。
 *
 * 契約在 IC1 的 `api/routes/player_identity_card.py`；這裡釘住路徑、方法、
 * 以及「409 的兩種 detail.code 分得開」——分不開的話撞名會被當成上限、玩家
 * 會拿到一則叫他去刪卡的錯誤訊息。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  createIdentityCard,
  deleteIdentityCard,
  identityCardErrorCode,
  isIdentityCardLimitReached,
  isIdentityCardNameConflict,
  listIdentityCards,
  renameIdentityCard,
  IDENTITY_CARD_LIMIT_REACHED_CODE,
  IDENTITY_CARD_NAME_CONFLICT_CODE,
  IDENTITY_CARD_NAME_MAX_CHARS,
  IDENTITY_CARDS_PER_OPERATOR,
} from '@/utils/api/identityCards'
import { emptyIdentityCardContent } from '@/utils/characterCreationFollowUp'

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

beforeEach(() => {
  vi.clearAllMocks()
})

describe('identity card API client', () => {
  it('列表帶回卡片與上限', async () => {
    const data = { cards: [], limit: IDENTITY_CARDS_PER_OPERATOR }
    mockedAxios.get.mockResolvedValueOnce({ data })

    await expect(listIdentityCards()).resolves.toEqual(data)
    expect(mockedAxios.get).toHaveBeenCalledWith('/api/v1/identity-cards')
  })

  it('建卡把內容與名稱一起 POST', async () => {
    mockedAxios.post.mockResolvedValueOnce({ data: { id: 'card-1' } })
    const body = { ...emptyIdentityCardContent(), name: '上班族的我' }

    await createIdentityCard(body)

    expect(mockedAxios.post).toHaveBeenCalledWith('/api/v1/identity-cards', body)
  })

  it('改名只送 name（沒有 overwrite 語意）', async () => {
    mockedAxios.patch.mockResolvedValueOnce({ data: { id: 'card-1' } })

    await renameIdentityCard('card 1', '異世界勇者的我')

    expect(mockedAxios.patch).toHaveBeenCalledWith(
      '/api/v1/identity-cards/card%201',
      { name: '異世界勇者的我' },
    )
  })

  it('刪卡走 DELETE，id 有做 URL 編碼', async () => {
    mockedAxios.delete.mockResolvedValueOnce({ data: null })

    await deleteIdentityCard('card/1')

    expect(mockedAxios.delete).toHaveBeenCalledWith('/api/v1/identity-cards/card%2F1')
  })

  it('上限常數鏡像後端', () => {
    expect(IDENTITY_CARD_NAME_MAX_CHARS).toBe(80)
    expect(IDENTITY_CARDS_PER_OPERATOR).toBe(30)
  })
})

describe('409 的兩種 detail.code 分得開', () => {
  function err(code: string) {
    return { response: { status: 409, data: { detail: { code } } } }
  }

  it('撞名只認撞名', () => {
    const error = err(IDENTITY_CARD_NAME_CONFLICT_CODE)
    expect(identityCardErrorCode(error)).toBe(IDENTITY_CARD_NAME_CONFLICT_CODE)
    expect(isIdentityCardNameConflict(error)).toBe(true)
    expect(isIdentityCardLimitReached(error)).toBe(false)
  })

  it('上限只認上限', () => {
    const error = err(IDENTITY_CARD_LIMIT_REACHED_CODE)
    expect(isIdentityCardLimitReached(error)).toBe(true)
    expect(isIdentityCardNameConflict(error)).toBe(false)
  })

  it('不是結構化 detail 的錯誤不會被誤判成任何一種', () => {
    expect(identityCardErrorCode(new Error('network'))).toBeNull()
    expect(identityCardErrorCode({ response: { data: { detail: 'nope' } } })).toBeNull()
    expect(identityCardErrorCode({ response: { data: { detail: [{ msg: 'x' }] } } })).toBeNull()
    expect(isIdentityCardNameConflict(null)).toBe(false)
  })
})
