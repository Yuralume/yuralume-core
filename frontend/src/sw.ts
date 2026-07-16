/// <reference lib="webworker" />

import { cleanupOutdatedCaches, precacheAndRoute } from 'workbox-precaching'

export {}

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<unknown>
}

precacheAndRoute(self.__WB_MANIFEST)
cleanupOutdatedCaches()

interface PushPayload {
  type?: string
  character_id?: string
  title?: string
  body?: string
  icon?: string | null
  url?: string
  tag?: string
  dedupe_key?: string
}

self.addEventListener('push', (event) => {
  event.waitUntil(handlePush(event))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = String(event.notification.data?.url || '/')
  event.waitUntil(openOrFocus(url))
})

async function handlePush(event: PushEvent): Promise<void> {
  const payload = parsePayload(event)
  if (await hasFocusedClient()) return
  await self.registration.showNotification(payload.title || 'Yuralume', {
    body: payload.body,
    icon: payload.icon || '/logo-mark.png',
    badge: '/favicon.png',
    tag: payload.tag || pushNotificationTag(payload),
    data: {
      url: payload.url || '/',
      type: payload.type,
      character_id: payload.character_id,
    },
  })
}

function parsePayload(event: PushEvent): PushPayload {
  if (!event.data) return {}
  try {
    return event.data.json() as PushPayload
  } catch {
    return { body: event.data.text() }
  }
}

async function hasFocusedClient(): Promise<boolean> {
  const clients = await self.clients.matchAll({
    type: 'window',
    includeUncontrolled: true,
  })
  return clients.some((client) => client.focused)
}

function pushNotificationTag(payload: PushPayload): string {
  const source = [
    payload.type || 'notification',
    payload.character_id || '',
    notificationUrlKey(payload.url),
    payload.dedupe_key || payload.body || '',
  ].join('|')
  return `yuralume:${hashStableString(source)}`
}

function notificationUrlKey(url: string | undefined): string {
  if (!url) return ''
  try {
    const parsed = new URL(url, self.location.origin)
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    return url
  }
}

function hashStableString(value: string): string {
  let hash = 5381
  for (let i = 0; i < value.length; i += 1) {
    hash = ((hash * 33) ^ value.charCodeAt(i)) >>> 0
  }
  return hash.toString(36)
}

async function openOrFocus(url: string): Promise<void> {
  const target = new URL(url, self.location.origin)
  const clients = await self.clients.matchAll({
    type: 'window',
    includeUncontrolled: true,
  })
  for (const client of clients) {
    const clientUrl = new URL(client.url)
    if (clientUrl.origin === target.origin) {
      await client.focus()
      client.postMessage({
        type: 'yuralume:notification-click',
        url: target.pathname + target.search + target.hash,
      })
      return
    }
  }
  await self.clients.openWindow(target.href)
}
