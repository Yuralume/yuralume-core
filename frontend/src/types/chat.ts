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
export type AccessContext =
  // Legacy compatibility only. New stage access verdicts should not use this.
  | 'remote_stage'
  | 'public_encounter'
  | 'invited_visit'
  | 'scheduled_meetup'
  | 'established_routine'
  | 'text_message_only'
  | 'not_plausible'

export type StageAccessDecision = 'allow' | 'warn' | 'block'
export type StageAccessAction =
  | 'use_stage'
  | 'use_phone'
  | 'ask_to_meet'
  | 'wait_for_open_scene'

export interface StageAccessVerdict {
  decision: StageAccessDecision
  recommended_action: StageAccessAction
  access_context: AccessContext
  reason_for_user: string
  prompt_fact: string
  suggested_opener?: string | null
}

export interface PresenceFramePayload {
  surface: ChatSurface
  channel: ChatChannel
  visibility: VisibilityMode
  display_name?: string | null
  access_context?: AccessContext | null
  co_presence_reason?: string | null
  stage_access_note?: string | null
}

export interface SendChatMessageRequest {
  character_id: string
  conversation_id?: string | null
  provider_id?: string
  model_id?: string
  message: string
  attachment_urls?: string[]
  presence_frame?: PresenceFramePayload | null
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

const REAL_STAGE_ACCESS_CONTEXTS = new Set<AccessContext>([
  'public_encounter',
  'invited_visit',
  'scheduled_meetup',
  'established_routine',
])

export function isRealStageAccessContext(accessContext?: AccessContext | null): boolean {
  return !!accessContext && REAL_STAGE_ACCESS_CONTEXTS.has(accessContext)
}

export function canUseStageAccess(verdict?: StageAccessVerdict | null): verdict is StageAccessVerdict {
  return !!verdict && verdict.decision !== 'block' && isRealStageAccessContext(verdict.access_context)
}

// NOTE: the client intentionally does NOT send `display_name` (plan #1 /
// D4). A hard-coded zh label used to flow verbatim into the prompt,
// pinning the presence line to Chinese for en/ja operators. The backend
// now derives the channel display name from the `channel` enum honouring
// the operator's language, so the client only sends the structural enums.
export function webStagePresenceFrame(
  hasAttachments = false,
  stageAccess?: StageAccessVerdict | null,
): PresenceFramePayload {
  const canUseStage = canUseStageAccess(stageAccess)
  return {
    surface: 'web_stage',
    channel: 'kokoro_stage',
    visibility: hasAttachments ? 'text_and_attachments' : 'virtual_same_space',
    access_context: canUseStage ? stageAccess.access_context : 'not_plausible',
    co_presence_reason: canUseStage ? stageAccess.reason_for_user : null,
    stage_access_note: canUseStage ? stageAccess.prompt_fact : null,
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
