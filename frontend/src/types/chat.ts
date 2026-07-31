import type { CharacterState } from './character'
import type { MessageAttachment } from './tool'

export type MessageRole = 'user' | 'assistant' | 'system'

export interface ChatMessage {
  role: MessageRole
  content: string
  attachments?: MessageAttachment[]
  turn_record_id?: string | null
}

export type ChatSurface = 'web_stage' | 'web_dm' | 'messaging'
export type ChatChannel = 'kokoro_stage' | 'kokoro_dm' | 'telegram' | 'line' | 'discord' | 'whatsapp' | 'unknown'
export type VisibilityMode = 'virtual_same_space' | 'text_only' | 'text_and_attachments'
/**
 * How this turn's co-presence is framed.
 *
 * Since the same-space judge retired (plan SA, D1/D2) this client produces
 * exactly two values: `player_declared` for a same-space turn — the player
 * declared they are there, and plausibility is handled *inside* the
 * narrative by the character anchoring its own schedule — and
 * `text_message_only` for every phone / external-messaging turn.
 *
 * The retired judge's verdict values are deliberately absent from this
 * union. The backend still parses and folds them, so already-deployed
 * self-hosted frontends and stored turn metadata keep working; but nothing
 * here may produce one again, and listing them would make that a
 * type-legal mistake.
 */
export type AccessContext = 'player_declared' | 'text_message_only'

export interface PresenceFramePayload {
  surface: ChatSurface
  channel: ChatChannel
  visibility: VisibilityMode
  display_name?: string | null
  access_context?: AccessContext | null
}

export interface SendChatMessageRequest {
  character_id: string
  conversation_id?: string | null
  provider_id?: string
  model_id?: string
  message: string
  attachment_urls?: string[]
  presence_frame?: PresenceFramePayload | null
  /**
   * The chat price this screen was quoting when the player pressed send, so
   * the charge binds to what they actually saw rather than to whatever the
   * server's own cache holds. Attached by `sendChatMessage` /
   * `sendChatMessageStream`; omitted when no price is quotable.
   */
  quoted_price_cr?: number
  /** The same, for the picture this turn may spawn (`image_chat_tool`). */
  quoted_image_price_cr?: number
}

export interface ChatReplyResponse {
  conversation_id: string
  user_message: ChatMessage
  assistant_message?: ChatMessage | null
  state: CharacterState
}

export interface ConversationSnapshot {
  id: string
  character_id: string
  messages: ChatMessage[]
}

// NOTE: the client intentionally does NOT send `display_name` (plan #1 /
// D4). A hard-coded zh label used to flow verbatim into the prompt,
// pinning the presence line to Chinese for en/ja operators. The backend
// now derives the channel display name from the `channel` enum honouring
// the operator's language, so the client only sends the structural enums.
//
// Same-space is the player's own declaration (plan SA, D2): there is no
// verdict to consult and nothing to justify, so the frame is a pure
// function of "did this turn carry pictures".
export function webStagePresenceFrame(hasAttachments = false): PresenceFramePayload {
  return {
    surface: 'web_stage',
    channel: 'kokoro_stage',
    visibility: hasAttachments ? 'text_and_attachments' : 'virtual_same_space',
    access_context: 'player_declared',
  }
}
export function webDmPresenceFrame(hasAttachments = false): PresenceFramePayload {
  return {
    surface: 'web_dm',
    channel: 'kokoro_dm',
    visibility: hasAttachments ? 'text_and_attachments' : 'text_only',
    access_context: 'text_message_only',
  }
}
