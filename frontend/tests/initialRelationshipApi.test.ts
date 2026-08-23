import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  getInitialRelationship,
  updateInitialRelationship,
} from '@/utils/api/initialRelationship'

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

describe('initial relationship seed API', () => {
  it('GETs the whole seed for a character', async () => {
    const data = {
      character_id: 'c1',
      operator_id: 'op1',
      has_seed: true,
      relationship_label: '朋友',
      known_context: '',
      living_arrangement: '',
      user_address_name: '阿丹',
      character_address_name: '澪',
      tone_distance: '',
      familiarity_boundary: '',
      schedule_involvement_policy: 'none',
      proactive_permission: false,
      proactive_cadence_hint: '',
      user_profile_notes: '',
      confirmed_by_user: true,
    }
    mockedAxios.get.mockResolvedValueOnce({ data })

    await expect(getInitialRelationship('c1')).resolves.toEqual(data)
    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/c1/initial-relationship',
    )
  })

  it('PATCHes only the fields the caller included, not the full form', async () => {
    mockedAxios.patch.mockResolvedValueOnce({
      data: { character_id: 'c1', operator_id: 'op1', has_seed: true },
    })

    await updateInitialRelationship('c1', { tone_distance: '更親近一點' })

    expect(mockedAxios.patch).toHaveBeenCalledWith(
      '/api/v1/characters/c1/initial-relationship',
      { tone_distance: '更親近一點' },
    )
  })

  it('sends an empty string through rather than short-circuiting a clear', () => {
    mockedAxios.patch.mockResolvedValueOnce({ data: {} })

    void updateInitialRelationship('c1', { known_context: '' })

    expect(mockedAxios.patch).toHaveBeenCalledWith(
      '/api/v1/characters/c1/initial-relationship',
      { known_context: '' },
    )
  })

  it('carries both address names through the same PATCH the rest of the seed uses', () => {
    // IR2 folds the old dedicated `/relationship-names` calls into this one
    // endpoint — the backend delegates the rename bookkeeping internally.
    mockedAxios.patch.mockResolvedValueOnce({ data: {} })

    void updateInitialRelationship('c1', {
      user_address_name: '小夏',
      character_address_name: '前輩',
    })

    expect(mockedAxios.patch).toHaveBeenCalledWith(
      '/api/v1/characters/c1/initial-relationship',
      { user_address_name: '小夏', character_address_name: '前輩' },
    )
  })

  it('encodes the character id in the path', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: {} })
    await getInitialRelationship('a/b')
    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/a%2Fb/initial-relationship',
    )
  })
})
