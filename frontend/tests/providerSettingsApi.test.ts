import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  testDraftProviderConnection,
  testProviderConnection,
  type ProbeReport,
  type ProviderConnectionPayload,
} from '@/utils/api/providerSettings'

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

function draftPayload(): ProviderConnectionPayload {
  return {
    provider: 'openai',
    label: 'OpenAI — Image',
    enabled: true,
    capabilities: ['image'],
    config: { image_model: 'gpt-image-2' },
    secret: { api_key: 'sk-test' },
  }
}

function probe(overrides: Partial<ProbeReport> = {}): ProbeReport {
  return {
    capability: 'image',
    action: 'generated_image',
    ok: true,
    detail: '1 image (1024x1024)',
    latency_ms: 1800,
    ...overrides,
  } as ProbeReport
}

describe('provider settings test API — deep flag & probes', () => {
  it('posts the draft with deep=false by default and returns probes', async () => {
    const payload = draftPayload()
    const result = {
      ok: true,
      last_validated_at: '2026-07-16T00:00:00Z',
      last_validation_error: null,
      probes: [probe({ action: 'config_check', detail: 'ok', latency_ms: 5 })],
    }
    mockedAxios.post.mockResolvedValueOnce({ data: result })

    await expect(testDraftProviderConnection(payload)).resolves.toEqual(result)

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/admin/providers/test-draft',
      { ...payload, deep: false },
    )
  })

  it('posts the draft with deep=true for the deep image test', async () => {
    const payload = draftPayload()
    const result = {
      ok: true,
      last_validated_at: '2026-07-16T00:00:00Z',
      last_validation_error: null,
      probes: [probe()],
    }
    mockedAxios.post.mockResolvedValueOnce({ data: result })

    await expect(testDraftProviderConnection(payload, true)).resolves.toEqual(result)

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/admin/providers/test-draft',
      { ...payload, deep: true },
    )
  })

  it('posts a saved-row test with an optional deep body and surfaces probes', async () => {
    const connection = {
      id: 'conn-1',
      provider: 'openai',
      label: 'OpenAI — LLM',
      enabled: true,
      capabilities: ['llm'],
      config: {},
      secret: { configured: true, fingerprint: 'ab12' },
      last_validated_at: '2026-07-16T00:00:00Z',
      last_validation_error: null,
      created_at: null,
      updated_at: null,
      probes: [probe({ capability: 'llm', action: 'listed_models', detail: '42 models', latency_ms: 300 })],
    }
    mockedAxios.post.mockResolvedValueOnce({ data: connection })

    await expect(testProviderConnection('conn-1')).resolves.toEqual(connection)

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/admin/providers/conn-1/test',
      { deep: false },
    )
  })

  it('sends deep=true and url-encodes the id for a deep saved-row test', async () => {
    const connection = {
      id: 'conn/2',
      provider: 'openai',
      label: 'OpenAI — Image',
      enabled: true,
      capabilities: ['image'],
      config: {},
      secret: { configured: true, fingerprint: 'cd34' },
      last_validated_at: null,
      last_validation_error: 'image: quota exceeded',
      created_at: null,
      updated_at: null,
      probes: [probe({ ok: false, detail: 'quota exceeded' })],
    }
    mockedAxios.post.mockResolvedValueOnce({ data: connection })

    await expect(testProviderConnection('conn/2', true)).resolves.toEqual(connection)

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/admin/providers/conn%2F2/test',
      { deep: true },
    )
  })
})
