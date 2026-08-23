import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import { setPersonaField } from '@/utils/api/operatorPersona'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    post: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
})

// The dedicated `/relationship-names` API wrapper (and its
// `RelationshipNamesEditor` consumer) was retired in IR2 — the two address
// name fields moved into `InitialRelationshipSettingsEditor`, which PATCHes
// them through `/initial-relationship` in the same call as the rest of the
// seed (see `tests/initialRelationshipApi.test.ts`). The backend route
// itself stays (IR1's docstring keeps it for other callers); this file just
// no longer exercises the now-unused frontend wrapper.
describe('persona field correction API', () => {
  it('PUTs an explicit name/nickname correction', async () => {
    const field = {
      field_id: 'f1',
      layer: 1,
      field_key: 'name',
      value: '阿丹',
      confidence: 0.95,
      source: 'user_explicit',
      update_count: 1,
      last_updated: '2026-06-29T00:00:00Z',
      evidence: [],
    }
    mockedAxios.put.mockResolvedValueOnce({ data: field })

    await expect(
      setPersonaField({ character_id: 'c1', field_key: 'name', value: '阿丹' }),
    ).resolves.toEqual(field)
    expect(mockedAxios.put).toHaveBeenCalledWith(
      '/api/v1/operator/persona/fields',
      { character_id: 'c1', field_key: 'name', value: '阿丹' },
    )
  })
})
