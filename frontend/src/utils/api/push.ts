import axios from 'axios'

const BASE = '/api/v1/push'

export interface VapidPublicKeyResponse {
  public_key: string
  configured: boolean
}

export interface PushSubscriptionPayload {
  endpoint: string
  keys: {
    p256dh: string
    auth: string
  }
}

export interface PushSubscriptionResponse {
  id: string
  endpoint: string
  last_seen_at: string
}

export interface NotificationPreferences {
  proactive_enabled: boolean
  feed_reply_enabled: boolean
  feed_post_enabled: boolean
  studio_enabled: boolean
  content_preview_enabled: boolean
  suppress_when_external_delivered: boolean
}

export async function getVapidPublicKey(): Promise<VapidPublicKeyResponse> {
  const { data } = await axios.get<VapidPublicKeyResponse>(
    `${BASE}/vapid-public-key`,
  )
  return data
}

export async function createPushSubscription(
  payload: PushSubscriptionPayload,
): Promise<PushSubscriptionResponse> {
  const { data } = await axios.post<PushSubscriptionResponse>(
    `${BASE}/subscriptions`,
    payload,
  )
  return data
}

export async function deletePushSubscription(endpoint: string): Promise<void> {
  await axios.delete(`${BASE}/subscriptions`, {
    data: { endpoint },
  })
}

export async function getNotificationPreferences(): Promise<NotificationPreferences> {
  const { data } = await axios.get<NotificationPreferences>(
    `${BASE}/preferences`,
  )
  return data
}

export async function updateNotificationPreferences(
  payload: NotificationPreferences,
): Promise<NotificationPreferences> {
  const { data } = await axios.put<NotificationPreferences>(
    `${BASE}/preferences`,
    payload,
  )
  return data
}
