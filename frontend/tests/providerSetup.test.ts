import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  countRealProviders,
  resolveNeedsProviderSetup,
  resolveNeedsRoutingSetup,
  resolveNeedsRoutingSetupFromApi,
  shouldGuideProviderSetup,
} from '@/utils/providerSetup'
import { listProviders } from '@/utils/api/system'

vi.mock('@/utils/api/system', () => ({
  listProviders: vi.fn(),
}))

const mockedListProviders = vi.mocked(listProviders)

afterEach(() => {
  vi.clearAllMocks()
})

describe('countRealProviders', () => {
  it('排除佔位用的 fake 後備', () => {
    expect(countRealProviders(['fake'])).toBe(0)
    expect(countRealProviders(['fake', 'openai'])).toBe(1)
    expect(countRealProviders(['openai', 'lmstudio'])).toBe(2)
    expect(countRealProviders([])).toBe(0)
  })
})

describe('shouldGuideProviderSetup', () => {
  it('自架且只有 fake 後備時需要引導', () => {
    expect(shouldGuideProviderSetup({ cloudMode: false, providerIds: ['fake'] })).toBe(true)
    expect(shouldGuideProviderSetup({ cloudMode: false, providerIds: [] })).toBe(true)
  })

  it('自架且已接上真實 provider 時不需要引導', () => {
    expect(shouldGuideProviderSetup({ cloudMode: false, providerIds: ['openai'] })).toBe(false)
    expect(shouldGuideProviderSetup({ cloudMode: false, providerIds: ['fake', 'lmstudio'] })).toBe(false)
  })

  it('cloud 模式一律不引導（路由由控制面板代管）', () => {
    expect(shouldGuideProviderSetup({ cloudMode: true, providerIds: [] })).toBe(false)
    expect(shouldGuideProviderSetup({ cloudMode: true, providerIds: ['fake'] })).toBe(false)
  })
})

describe('resolveNeedsProviderSetup', () => {
  it('cloud 模式直接回 false，不查詢 provider', async () => {
    await expect(resolveNeedsProviderSetup(true)).resolves.toBe(false)
    expect(mockedListProviders).not.toHaveBeenCalled()
  })

  it('自架且沒有真實 provider 時回 true', async () => {
    mockedListProviders.mockResolvedValue(['fake'])
    await expect(resolveNeedsProviderSetup(false)).resolves.toBe(true)
  })

  it('自架且已有真實 provider 時回 false', async () => {
    mockedListProviders.mockResolvedValue(['fake', 'openai'])
    await expect(resolveNeedsProviderSetup(false)).resolves.toBe(false)
  })

  it('查詢拋錯時保守回 false', async () => {
    mockedListProviders.mockRejectedValue(new Error('boom'))
    await expect(resolveNeedsProviderSetup(false)).resolves.toBe(false)
  })
})

describe('resolveNeedsRoutingSetup', () => {
  it('沒有真實 provider 時回 false（讓位給 needsProviderSetup）', () => {
    expect(resolveNeedsRoutingSetup({
      cloudMode: false,
      providerIds: ['fake'],
      groups: [{ model: null }],
      activeModel: null,
    })).toBe(false)
  })

  it('有 provider 但群組與全域 fallback 都未顯式設定時回 true', () => {
    expect(resolveNeedsRoutingSetup({
      cloudMode: false,
      providerIds: ['openai'],
      groups: [{ model: null }, { model: null }],
      activeModel: { provider_id: null, model_id: null },
    })).toBe(true)
  })

  it('有 provider 且任一群組已被顯式覆寫時回 false', () => {
    expect(resolveNeedsRoutingSetup({
      cloudMode: false,
      providerIds: ['openai'],
      groups: [{ model: null }, { model: { provider_id: 'openai', model_id: 'gpt-5' } }],
      activeModel: null,
    })).toBe(false)
  })

  it('有 provider 且全域 fallback 已顯式設定時回 false', () => {
    expect(resolveNeedsRoutingSetup({
      cloudMode: false,
      providerIds: ['openai'],
      groups: [{ model: null }],
      activeModel: { provider_id: 'openai', model_id: 'gpt-5' },
    })).toBe(false)
  })

  it('cloud 模式一律回 false', () => {
    expect(resolveNeedsRoutingSetup({
      cloudMode: true,
      providerIds: ['openai'],
      groups: [{ model: null }],
      activeModel: null,
    })).toBe(false)
  })
})

describe('resolveNeedsRoutingSetupFromApi', () => {
  it('cloud 模式不查詢路由狀態，直接回 false', async () => {
    const loadRoutingState = vi.fn()
    await expect(resolveNeedsRoutingSetupFromApi({
      cloudMode: true,
      providerIds: ['openai'],
      loadRoutingState,
    })).resolves.toBe(false)
    expect(loadRoutingState).not.toHaveBeenCalled()
  })

  it('沒有真實 provider 時不查詢路由狀態，直接回 false', async () => {
    const loadRoutingState = vi.fn()
    await expect(resolveNeedsRoutingSetupFromApi({
      cloudMode: false,
      providerIds: ['fake'],
      loadRoutingState,
    })).resolves.toBe(false)
    expect(loadRoutingState).not.toHaveBeenCalled()
  })

  it('查詢成功且未設定路由時回 true', async () => {
    const loadRoutingState = vi.fn().mockResolvedValue({
      groups: [{ model: null }],
      activeModel: { provider_id: null, model_id: null },
    })
    await expect(resolveNeedsRoutingSetupFromApi({
      cloudMode: false,
      providerIds: ['openai'],
      loadRoutingState,
    })).resolves.toBe(true)
  })

  it('查詢拋錯時保守回 false（非阻斷式卡片，寧可不顯示也不要噴錯）', async () => {
    const loadRoutingState = vi.fn().mockRejectedValue(new Error('boom'))
    await expect(resolveNeedsRoutingSetupFromApi({
      cloudMode: false,
      providerIds: ['openai'],
      loadRoutingState,
    })).resolves.toBe(false)
  })
})
