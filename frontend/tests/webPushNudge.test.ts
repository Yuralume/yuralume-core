import { afterEach, describe, expect, it, vi } from 'vitest'
import { resolveWebPushNudge, shouldNudgeWebPush } from '@/utils/webPushNudge'
import {
  getCurrentPushSubscription,
  resolvePushSupportState,
} from '@/utils/pushNotifications'

vi.mock('@/utils/pushNotifications', () => ({
  resolvePushSupportState: vi.fn(),
  getCurrentPushSubscription: vi.fn(),
}))

const mockedResolveState = vi.mocked(resolvePushSupportState)
const mockedGetSubscription = vi.mocked(getCurrentPushSubscription)

const fakeSubscription = { endpoint: 'https://push.example/sub' } as PushSubscription

afterEach(() => {
  vi.clearAllMocks()
})

describe('shouldNudgeWebPush', () => {
  it('提醒：瀏覽器支援且尚未訂閱', () => {
    expect(shouldNudgeWebPush('supported', null)).toBe(true)
  })

  it('不提醒：已經有訂閱', () => {
    expect(shouldNudgeWebPush('supported', fakeSubscription)).toBe(false)
  })

  it('不提醒：瀏覽器不支援 / 未設定 VAPID / 權限被封鎖', () => {
    expect(shouldNudgeWebPush('unsupported', null)).toBe(false)
    expect(shouldNudgeWebPush('unconfigured', null)).toBe(false)
    expect(shouldNudgeWebPush('denied', null)).toBe(false)
  })
})

describe('resolveWebPushNudge', () => {
  it('支援且未訂閱時回傳 true', async () => {
    mockedResolveState.mockResolvedValue('supported')
    mockedGetSubscription.mockResolvedValue(null)

    await expect(resolveWebPushNudge()).resolves.toBe(true)
  })

  it('已訂閱時回傳 false', async () => {
    mockedResolveState.mockResolvedValue('supported')
    mockedGetSubscription.mockResolvedValue(fakeSubscription)

    await expect(resolveWebPushNudge()).resolves.toBe(false)
  })

  it('查詢拋錯時保守回傳 false', async () => {
    mockedResolveState.mockRejectedValue(new Error('boom'))
    mockedGetSubscription.mockResolvedValue(null)

    await expect(resolveWebPushNudge()).resolves.toBe(false)
  })
})
