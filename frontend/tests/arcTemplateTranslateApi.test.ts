import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'

import {
  listArcTemplates,
  previewArcTemplateTranslation,
} from '@/utils/api/arcTemplates'
import type { ArcTemplate } from '@/types/arcTemplate'

vi.mock('axios', () => {
  const api = { get: vi.fn(), post: vi.fn() }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
})

function template(overrides: Partial<ArcTemplate> = {}): ArcTemplate {
  return {
    id: 'quiet_breakup',
    title: '沒有吵架的告別',
    premise: '一段沒有溫度的關係。',
    theme: 'loss',
    tone: 'dark',
    language: 'zh-TW',
    duration_days: 10,
    beat_count: 1,
    applicability_scope: 'generic',
    target_character_ids: [],
    binding: { world_frames: ['modern'], required_traits: [] },
    beats: [
      {
        sequence: 0,
        day_offset: 0,
        title: '週日的早餐',
        summary: '兩個人一起吃早餐。',
        tension: 'setup',
        scene_type: 'revelation',
        location: '共同的家',
        scene_characters: ['伴侶'],
        dramatic_question: '這算還在一起嗎？',
        required: true,
      },
    ],
    ...overrides,
  }
}

describe('listArcTemplates', () => {
  it('surfaces the language field from the picker list payload', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: [template()] })
    const result = await listArcTemplates()
    expect(result[0].language).toBe('zh-TW')
  })
})

describe('previewArcTemplateTranslation', () => {
  it('requests the preview endpoint with the target language and returns the translated view', async () => {
    const translated = template({
      title: 'A Quiet Goodbye',
      premise: 'A relationship with no warmth left.',
      language: 'en-US',
    })
    mockedAxios.get.mockResolvedValueOnce({ data: translated })

    const result = await previewArcTemplateTranslation('quiet_breakup', 'en-US')

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/arc-templates/quiet_breakup/preview-translation',
      expect.objectContaining({ params: expect.any(URLSearchParams) }),
    )
    const config = mockedAxios.get.mock.calls[0][1]
    expect(config).toBeDefined()
    const params = config!.params as URLSearchParams
    expect(params.get('target_language')).toBe('en-US')
    expect(result.title).toBe('A Quiet Goodbye')
    expect(result.language).toBe('en-US')
  })

  it('omits target_language when none is provided (backend resolves operator language)', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: template() })
    await previewArcTemplateTranslation('quiet_breakup')
    const config = mockedAxios.get.mock.calls[0][1]
    expect(config).toBeDefined()
    const params = config!.params as URLSearchParams
    expect(params.get('target_language')).toBeNull()
  })
})
