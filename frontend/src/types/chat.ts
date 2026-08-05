import type { CharacterState } from './character'
import type { MessageAttachment } from './tool'
import type {
  StorySceneSession,
  StorySceneSuggestedAction,
} from './storyScene'

export type MessageRole = 'user' | 'assistant' | 'system'

/**
 * What a message *is*, beyond who said it (plan SC, ticket SC1-A).
 *
 * `chat` is somebody speaking and renders as a bubble; `scene_narration` is
 * the story speaking and renders as a scene frame; `tool_only` is an
 * assistant row whose whole payload is a tool artifact (a bare `/pic` image)
 * with no words — a backend-side context-gating marker that the thread
 * nonetheless renders as ordinary chat.
 *
 * All three values the backend's `MessageKind` can emit are listed, because
 * the server sends `kind` verbatim (`ChatMessageResponse.kind`) and a union
 * that omitted one would make the honest payload a type error while changing
 * nothing about how it renders.
 *
 * Optional on purpose: every message stored before scenes existed, and every
 * backend older than SC1-A, omits it, and those must keep rendering as
 * ordinary chat.
 */
export type ChatMessageKind = 'chat' | 'tool_only' | 'scene_narration'

export interface ChatMessage {
  role: MessageRole
  content: string
  attachments?: MessageAttachment[]
  turn_record_id?: string | null
  kind?: ChatMessageKind | null
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
  /**
   * Suggested next actions for a turn played inside a story scene (SC1-C).
   *
   * Carried by the ordinary chat response — non-streaming here, and inside the
   * `done` frame's `response` when streaming — so a scene turn stays one
   * round trip. Absent outside a scene, and absent when generating them
   * failed: chips are a courtesy and never hold up a reply.
   */
  suggested_actions?: StorySceneSuggestedAction[] | null
  /**
   * The scene this turn *ended* (SC1-D), when its own verdict wrapped one up.
   *
   * Absent on every other turn — including every turn of a scene that is
   * still running — so its arrival, carrying `status: 'closed'`, is the
   * signal. Without it the player would have to reload to discover the story
   * answered its own dramatic question.
   */
  story_scene_session?: StorySceneSession | null
  /**
   * The send-off appended to the thread (`kind: 'scene_narration'`).
   *
   * Independently nullable from `story_scene_session`: a scene can close
   * without prose (no writer, or a draft refused for inventing the player's
   * actions), and the close is the half that matters.
   */
  story_scene_closing?: ChatMessage | null
}

export interface ConversationSnapshot {
  id: string
  character_id: string
  /** One page of the thread, oldest-first. */
  messages: ChatMessage[]
  /**
   * Whether messages older than `messages[0]` exist (IV10).
   *
   * Optional because the field is a superset added to a shape that already
   * shipped: a backend that predates pagination sends the whole thread and no
   * flag, and `undefined` must read as "there is nothing more to fetch".
   */
  has_more?: boolean
  /**
   * The `position` of this page's oldest message — pass back as `before` for
   * the page before it. Null/absent once `has_more` is false, so the client
   * can stop without a probing request.
   */
  next_before?: number | null
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
