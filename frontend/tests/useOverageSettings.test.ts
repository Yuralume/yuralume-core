import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  fetchOverageSettings,
  updateOverageSettings,
} from '@/utils/api/overageSettings'
import { notifyIdentityChanged } from '@/utils/identityLifecycle'
import { useOverageSettings } from '@/composables/useOverageSettings'

vi.mock('@/utils/api/overageSettings', () => ({
  fetchOverageSettings: vi.fn(),
  updateOverageSettings: vi.fn(),
}))

const mockedFetch = vi.mocked(fetchOverageSettings)
const mockedUpdate = vi.mocked(updateOverageSettings)

function settings(overrides: Partial<{
  chat_image_enabled: boolean
  feed_post_enabled: boolean
  tier_overage_enabled: boolean
  daily_limit: number
  chat_image_price_cr: number | null
  feed_post_price_cr: number | null
}> = {}) {
  return {
    chat_image_enabled: false,
    feed_post_enabled: false,
    tier_overage_enabled: true,
    daily_limit: 5,
    chat_image_price_cr: 8,
    feed_post_price_cr: 6,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useOverageSettings().reset()
})

describe('useOverageSettings', () => {
  it('stays unready until real switch states arrive', async () => {
    const overage = useOverageSettings()
    expect(overage.ready.value).toBe(false)

    mockedFetch.mockResolvedValueOnce({ kind: 'ok', settings: settings() })
    await overage.ensureLoaded()

    expect(overage.ready.value).toBe(true)
    expect(overage.settings.value?.daily_limit).toBe(5)
  })

  it('never invents a defaults object out of a failed read', async () => {
    // A drawn "off" switch is a claim about this player's consent.
    const overage = useOverageSettings()
    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })

    await overage.ensureLoaded()

    expect(overage.ready.value).toBe(false)
    expect(overage.settings.value).toBeNull()
  })

  it('stops asking once the deployment says there is nothing to consent to', async () => {
    const overage = useOverageSettings()
    mockedFetch.mockResolvedValueOnce({ kind: 'unsupported' })

    await overage.ensureLoaded()
    await overage.reload()

    expect(mockedFetch).toHaveBeenCalledTimes(1)
    expect(overage.supported.value).toBe(false)
  })

  it('loads once — a second ensureLoaded does not re-request', async () => {
    const overage = useOverageSettings()
    mockedFetch.mockResolvedValueOnce({ kind: 'ok', settings: settings() })

    await overage.ensureLoaded()
    await overage.ensureLoaded()

    expect(mockedFetch).toHaveBeenCalledTimes(1)
  })

  it('adopts the server answer as the new truth after a flip', async () => {
    const overage = useOverageSettings()
    mockedFetch.mockResolvedValueOnce({ kind: 'ok', settings: settings() })
    await overage.ensureLoaded()

    mockedUpdate.mockResolvedValueOnce(settings({ feed_post_enabled: true }))
    await overage.setSwitch('feed_post', true)

    expect(mockedUpdate).toHaveBeenCalledWith({ feed_post_enabled: true })
    expect(overage.settings.value?.feed_post_enabled).toBe(true)
    expect(overage.settings.value?.chat_image_enabled).toBe(false)
  })

  it('propagates a failed flip so the caller can restore the control', async () => {
    const overage = useOverageSettings()
    mockedFetch.mockResolvedValueOnce({ kind: 'ok', settings: settings() })
    await overage.ensureLoaded()

    mockedUpdate.mockRejectedValueOnce(new Error('boom'))

    await expect(overage.setSwitch('chat_image', true)).rejects.toThrow()
    expect(overage.settings.value?.chat_image_enabled).toBe(false)
  })

  it('drops one player’s consent when the signed-in identity changes', async () => {
    // Shared browser: the next account must never inherit these switches.
    const overage = useOverageSettings()
    mockedFetch.mockResolvedValueOnce({
      kind: 'ok', settings: settings({ feed_post_enabled: true }),
    })
    await overage.ensureLoaded()

    notifyIdentityChanged()

    expect(overage.settings.value).toBeNull()
    expect(overage.ready.value).toBe(false)
  })

  it('discards a read that lands after the identity changed', async () => {
    const overage = useOverageSettings()
    let resolveRead: (value: { kind: 'ok'; settings: ReturnType<typeof settings> }) => void
    mockedFetch.mockReturnValueOnce(new Promise((resolve) => {
      resolveRead = resolve as typeof resolveRead
    }))

    const pending = overage.ensureLoaded()
    notifyIdentityChanged()
    resolveRead!({ kind: 'ok', settings: settings({ feed_post_enabled: true }) })
    await pending

    expect(overage.settings.value).toBeNull()
  })
})
