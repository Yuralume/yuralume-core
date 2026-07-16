import {
  createPushSubscription,
  deletePushSubscription,
  getVapidPublicKey,
  type PushSubscriptionPayload,
} from '@/utils/api/push'

export type PushSupportState =
  | 'supported'
  | 'unsupported'
  | 'unconfigured'
  | 'denied'

/**
 * Raised when the browser's PushSubscription is missing fields we need
 * to register it server-side. ``code`` lets the display boundary map
 * this to a localized message instead of showing the English literal
 * baked into ``Error#message``.
 */
export class PushSubscriptionIncompleteError extends Error {
  code = 'push_subscription_incomplete' as const

  constructor(message: string) {
    super(message)
    this.name = 'PushSubscriptionIncompleteError'
  }
}

export interface LocalNotificationPayload {
  title: string
  body?: string
  icon?: string | null
  url?: string
  type?: string
  characterId?: string
  dedupeKey?: string
}

export function isWebPushSupported(): boolean {
  return (
    typeof window !== 'undefined'
    && 'Notification' in window
    && 'serviceWorker' in navigator
    && 'PushManager' in window
  )
}

export function urlBase64ToUint8Array(
  base64String: string,
): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding)
    .replace(/-/g, '+')
    .replace(/_/g, '/')
  const rawData = window.atob(base64)
  const outputArray = new Uint8Array(new ArrayBuffer(rawData.length))
  for (let i = 0; i < rawData.length; i += 1) {
    outputArray[i] = rawData.charCodeAt(i)
  }
  return outputArray
}

export async function resolvePushSupportState(): Promise<PushSupportState> {
  if (!isWebPushSupported()) return 'unsupported'
  if (Notification.permission === 'denied') return 'denied'
  const key = await getVapidPublicKey()
  return key.configured && key.public_key ? 'supported' : 'unconfigured'
}

export async function getCurrentPushSubscription(): Promise<PushSubscription | null> {
  if (!isWebPushSupported()) return null
  const registration = await navigator.serviceWorker.ready
  return registration.pushManager.getSubscription()
}

export async function enableWebPushSubscription(): Promise<PushSupportState> {
  if (!isWebPushSupported()) return 'unsupported'
  const key = await getVapidPublicKey()
  if (!key.configured || !key.public_key) return 'unconfigured'
  if (Notification.permission !== 'granted') {
    const permission = await Notification.requestPermission()
    if (permission !== 'granted') return 'denied'
  }
  const registration = await navigator.serviceWorker.ready
  const existing = await registration.pushManager.getSubscription()
  const subscription = existing ?? await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(key.public_key),
  })
  await createPushSubscription(subscriptionPayload(subscription))
  return 'supported'
}

export async function disableWebPushSubscription(): Promise<void> {
  const subscription = await getCurrentPushSubscription()
  if (subscription === null) return
  const endpoint = subscription.endpoint
  try {
    await subscription.unsubscribe()
  } finally {
    await deletePushSubscription(endpoint)
  }
}

export async function showBackgroundPageNotification(
  payload: LocalNotificationPayload,
): Promise<boolean> {
  if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
    return false
  }
  if (typeof window === 'undefined' || !('Notification' in window)) {
    return false
  }
  if (Notification.permission !== 'granted') return false

  const tag = localNotificationTag(payload)
  const data = {
    url: payload.url ?? '/',
    type: payload.type,
    character_id: payload.characterId,
  }
  if ('serviceWorker' in navigator) {
    const registration = await navigator.serviceWorker.ready
    await registration.showNotification(payload.title, {
      body: payload.body,
      icon: payload.icon ?? undefined,
      badge: '/favicon.png',
      tag,
      data,
    })
    return true
  }

  const notification = new Notification(payload.title, {
    body: payload.body,
    icon: payload.icon ?? undefined,
    tag,
    data,
  })
  notification.onclick = () => {
    window.focus()
    if (payload.url) window.location.href = payload.url
  }
  return true
}

export function localNotificationTag(
  payload: LocalNotificationPayload,
): string {
  const source = [
    payload.type ?? 'notification',
    payload.characterId ?? '',
    notificationUrlKey(payload.url),
    payload.dedupeKey ?? payload.body ?? '',
  ].join('|')
  return `yuralume:${hashStableString(source)}`
}

function notificationUrlKey(url: string | undefined): string {
  if (!url) return ''
  try {
    const origin = typeof window !== 'undefined' && window.location?.origin
      ? window.location.origin
      : 'http://yuralume.local'
    const parsed = new URL(url, origin)
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    return url
  }
}

function subscriptionPayload(
  subscription: PushSubscription,
): PushSubscriptionPayload {
  const json = subscription.toJSON() as {
    endpoint?: string
    keys?: { p256dh?: string, auth?: string }
  }
  const p256dh = json.keys?.p256dh
    ?? keyToBase64Url(subscription.getKey('p256dh'))
  const auth = json.keys?.auth
    ?? keyToBase64Url(subscription.getKey('auth'))
  if (!subscription.endpoint || !p256dh || !auth) {
    throw new PushSubscriptionIncompleteError('Push subscription is missing endpoint or keys')
  }
  return {
    endpoint: json.endpoint ?? subscription.endpoint,
    keys: { p256dh, auth },
  }
}

function keyToBase64Url(buffer: ArrayBuffer | null): string {
  if (buffer === null) return ''
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return window.btoa(binary)
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/g, '')
}

function hashStableString(value: string): string {
  let hash = 5381
  for (let i = 0; i < value.length; i += 1) {
    hash = ((hash * 33) ^ value.charCodeAt(i)) >>> 0
  }
  return hash.toString(36)
}
