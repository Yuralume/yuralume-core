import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  createPushSubscription,
  deletePushSubscription,
  getNotificationPreferences,
  getVapidPublicKey,
  updateNotificationPreferences,
  type NotificationPreferences,
} from '@/utils/api/push'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('push API client', () => {
  it('reads VAPID public key', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: { public_key: 'public', configured: true },
    })

    await expect(getVapidPublicKey()).resolves.toEqual({
      public_key: 'public',
      configured: true,
    })

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/push/vapid-public-key',
    )
  })

  it('creates and deletes subscriptions', async () => {
    const payload = {
      endpoint: 'https://push.example/sub',
      keys: { p256dh: 'p256', auth: 'auth' },
    }
    mockedAxios.post.mockResolvedValueOnce({
      data: {
        id: 'sub-1',
        endpoint: payload.endpoint,
        last_seen_at: '2026-06-20T00:00:00Z',
      },
    })
    mockedAxios.delete.mockResolvedValueOnce({ data: undefined })

    await expect(createPushSubscription(payload)).resolves.toEqual({
      id: 'sub-1',
      endpoint: payload.endpoint,
      last_seen_at: '2026-06-20T00:00:00Z',
    })
    await deletePushSubscription(payload.endpoint)

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/push/subscriptions',
      payload,
    )
    expect(mockedAxios.delete).toHaveBeenCalledWith(
      '/api/v1/push/subscriptions',
      { data: { endpoint: payload.endpoint } },
    )
  })

  it('roundtrips notification preferences', async () => {
    const preferences: NotificationPreferences = {
      proactive_enabled: true,
      feed_reply_enabled: true,
      feed_post_enabled: false,
      studio_enabled: true,
      content_preview_enabled: true,
      suppress_when_external_delivered: true,
    }
    mockedAxios.get.mockResolvedValueOnce({ data: preferences })
    mockedAxios.put.mockResolvedValueOnce({
      data: { ...preferences, feed_post_enabled: true },
    })

    await expect(getNotificationPreferences()).resolves.toEqual(preferences)
    await expect(updateNotificationPreferences({
      ...preferences,
      feed_post_enabled: true,
    })).resolves.toEqual({ ...preferences, feed_post_enabled: true })

    expect(mockedAxios.get).toHaveBeenCalledWith('/api/v1/push/preferences')
    expect(mockedAxios.put).toHaveBeenCalledWith(
      '/api/v1/push/preferences',
      { ...preferences, feed_post_enabled: true },
    )
  })
})
