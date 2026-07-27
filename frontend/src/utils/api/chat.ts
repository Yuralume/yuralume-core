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
 * Fetch the most recently active conversation for a character.
 * Returns null when the character has no prior conversations.
 */
export async function getLatestConversation(
  characterId: string,
): Promise<ConversationSnapshot | null> {
  const res = await authedFetch(`/api/v1/characters/${characterId}/conversations/latest`)
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
 * Send a chat message and stream the assistant reply via SSE.
 * Falls back to non-streaming if the streaming endpoint is not available.
 */
export async function sendChatMessage(req: SendChatMessageRequest): Promise<ChatReplyResponse> {
  const res = await authedFetch('/api/v1/chat/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
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
    body: JSON.stringify(req),
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
  if (response.status === 429 && isSessionMessageLimit(detail)) {
    return new ChatRuntimeLimitError({
      code: 'max_messages_per_session',
      message: 'Public demo message cap reached.',
      statusCode: response.status,
    })
  }
  // A lapsed-subscription hard lock (backend: 403 with a structured
  // ``{code: 'subscription_frozen', message}`` detail). Surface it as a typed
  // error so the chat boundary shows the localized "renew to continue" copy
  // instead of an opaque "Chat request failed: 403".
  if (response.status === 403 && subscriptionFrozenCode(detail)) {
    return new ChatRuntimeLimitError({
      code: 'subscription_frozen',
      message: subscriptionFrozenMessage(detail),
      statusCode: response.status,
    })
  }
  return new Error(`${prefix}: ${response.status}`)
}

function subscriptionFrozenCode(detail: unknown): boolean {
  return (
    typeof detail === 'object'
    && detail !== null
    && (detail as { code?: unknown }).code === 'subscription_frozen'
  )
}

function subscriptionFrozenMessage(detail: unknown): string {
  if (typeof detail === 'object' && detail !== null) {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  return 'Subscription lapsed.'
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
