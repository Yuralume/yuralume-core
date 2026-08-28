/**
 * The typed refusal for "this conversation already has a turn in flight".
 *
 * The backend (`api/routes/chat.py::_conversation_busy_http_error`) answers
 * HTTP `409 {"detail": {"code": "conversation_busy", "message": …,
 * "conversation_id": …}}` on both the sync and the streaming send. Nothing
 * ran and nothing was charged: the turn lease refused before the model was
 * asked, so the only right rendering is "wait for the reply, then send
 * again" — never a generic failure.
 *
 * This became a *routine* answer the moment the client learned to abandon a
 * stream (character switch, leaving the page): the server finishes that turn
 * on its own, so a player who switches away and straight back can press send
 * while the previous turn is still landing. Before this type existed that
 * press showed the bare "Chat request failed: 409".
 *
 * Modelled on `priceChanged.ts` — same shape, same reason: several surfaces
 * (chat, story scene, messaging) meet the same refusal and must not each
 * invent their own reading of it.
 */

export const CONVERSATION_BUSY_CODE = 'conversation_busy'
export const CONVERSATION_BUSY_STATUS = 409

/** Never rendered — the panels show localized copy keyed off the type. */
const FALLBACK_MESSAGE = 'a turn is already in flight on this conversation'

export class ConversationBusyError extends Error {
  code: string
  statusCode: number
  /** The conversation the server is still working on, when it named one. */
  conversationId: string | null

  constructor(
    input: { message?: string; conversationId?: string | null } = {},
  ) {
    super(input.message || FALLBACK_MESSAGE)
    this.name = 'ConversationBusyError'
    this.code = CONVERSATION_BUSY_CODE
    this.statusCode = CONVERSATION_BUSY_STATUS
    this.conversationId
      = typeof input.conversationId === 'string' ? input.conversationId : null
  }
}

export function isConversationBusyError(
  error: unknown,
): error is ConversationBusyError {
  return error instanceof ConversationBusyError
}

/**
 * Typed error for a FastAPI 409 body (`{detail: {code, …}}`), else `null`.
 * The status is checked so any other 4xx carrying a stray `code` field can
 * never be mistaken for this conflict.
 */
export function conversationBusyFromBody(
  body: unknown,
  status: number,
): ConversationBusyError | null {
  if (status !== CONVERSATION_BUSY_STATUS) return null
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail !== 'object' || detail === null) return null
  if ((detail as { code?: unknown }).code !== CONVERSATION_BUSY_CODE) return null
  const message = (detail as { message?: unknown }).message
  const conversationId = (detail as { conversation_id?: unknown }).conversation_id
  return new ConversationBusyError({
    message: typeof message === 'string' && message ? message : undefined,
    conversationId:
      typeof conversationId === 'string' ? conversationId : null,
  })
}
