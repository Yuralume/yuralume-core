// Thin API client. Slice 6.3 will expand this with account CRUD;
// for now we expose the raw shapes so early adopters can script
// against them from the browser console if needed.

import axios from 'axios'
import { getStoredToken } from '@/composables/useAuth'
import type {
  ChannelBinding,
  CreateChannelBindingRequest,
  CreateMessagingAccountRequest,
  MessagingSettingsResponse,
  MessagingAccount,
  UpdateMessagingSettingsRequest,
  UpdateMessagingAccountRequest,
  WebhookRegisterResponse,
  WebhookStatusResponse,
} from '@/types/messaging'

const BASE = '/api/v1/messaging'

export async function getMessagingSettings(): Promise<MessagingSettingsResponse> {
  const { data } = await axios.get<MessagingSettingsResponse>(`${BASE}/settings`)
  return data
}

export async function updateMessagingSettings(
  req: UpdateMessagingSettingsRequest,
): Promise<MessagingSettingsResponse> {
  const { data } = await axios.put<MessagingSettingsResponse>(`${BASE}/settings`, req)
  return data
}

export async function listAccounts(characterId: string): Promise<MessagingAccount[]> {
  const { data } = await axios.get<MessagingAccount[]>(`${BASE}/accounts`, {
    params: { character_id: characterId },
  })
  return data
}

export async function createAccount(
  req: CreateMessagingAccountRequest,
): Promise<MessagingAccount> {
  const { data } = await axios.post<MessagingAccount>(`${BASE}/accounts`, req)
  return data
}

export async function updateAccount(
  accountId: string,
  req: UpdateMessagingAccountRequest,
): Promise<MessagingAccount> {
  const { data } = await axios.patch<MessagingAccount>(
    `${BASE}/accounts/${accountId}`,
    req,
  )
  return data
}

export async function deleteAccount(accountId: string): Promise<void> {
  await axios.delete(`${BASE}/accounts/${accountId}`)
}

export async function listBindings(accountId: string): Promise<ChannelBinding[]> {
  const { data } = await axios.get<ChannelBinding[]>(`${BASE}/bindings`, {
    params: { account_id: accountId },
  })
  return data
}

export async function createBinding(
  req: CreateChannelBindingRequest,
): Promise<ChannelBinding> {
  const { data } = await axios.post<ChannelBinding>(`${BASE}/bindings`, req)
  return data
}

export async function setBindingEnabled(
  bindingId: string,
  enabled: boolean,
): Promise<ChannelBinding> {
  const { data } = await axios.patch<ChannelBinding>(
    `${BASE}/bindings/${bindingId}`,
    { enabled },
  )
  return data
}

export async function setBindingAcceptsProactive(
  bindingId: string,
  acceptsProactive: boolean,
): Promise<ChannelBinding> {
  const { data } = await axios.patch<ChannelBinding>(
    `${BASE}/bindings/${bindingId}`,
    { accepts_proactive: acceptsProactive },
  )
  return data
}

export async function deleteBinding(bindingId: string): Promise<void> {
  await axios.delete(`${BASE}/bindings/${bindingId}`)
}

export async function registerWebhook(
  accountId: string,
  publicBaseUrl?: string,
): Promise<WebhookRegisterResponse> {
  const { data } = await axios.post<WebhookRegisterResponse>(
    `${BASE}/accounts/${accountId}/webhook/register`,
    publicBaseUrl ? { public_base_url: publicBaseUrl } : {},
  )
  return data
}

export async function getWebhookStatus(
  accountId: string,
): Promise<WebhookStatusResponse> {
  const { data } = await axios.get<WebhookStatusResponse>(
    `${BASE}/accounts/${accountId}/webhook/status`,
  )
  return data
}

export function whatsappQrSvgUrl(accountId: string): string {
  const params = new URLSearchParams()
  const token = getStoredToken()
  if (token) {
    // Native image requests cannot send Authorization headers. Backend auth
    // accepts this GET-only fallback for the same browser limitation used by
    // SSE and character-card image downloads.
    params.set('access_token', token)
  }
  const query = params.toString()
  return `${BASE}/accounts/${accountId}/whatsapp/qr.svg${query ? `?${query}` : ''}`
}
