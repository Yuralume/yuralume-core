import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  getRelationshipNames,
  updateRelationshipNames,
} from '@/utils/api/relationshipNames'
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

describe('relationship names API', () => {
  it('loads current names for a character', async () => {
    const data = {
      character_id: 'c1',
      operator_id: 'op1',
      user_address_name: '阿丹',
      character_address_name: '美緒姐',
    }
    mockedAxios.get.mockResolvedValueOnce({ data })

    await expect(getRelationshipNames('c1')).resolves.toEqual(data)
    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/c1/relationship-names',
    )
  })

  it('PATCHes only the provided names', async () => {
    const data = {
      character_id: 'c1',
      operator_id: 'op1',
      user_address_name: '阿丹',
      character_address_name: '',
    }
    mockedAxios.patch.mockResolvedValueOnce({ data })

    await expect(
      updateRelationshipNames('c1', { user_address_name: '阿丹' }),
    ).resolves.toEqual(data)
    expect(mockedAxios.patch).toHaveBeenCalledWith(
      '/api/v1/characters/c1/relationship-names',
      { user_address_name: '阿丹' },
    )
  })

  it('encodes the character id in the path', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: {} })
    await getRelationshipNames('a/b')
    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/a%2Fb/relationship-names',
    )
  })
})

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
