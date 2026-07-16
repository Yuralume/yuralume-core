export type MessagingPlatform = 'telegram' | 'line' | 'discord' | 'whatsapp'
export type DeliveryMode = 'webhook' | 'polling' | 'gateway'

export interface PollingStatus {
  enabled: boolean
  running: boolean
  last_update_at: string | null
  last_error: string | null
}

export interface MessagingAccount {
  id: string
  character_id: string
  platform: MessagingPlatform
  display_name: string
  webhook_slug: string
  delivery_mode: DeliveryMode
  allowed_sender_refs: string[]
  enabled: boolean
  polling_status: PollingStatus
  created_at: string
  updated_at: string
  has_credentials: boolean
}

export interface CreateMessagingAccountRequest {
  character_id: string
  platform: MessagingPlatform
  display_name?: string
  credentials: Record<string, string>
  allowed_sender_refs?: string[]
  enabled?: boolean
}

export interface UpdateMessagingAccountRequest {
  display_name?: string | null
  credentials?: Record<string, string> | null
  allowed_sender_refs?: string[] | null
  enabled?: boolean | null
}

export interface ChannelBinding {
  id: string
  account_id: string
  chat_ref: string
  conversation_id: string | null
  enabled: boolean
  accepts_proactive: boolean
  created_at: string
  updated_at: string
}

export interface CreateChannelBindingRequest {
  account_id: string
  chat_ref: string
  enabled?: boolean
  accepts_proactive?: boolean
}

export interface UpdateChannelBindingRequest {
  enabled?: boolean
  accepts_proactive?: boolean
}

export interface WebhookRegisterResponse {
  ok: boolean
  webhook_url: string
  message?: string | null
  platform_response?: Record<string, unknown> | null
}

export interface WebhookStatusResponse {
  ok: boolean
  info?: Record<string, unknown> | null
  message?: string | null
}

export interface MessagingSettingsResponse {
  public_base_url: string
  effective_public_base_url: string
  source: 'preference' | 'env' | 'empty' | string
  telegram_delivery_mode: DeliveryMode
}

export interface UpdateMessagingSettingsRequest {
  public_base_url?: string | null
  telegram_delivery_mode?: DeliveryMode | null
}
