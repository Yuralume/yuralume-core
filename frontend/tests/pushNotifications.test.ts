import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  enableWebPushSubscription,
  localNotificationTag,
  PushSubscriptionIncompleteError,
  showBackgroundPageNotification,
  urlBase64ToUint8Array,
} from '@/utils/pushNotifications'
import {
  createPushSubscription,
  getVapidPublicKey,
} from '@/utils/api/push'

vi.mock('@/utils/api/push', () => ({
  getVapidPublicKey: vi.fn(),
  createPushSubscription: vi.fn(),
  deletePushSubscription: vi.fn(),
}))

const mockedGetVapidPublicKey = vi.mocked(getVapidPublicKey)
const mockedCreatePushSubscription = vi.mocked(createPushSubscription)

afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('push notification browser helpers', () => {
  it('decodes VAPID base64url keys', () => {
    vi.stubGlobal('window', { atob: (value: string) => atob(value) })

    expect(Array.from(urlBase64ToUint8Array('AQIDBA'))).toEqual([1, 2, 3, 4])
  })

  it('subscribes from a user permission flow and posts browser keys', async () => {
    const subscription = {
      endpoint: 'https://push.example/sub',
      toJSON: () => ({
        endpoint: 'https://push.example/sub',
        keys: { p256dh: 'p256', auth: 'auth' },
      }),
      getKey: vi.fn(),
    } as unknown as PushSubscription
    const subscribe = vi.fn().mockResolvedValue(subscription)
    const registration = {
      pushManager: {
        getSubscription: vi.fn().mockResolvedValue(null),
        subscribe,
      },
    }
    mockedGetVapidPublicKey.mockResolvedValue({
      public_key: 'AQIDBA',
      configured: true,
    })
    mockedCreatePushSubscription.mockResolvedValue({
      id: 'sub-1',
      endpoint: 'https://push.example/sub',
      last_seen_at: '2026-06-20T00:00:00Z',
    })
    vi.stubGlobal('window', {
      atob: (value: string) => atob(value),
      PushManager: function PushManager() {},
      Notification: {},
    })
    vi.stubGlobal('navigator', {
      serviceWorker: { ready: Promise.resolve(registration) },
    })
    vi.stubGlobal('Notification', {
      permission: 'default',
      requestPermission: vi.fn().mockResolvedValue('granted'),
    })

    await expect(enableWebPushSubscription()).resolves.toBe('supported')

    expect(subscribe).toHaveBeenCalledWith({
      userVisibleOnly: true,
      applicationServerKey: new Uint8Array([1, 2, 3, 4]),
    })
    expect(mockedCreatePushSubscription).toHaveBeenCalledWith({
      endpoint: 'https://push.example/sub',
      keys: { p256dh: 'p256', auth: 'auth' },
    })
  })

  it('raises a typed, coded error when the browser subscription is missing keys', async () => {
    const subscription = {
      endpoint: '',
      toJSON: () => ({ endpoint: '', keys: {} }),
      getKey: vi.fn().mockReturnValue(null),
    } as unknown as PushSubscription
    const registration = {
      pushManager: {
        getSubscription: vi.fn().mockResolvedValue(null),
        subscribe: vi.fn().mockResolvedValue(subscription),
      },
    }
    mockedGetVapidPublicKey.mockResolvedValue({
      public_key: 'AQIDBA',
      configured: true,
    })
    vi.stubGlobal('window', {
      atob: (value: string) => atob(value),
      PushManager: function PushManager() {},
      Notification: {},
    })
    vi.stubGlobal('navigator', {
      serviceWorker: { ready: Promise.resolve(registration) },
    })
    vi.stubGlobal('Notification', {
      permission: 'granted',
      requestPermission: vi.fn().mockResolvedValue('granted'),
    })

    await expect(enableWebPushSubscription()).rejects.toBeInstanceOf(PushSubscriptionIncompleteError)
    await expect(enableWebPushSubscription()).rejects.toMatchObject({
      code: 'push_subscription_incomplete',
    })
    expect(mockedCreatePushSubscription).not.toHaveBeenCalled()
  })

  it('shows background fallback notifications even when a push subscription exists', async () => {
    const payload = {
      title: '新訊息',
      body: '等等想跟你說件事',
      icon: '/avatar.png',
      url: '/?character=char-1',
      type: 'proactive',
      characterId: 'char-1',
    }
    const getSubscription = vi.fn().mockResolvedValue({
      endpoint: 'https://push.example/sub',
    })
    const showNotification = vi.fn().mockResolvedValue(undefined)
    const registration = {
      pushManager: { getSubscription },
      showNotification,
    }
    vi.stubGlobal('document', { visibilityState: 'hidden' })
    vi.stubGlobal('window', { Notification: function Notification() {} })
    vi.stubGlobal('navigator', {
      serviceWorker: { ready: Promise.resolve(registration) },
    })
    vi.stubGlobal('Notification', { permission: 'granted' })

    await expect(showBackgroundPageNotification(payload)).resolves.toBe(true)

    const expectedTag = localNotificationTag(payload)
    expect(expectedTag).toBe(localNotificationTag({
      ...payload,
      url: 'https://app.example/?character=char-1',
    }))
    expect(getSubscription).not.toHaveBeenCalled()
    expect(showNotification).toHaveBeenCalledWith('新訊息', {
      body: '等等想跟你說件事',
      icon: '/avatar.png',
      badge: '/favicon.png',
      tag: expectedTag,
      data: {
        url: '/?character=char-1',
        type: 'proactive',
        character_id: 'char-1',
      },
    })
  })
})
