import type {
  ConversationSnapshot,
  SendChatMessageRequest,
  ChatReplyResponse,
} from '@/types/chat'
import type { Character } from '@/types/character'
import { authedFetch } from '@/utils/authedFetch'
import {
  InsufficientCreditsError,
  insufficientCreditsFromBody,
  insufficientCreditsFromStreamFrame,
} from '@/utils/api/insufficientCredits'
import {
  priceChangedFromBody,
  priceChangedFromStreamFrame,
} from '@/utils/api/priceChanged'
import {
  ACTION_CHAT,
  ACTION_IMAGE_CHAT_TOOL,
  useActionPricing,
} from '@/composables/useActionPricing'

/**
 * Re-exported so chat call sites can catch every typed chat outcome from one
 * module. The definition lives in ``insufficientCredits`` because image / TTS
 * / studio entry points raise the same error.
 */
export { InsufficientCreditsError }

export class ChatRuntimeLimitError extends Error {
  code: string
  statusCode: number

  constructor(input: {
    code: string
    message: string
    statusCode: number
  }) {
    super(input.message)
    this.name = 'ChatRuntimeLimitError'
    this.code = input.code
    this.statusCode = input.statusCode
  }
}

/**
 * Raised for stream-protocol failures that originate on the client side
 * (not an HTTP error response) — e.g. the SSE stream closed before a
 * final ``done`` event arrived. ``code`` lets the display boundary map
 * this to a localized message instead of showing the English literal
 * baked into ``Error#message``.
 */
export class ChatStreamProtocolError extends Error {
  code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'ChatStreamProtocolError'
    this.code = code
  }
}

/**
 * How many messages one thread page carries (IV10).
 *
 * Mirrors the server's own default; sent explicitly anyway so the number the
 * panel reasons about is the number it asked for, not whatever the deployed
 * backend happens to default to.
 */
export const CONVERSATION_PAGE_SIZE = 50

export interface ConversationPageQuery {
  /** Messages per page; omitted means `CONVERSATION_PAGE_SIZE`. */
  limit?: number
  /**
   * Fetch the page immediately older than this message `position`
   * (exclusive) — the `next_before` of the page you already hold. Omitted
   * means the newest page.
   */
  before?: number | null
}

/**
 * Fetch one page of the most recently active conversation for a character.
 * Returns null when the character has no prior conversations.
 *
 * Which conversation is "most recent" is a server-side decision and is not
 * affected by these parameters — they only move the window over its messages.
 */
export async function getLatestConversation(
  characterId: string,
  query: ConversationPageQuery = {},
): Promise<ConversationSnapshot | null> {
  const params = new URLSearchParams({
    limit: String(query.limit ?? CONVERSATION_PAGE_SIZE),
  })
  if (query.before !== undefined && query.before !== null) {
    params.set('before', String(query.before))
  }
  const res = await authedFetch(
    `/api/v1/characters/${characterId}/conversations/latest?${params.toString()}`,
  )
  if (!res.ok) {
    throw new Error(`Failed to load conversation history: ${res.status}`)
  }
  const text = await res.text()
  if (!text || text === 'null') return null
  return JSON.parse(text) as ConversationSnapshot
}

/**
 * Upload image files the user wants to attach to the next chat turn.
 * Returns server-relative URLs that should be passed as
 * ``attachment_urls`` on the subsequent send.
 */
