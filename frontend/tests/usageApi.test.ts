import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  exportUsageEventsCsv,
  listUsageEvents,
  usageSummary,
  type UsageEventRow,
  type UsageSummary,
} from '@/utils/api/observability'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('usage observability API', () => {
  it('loads summary without implicit date or character filters by default', async () => {
    const response: UsageSummary = {
      request_count: 2,
      succeeded_count: 2,
      failed_count: 0,
      cached_count: 0,
      estimated_usage_count: 2,
      estimated_cost_count: 2,
      total_input_quantity: 20,
      total_output_quantity: 10,
      total_billable_quantity: 30,
      cost_currency: 'USD',
      total_cost_amount: '0.0002',
    }
    mockedAxios.get.mockResolvedValueOnce({ data: response })

    await expect(usageSummary()).resolves.toEqual(response)

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/admin/usage/summary',
      {
        params: {
          from: undefined,
          to: undefined,
          capability: undefined,
          character_id: undefined,
          limit: undefined,
        },
      },
    )
  })

  it('loads summary with supported filters', async () => {
    const response: UsageSummary = {
      request_count: 1,
      succeeded_count: 1,
      failed_count: 0,
      cached_count: 0,
      estimated_usage_count: 1,
      estimated_cost_count: 1,
      total_input_quantity: 10,
      total_output_quantity: 5,
      total_billable_quantity: 15,
      cost_currency: 'USD',
      total_cost_amount: '0.0001',
    }
    mockedAxios.get.mockResolvedValueOnce({ data: response })

    await expect(usageSummary({
      from: '2026-06-14T00:00:00Z',
      to: '2026-06-14T23:59:59Z',
      capability: 'llm',
      characterId: 'char-1',
    })).resolves.toEqual(response)

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/admin/usage/summary',
      {
        params: {
          from: '2026-06-14T00:00:00Z',
          to: '2026-06-14T23:59:59Z',
          capability: 'llm',
          character_id: 'char-1',
          limit: undefined,
        },
      },
    )
  })

  it('defaults events limit and preserves upstream request ids', async () => {
    const response: UsageEventRow[] = [{
      id: 'usage-1',
      request_id: 'core-1',
      upstream_request_id: 'gw-1',
      turn_record_id: null,
      conversation_id: null,
      character_id: 'char-1',
      operator_id: 'operator',
      capability: 'image',
      feature_key: 'chat_image_tool',
      source_surface: 'chat_image_tool',
      routing_mode: '',
      provider_id: 'cloud',
      model_id: '',
      profile_id: 'default',
      voice_id: '',
      usage_unit: 'image',
      input_quantity: 1,
      output_quantity: 1,
      total_quantity: 1,
      billable_quantity: 1,
      cached: false,
      usage_is_estimated: true,
      cost_currency: 'USD',
      cost_amount: '0',
      cost_is_estimated: true,
      pricing_source: 'unknown',
      pricing_version: '',
      latency_ms: null,
      status: 'succeeded',
      error_code: null,
      error_message: null,
      artifact_count: 1,
      output_bytes: null,
      duration_seconds: null,
      created_at: '2026-06-14T00:00:00Z',
      completed_at: null,
    }]
    mockedAxios.get.mockResolvedValueOnce({ data: response })

    await expect(listUsageEvents({ capability: 'image' })).resolves.toEqual(response)

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/admin/usage/events',
      {
        params: {
          from: undefined,
          to: undefined,
          capability: 'image',
          character_id: undefined,
          limit: 50,
        },
      },
    )
    expect(response[0].upstream_request_id).toBe('gw-1')
  })

  it('exports CSV as a blob with the default export limit', async () => {
    const csv = new Blob(['id,request_id,upstream_request_id\nusage-1,core-1,gw-1'])
    mockedAxios.get.mockResolvedValueOnce({ data: csv })

    await expect(exportUsageEventsCsv()).resolves.toBe(csv)

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/admin/usage/events.csv',
      {
        params: {
          from: undefined,
          to: undefined,
          capability: undefined,
          character_id: undefined,
          limit: 500,
        },
        responseType: 'blob',
      },
    )
  })
})
