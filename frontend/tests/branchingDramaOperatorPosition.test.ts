/**
 * BD2 — the player's declared place in a branching drama, front end side.
 *
 * Two things worth pinning here, both of them silent when broken:
 * the create payload actually carrying the choice (a dropped field looks
 * exactly like "the player picked 主演"), and the three option labels
 * existing in every shipped locale (a missing key renders as the raw key
 * inside a picker the player has to choose from).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import { createBranchingDrama } from '@/utils/api/branchingDrama'
import {
  DEFAULT_DRAMA_OPERATOR_POSITION,
  DRAMA_OPERATOR_NOTE_MAX_CHARS,
  DRAMA_OPERATOR_POSITIONS,
} from '@/types/branchingDrama'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

describe('createBranchingDrama', () => {
  it('sends the position and note the player chose', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(202, { id: 'd-1' }))

    await createBranchingDrama({
      character_ids: ['c-a', 'c-b'],
      prompt: '下雪的旅館',
      total_segments: 6,
      operator_position: 'absent',
      operator_note: '我只是看戲',
    })

    const [, init] = mockedAuthedFetch.mock.calls[0]
    expect(JSON.parse(String(init?.body))).toMatchObject({
      operator_position: 'absent',
      operator_note: '我只是看戲',
    })
  })

  it('omits the slot entirely for an entry point with no opinion', async () => {
    // The fusion 「換個玩法」 handoff seeds prompt + cast only; leaving the
    // field out is what makes the backend default apply.
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(202, { id: 'd-2' }))

    await createBranchingDrama({
      character_ids: ['c-a', 'c-b'],
      prompt: '下雪的旅館',
      total_segments: 6,
    })

    const [, init] = mockedAuthedFetch.mock.calls[0]
    const payload = JSON.parse(String(init?.body))
    expect(payload).not.toHaveProperty('operator_position')
    expect(payload).not.toHaveProperty('operator_note')
  })
})

describe('operator position vocabulary', () => {
  it('defaults to the lead role', () => {
    expect(DEFAULT_DRAMA_OPERATOR_POSITION).toBe('central')
    expect(DRAMA_OPERATOR_POSITIONS).toEqual(['central', 'present', 'absent'])
  })

  it('mirrors the backend note ceiling', () => {
    expect(DRAMA_OPERATOR_NOTE_MAX_CHARS).toBe(120)
  })

  it.each([
    ['zh-TW', zhTW],
    ['en-US', enUS],
    ['ja-JP', jaJP],
  ])('has a label and hint for every option in %s', (_tag, catalog) => {
    const page = (catalog as Record<string, any>).branchingDrama.page
    for (const position of DRAMA_OPERATOR_POSITIONS) {
      const option = page.operatorPosition.options[position]
      expect(option.label).toBeTruthy()
      expect(option.hint).toBeTruthy()
    }
    expect(page.operatorPosition.label).toBeTruthy()
    expect(page.operatorNote.label).toBeTruthy()
    expect(page.operatorNote.placeholder).toBeTruthy()
    expect(page.operatorNote.hint).toBeTruthy()
  })
})