export async function uploadChatAttachments(files: File[]): Promise<string[]> {
  if (files.length === 0) return []
  const form = new FormData()
  for (const file of files) form.append('files', file, file.name)
  const res = await authedFetch('/api/v1/chat/uploads', {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    let msg = `Upload failed: ${res.status}`
    try {
      const data = await res.json()
      if (data?.detail) msg = `Upload failed: ${data.detail}`
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  const data = await res.json() as { urls: string[] }
  return Array.isArray(data.urls) ? data.urls : []
}

/**
 * Summary returned by the undo-last-turn endpoint. The frontend only
 * surfaces ``reverted_messages`` + ``restored_character_state`` in
 * toasts; the rest is logged for debugging.
 */
export interface UndoTurnResponse {
  conversation_id: string
  turn_index: number
  reverted_messages: number
  deleted_memories: number
  deleted_state_snapshots: number
  restored_goals: boolean
  restored_arc: boolean
  restored_schedule: boolean
  restored_character_state: boolean
}

/**
 * Reverse the most recent turn of ``conversationId``. Throws when the
 * server has no journal for this conversation (409) — typically means
 * the user already undid everything or the conversation just started.
 */
export async function undoLastTurn(conversationId: string): Promise<UndoTurnResponse> {
  const res = await authedFetch(
    `/api/v1/conversations/${conversationId}/turns/undo`,
    { method: 'POST' },
  )
  if (!res.ok) {
    let msg = `Undo failed: ${res.status}`
    try {
      const data = await res.json()
      if (data?.detail) msg = data.detail
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json() as Promise<UndoTurnResponse>
}

/**
 * Mark the character's web conversation as read — zeros out the
 * proactive unread badge on the sidebar. Idempotent on the server.
 */
export async function markConversationRead(characterId: string): Promise<Character> {
  const res = await authedFetch(
    `/api/v1/characters/${characterId}/conversations/mark-read`,
    { method: 'POST' },
  )
  if (!res.ok) {
    throw new Error(`Failed to mark conversation read: ${res.status}`)
  }
  return res.json() as Promise<Character>
}

/**
 * Attach the prices this client is quoting to a chat send (plan R9).
 *
 * The server binds the charge to a price and refuses (`409 price_changed`)
 * when it moved — but "the price we quoted" was, until now, read from the
 * server's own process-local cache. With several hosted replicas, or right
 * after a back-office edit, that number is not necessarily the one this
 * player's screen showed. Sending it from here closes that gap: the charge is
 * bound to what was actually on screen when they pressed send.
 *
 * Both fields are optional and dropped when nothing is quotable (self-host, a
 * token-billed tier, a price list that has not loaded) — the server then falls
 * back to its cache exactly as before. Nothing is gained by lying: a low quote
 * is refused, never charged.
 *
 * Set on the transport rather than at each call site so every chat entry point
 * — composer, retry, external surfaces — carries the same binding.
 */
function withQuotedPrices(req: SendChatMessageRequest): SendChatMessageRequest {
  const pricing = useActionPricing()
  const chat = pricing.priceOf(ACTION_CHAT)
  const image = pricing.priceOf(ACTION_IMAGE_CHAT_TOOL)
  if (chat === null && image === null) return req
  return {
    ...req,
    ...(chat === null ? {} : { quoted_price_cr: chat.price_cr }),
    ...(image === null ? {} : { quoted_image_price_cr: image.price_cr }),
  }
}

/**
 * Send a chat message and stream the assistant reply via SSE.
 * Falls back to non-streaming if the streaming endpoint is not available.
 */
export async function sendChatMessage(req: SendChatMessageRequest): Promise<ChatReplyResponse> {
  const res = await authedFetch('/api/v1/chat/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(withQuotedPrices(req)),
  })
  if (!res.ok) {
    throw await chatErrorFromResponse(res, 'Chat request failed')
  }
  return res.json()
}

/**
 * Send a chat message and receive streaming SSE tokens.
 * Calls onToken for each incremental text chunk.
 * Returns the full ChatReplyResponse once the stream ends.
 */
export async function sendChatMessageStream(
  req: SendChatMessageRequest,
  onToken: (token: string) => void,
  onConversationId?: (id: string) => void,
): Promise<ChatReplyResponse> {
  const res = await authedFetch('/api/v1/chat/messages/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(withQuotedPrices(req)),
  })
  if (!res.ok) {
    throw await chatErrorFromResponse(res, 'Stream chat request failed')
  }

  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finalResponse: ChatReplyResponse | null = null
  // Terminal ``error`` frames land here rather than throwing from inside
  // ``processLine``: the reader loop must still drain (and the trailing
  // ``[DONE]`` still be consumed) before we surface the failure. An array
  // rather than a nullable local so only the *first* error wins and TypeScript
  // sees the closure write.
  const streamErrors: Error[] = []

  const processLine = (line: string): void => {
    if (!line.startsWith('data: ')) return
    const payload = line.slice(6).trim()
    if (!payload || payload === '[DONE]') return
    try {
      const parsed = JSON.parse(payload)
      // A refusal after the SSE headers are already on the wire cannot be an
      // HTTP status, so the backend closes the stream with
      // ``{"error": {"code": ..., "message": ...}}`` + ``[DONE]``. Handled
      // before the token/done branches: an error frame never carries either.
      if (parsed && typeof parsed === 'object' && parsed.error) {
        if (streamErrors.length === 0) {
          streamErrors.push(
            insufficientCreditsFromStreamFrame(parsed)
            ?? priceChangedFromStreamFrame(parsed)
            ?? new ChatStreamProtocolError(
              'stream_error_frame',
              streamErrorFrameMessage(parsed.error),
            ),
          )
        }
        return
      }
      // Backend emits ``{"conversation_id": ...}`` as the first SSE event —
      // surface it to the caller right away so a later network failure
      // still leaves the frontend knowing which conversation to reload.
      if (parsed.conversation_id && onConversationId) {
        onConversationId(parsed.conversation_id)
      }
      if (parsed.token) {
        onToken(parsed.token)
      }
      if (parsed.done && parsed.response) {
        finalResponse = parsed.response
      }
    } catch {
      // skip malformed lines
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split('\n')
    buffer = lines.pop()!

    for (const line of lines) processLine(line)
  }

  // Flush any trailing bytes (e.g. last event without final "\n").
  buffer += decoder.decode()
  if (buffer.length > 0) {
    for (const line of buffer.split('\n')) processLine(line)
  }

  // A deliberate refusal outranks the generic "no final event" diagnosis —
  // the stream did end without a response, but we know exactly why.
  if (streamErrors.length > 0) {
    throw streamErrors[0]
  }

  if (!finalResponse) {
    throw new ChatStreamProtocolError(
      'stream_ended_without_final_response',
      'Stream ended without final response',
    )
  }
  return finalResponse
}

function streamErrorFrameMessage(error: unknown): string {
  if (typeof error === 'object' && error !== null) {
    const message = (error as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  if (typeof error === 'string' && error) return error
  return 'Stream ended with an error'
}

async function chatErrorFromResponse(
  response: Response,
  prefix: string,
): Promise<Error> {
  const detail = await detailFromResponse(response)
  // Out of credits (402) — the player can act on this, so it gets the same
  // typed treatment as the other entitlement outcomes below.
  const creditsError = insufficientCreditsFromBody({ detail }, response.status)
  if (creditsError) return creditsError
  // A stale fixed-action quote (409) — nothing ran and nothing was charged;
  // the panel asks the player to resend at the refreshed price.
  const priceError = priceChangedFromBody({ detail }, response.status)
  if (priceError) return priceError
  if (response.status === 429 && isSessionMessageLimit(detail)) {
    return new ChatRuntimeLimitError({
      code: 'max_messages_per_session',
      message: 'Public demo message cap reached.',
      statusCode: response.status,
    })
  }
  // Cost-cap / quota throttling (backend: 429 with a structured
  // ``{code: 'cost_cap_exceeded' | 'quota_exceeded', message}`` detail,
  // shaped like the 402 insufficient-credits body above). Distinct from
  // ``max_messages_per_session`` above — that one is the hosted demo's own
  // session ceiling, this is the runtime hitting an operator-side cost cap
  // or plan quota. Surfaced as a typed error so the panel shows localized,
  // non-blaming copy instead of an opaque "Chat request failed: 429".
  if (
    response.status === 429
    && (detailCodeIs(detail, 'cost_cap_exceeded') || detailCodeIs(detail, 'quota_exceeded'))
  ) {
    const code = (detail as { code: 'cost_cap_exceeded' | 'quota_exceeded' }).code
    return new ChatRuntimeLimitError({
      code,
      message: detailMessage(detail, 'Runtime limit reached.'),
      statusCode: response.status,
    })
  }
  // A lapsed-subscription hard lock (backend: 403 with a structured
  // ``{code: 'subscription_frozen', message}`` detail). Surface it as a typed
  // error so the chat boundary shows the localized "renew to continue" copy
  // instead of an opaque "Chat request failed: 403".
  if (response.status === 403 && detailCodeIs(detail, 'subscription_frozen')) {
    return new ChatRuntimeLimitError({
      code: 'subscription_frozen',
      message: subscriptionFrozenMessage(detail),
      statusCode: response.status,
    })
  }
  // D7 / EC10 — the licensor's contract for this character's source card
  // ended. Its own code, deliberately not `subscription_frozen`: the
  // player's plan is fine, and the renew-your-plan copy would send them
  // to a payment page that cannot bring the character back.
  if (
    response.status === 403
    && detailCodeIs(detail, 'character_contract_ended')
  ) {
    return new ChatRuntimeLimitError({
      code: 'character_contract_ended',
      message: detailMessage(detail, 'Character licensing agreement ended.'),
      statusCode: response.status,
    })
  }
  return new Error(`${prefix}: ${response.status}`)
}

function detailCodeIs(detail: unknown, code: string): boolean {
  return (
    typeof detail === 'object'
    && detail !== null
    && (detail as { code?: unknown }).code === code
  )
}

function subscriptionFrozenMessage(detail: unknown): string {
  return detailMessage(detail, 'Subscription lapsed.')
}

function detailMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'object' && detail !== null) {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  return fallback
}

async function detailFromResponse(response: Response): Promise<unknown> {
  try {
    const payload = await response.json()
    if (payload && typeof payload === 'object') {
      return (payload as { detail?: unknown }).detail
    }
  } catch {
    return null
  }
  return null
}

function isSessionMessageLimit(detail: unknown): boolean {
  if (typeof detail !== 'string') return false
  return detail.toLowerCase().includes('session message limit')
}
