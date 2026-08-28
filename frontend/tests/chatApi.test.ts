import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import {
  ChatRuntimeLimitError,
  ChatStreamAbortedError,
  ChatStreamProtocolError,
  InsufficientCreditsError,
  isChatStreamAbortedError,
  sendChatMessage,
  sendChatMessageStream,
} from '@/utils/api/chat'
import { ConversationBusyError } from '@/utils/api/conversationBusy'
import { PriceChangedError } from '@/utils/api/priceChanged'

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('chat API runtime limit errors', () => {
  it('maps a non-streaming per-session message cap 429 to a typed error', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(429, {
      detail: 'account runtime profile session message limit reached (80/session)',
    }))

    await expect(sendChatMessage({
      character_id: 'char-1',
      message: 'one more',
    })).rejects.toMatchObject({
      code: 'max_messages_per_session',
      statusCode: 429,
    })
  })

  it('maps a streaming per-session message cap 429 to a typed error', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(429, {
      detail: 'account runtime profile session message limit reached (80/session)',
    }))

    await expect(sendChatMessageStream({
      character_id: 'char-1',
      message: 'one more',
    }, () => {})).rejects.toBeInstanceOf(ChatRuntimeLimitError)
  })

  it('raises a typed, coded error when the SSE stream closes without a final response', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(streamResponseWithoutFinalEvent())

    await expect(sendChatMessageStream({
      character_id: 'char-1',
      message: 'hello',
    }, () => {})).rejects.toMatchObject({
      code: 'stream_ended_without_final_response',
    })
  })

  it('raised stream-closed error is an instance of ChatStreamProtocolError', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(streamResponseWithoutFinalEvent())

    await expect(sendChatMessageStream({
      character_id: 'char-1',
      message: 'hello',
    }, () => {})).rejects.toBeInstanceOf(ChatStreamProtocolError)
  })

  it('maps a 429 cost_cap_exceeded to a typed error carrying the backend message', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(429, {
      detail: { code: 'cost_cap_exceeded', message: 'monthly cost cap reached' },
    }))

    await expect(sendChatMessage({
      character_id: 'char-1',
      message: 'one more',
    })).rejects.toMatchObject({
      code: 'cost_cap_exceeded',
      message: 'monthly cost cap reached',
      statusCode: 429,
    })
  })

  it('maps a 429 quota_exceeded to a typed error carrying the backend message', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(429, {
      detail: { code: 'quota_exceeded', message: 'monthly quota reached' },
    }))

    await expect(sendChatMessageStream({
      character_id: 'char-1',
      message: 'one more',
    }, () => {})).rejects.toMatchObject({
      code: 'quota_exceeded',
      message: 'monthly quota reached',
      statusCode: 429,
    })
  })

  it('cost_cap_exceeded and quota_exceeded errors are instances of ChatRuntimeLimitError', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(429, {
      detail: { code: 'cost_cap_exceeded', message: 'monthly cost cap reached' },
    }))

    await expect(sendChatMessage({
      character_id: 'char-1',
      message: 'one more',
    })).rejects.toBeInstanceOf(ChatRuntimeLimitError)
  })
})

describe('chat API out-of-credits handling', () => {
  it('maps a non-streaming 402 to InsufficientCreditsError', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(402, {
      detail: { code: 'insufficient_credits', message: 'out of credits' },
    }))

    await expect(sendChatMessage({
      character_id: 'char-1',
      message: 'hello',
    })).rejects.toBeInstanceOf(InsufficientCreditsError)
  })

  it('maps a pre-stream 402 to InsufficientCreditsError', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(402, {
      detail: { code: 'insufficient_credits', message: 'out of credits' },
    }))

    await expect(sendChatMessageStream({
      character_id: 'char-1',
      message: 'hello',
    }, () => {})).rejects.toBeInstanceOf(InsufficientCreditsError)
  })

  it('parses the terminal SSE error frame emitted mid-generation', async () => {
    // Once the 200 + SSE headers are on the wire the refusal cannot be a
    // status code, so the backend closes with an `error` frame + [DONE].
    // Before this was parsed the frame was silently ignored and the player
    // saw the generic "stream ended without final response".
    mockedAuthedFetch.mockResolvedValueOnce(streamResponse([
      'data: {"conversation_id":"conv-1"}\n\n',
      'data: {"token":"hi"}\n\n',
      'data: {"error":{"code":"insufficient_credits","message":"out of credits"}}\n\n',
      'data: [DONE]\n\n',
    ]))

    const tokens: string[] = []
    const rejected = sendChatMessageStream({
      character_id: 'char-1',
      message: 'hello',
    }, (token) => tokens.push(token))

    await expect(rejected).rejects.toBeInstanceOf(InsufficientCreditsError)
    await expect(rejected).rejects.toMatchObject({
      code: 'insufficient_credits',
    })
    // Tokens streamed before the refusal are still delivered.
    expect(tokens).toEqual(['hi'])
  })

  it('raises the refusal once even though [DONE] follows it', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(streamResponse([
      'data: {"error":{"code":"insufficient_credits","message":"first"}}\n\n',
      'data: [DONE]\n\n',
      'data: {"error":{"code":"insufficient_credits","message":"second"}}\n\n',
    ]))

    await expect(sendChatMessageStream({
      character_id: 'char-1',
      message: 'hello',
    }, () => {})).rejects.toMatchObject({ message: 'first' })
  })

  it('surfaces a non-credit error frame instead of the generic stream-ended error', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(streamResponse([
      'data: {"error":{"code":"some_other_failure","message":"upstream exploded"}}\n\n',
      'data: [DONE]\n\n',
    ]))

    await expect(sendChatMessageStream({
      character_id: 'char-1',
      message: 'hello',
    }, () => {})).rejects.toMatchObject({
      code: 'stream_error_frame',
      message: 'upstream exploded',
    })
  })
})

describe('chat API tool activity frames', () => {
  it('routes tool_activity frames to the callback, never into tokens', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(streamResponse([
      'data: {"conversation_id":"conv-1"}\n\n',
      'data: {"tool_activity":{"tool":"generate_image","status":"started"}}\n\n',
      'data: {"tool_activity":{"tool":"generate_image","status":"finished"}}\n\n',
      'data: {"token":"畫好了"}\n\n',
      'data: {"done":true,"response":{"conversation_id":"conv-1"}}\n\n',
      'data: [DONE]\n\n',
    ]))

    const tokens: string[] = []
    const activities: Array<{ tool: string; status: string }> = []
    await sendChatMessageStream(
      { character_id: 'char-1', message: 'pic please' },
      (token) => tokens.push(token),
      undefined,
      (activity) => activities.push(activity),
    )

    expect(activities).toEqual([
      { tool: 'generate_image', status: 'started' },
      { tool: 'generate_image', status: 'finished' },
    ])
    expect(tokens).toEqual(['畫好了'])
  })

  it('ignores malformed tool_activity frames without breaking the stream', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(streamResponse([
      'data: {"tool_activity":"oops"}\n\n',
      'data: {"tool_activity":{"tool":123,"status":"started"}}\n\n',
      'data: {"tool_activity":{"tool":"web_search"}}\n\n',
      'data: {"done":true,"response":{"conversation_id":"conv-1"}}\n\n',
      'data: [DONE]\n\n',
    ]))

    const activities: unknown[] = []
    await sendChatMessageStream(
      { character_id: 'char-1', message: 'hello' },
      () => {},
      undefined,
      (activity) => activities.push(activity),
    )

    expect(activities).toEqual([])
  })

  it('callers without the callback are untouched by activity frames', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(streamResponse([
      'data: {"tool_activity":{"tool":"web_search","status":"started"}}\n\n',
      'data: {"token":"hi"}\n\n',
      'data: {"done":true,"response":{"conversation_id":"conv-1"}}\n\n',
      'data: [DONE]\n\n',
    ]))

    const tokens: string[] = []
    await sendChatMessageStream(
      { character_id: 'char-1', message: 'hello' },
      (token) => tokens.push(token),
    )

    expect(tokens).toEqual(['hi'])
  })
})

// ----------------------------------------------------------------------
// Walking away from a turn (abort)
// ----------------------------------------------------------------------

/**
 * Abandoning a stream is an *expected* path, not a failure: the reader
 * switched character or left the page, and the server finishes the turn on
 * its own and stores it as an ordinary message. Two properties have to hold
 * or the panel cannot treat it that way — the rejection must be
 * recognisable, and not one further token may be delivered after the abort
 * (a token that arrives late is the previous character's reply animating
 * into the new one's thread).
 */
describe('chat stream abort', () => {
  it('stops delivering tokens and rejects with a typed abort error', async () => {
    const stream = controllableStreamResponse()
    mockedAuthedFetch.mockResolvedValueOnce(stream.response)

    const abort = new AbortController()
    const tokens: string[] = []
    const streaming = sendChatMessageStream(
      { character_id: 'char-1', message: 'hello' },
      (token) => tokens.push(token),
      undefined,
      undefined,
      { signal: abort.signal },
    )
    // Prevent an unhandled rejection while we drive the stream below.
    const settled = streaming.catch((error: unknown) => error)

    stream.push('data: {"token":"hi"}\n\n')
    await vi.waitFor(() => expect(tokens).toEqual(['hi']))

    abort.abort()
    // The transfer itself is stopped, not merely ignored — the point of
    // aborting is that the bytes stop arriving at all.
    await vi.waitFor(() => expect(stream.cancelled()).toBe(true))
    // ...and anything the server had already put on the wire is dropped
    // rather than animated into whatever thread is on screen now.
    stream.push('data: {"token":"ding"}\n\n')
    stream.push('data: {"done":true,"response":{"conversation_id":"conv-1"}}\n\n')

    const error = await settled
    expect(error).toBeInstanceOf(ChatStreamAbortedError)
    expect(isChatStreamAbortedError(error)).toBe(true)
    expect(tokens).toEqual(['hi'])
  })

  it('never spends a request on a turn that was abandoned before it started', async () => {
    const abort = new AbortController()
    abort.abort()

    await expect(sendChatMessageStream(
      { character_id: 'char-1', message: 'hello' },
      () => {},
      undefined,
      undefined,
      { signal: abort.signal },
    )).rejects.toBeInstanceOf(ChatStreamAbortedError)
    expect(mockedAuthedFetch).not.toHaveBeenCalled()
  })

  it('reads the platform’s own AbortError as the same abandonment', async () => {
    // `fetch` rejects with `DOMException{name: "AbortError"}` when the signal
    // fires during the request phase — before there is any body to cancel.
    const abort = new AbortController()
    mockedAuthedFetch.mockImplementationOnce(async () => {
      abort.abort()
      throw new DOMException('The operation was aborted.', 'AbortError')
    })

    await expect(sendChatMessageStream(
      { character_id: 'char-1', message: 'hello' },
      () => {},
      undefined,
      undefined,
      { signal: abort.signal },
    )).rejects.toBeInstanceOf(ChatStreamAbortedError)
  })

  it('leaves an ordinary transport failure alone', async () => {
    // Only aborts get the quiet treatment; a dropped connection is still a
    // failure the player has to be told about.
    mockedAuthedFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))

    const failure = sendChatMessageStream(
      { character_id: 'char-1', message: 'hello' },
      () => {},
      undefined,
      undefined,
      { signal: new AbortController().signal },
    )
    await expect(failure).rejects.toBeInstanceOf(TypeError)
    await expect(failure).rejects.not.toBeInstanceOf(ChatStreamAbortedError)
  })

  it('is unchanged for callers that pass no signal', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(streamResponse([
      'data: {"token":"hi"}\n\n',
      'data: {"done":true,"response":{"conversation_id":"conv-1"}}\n\n',
      'data: [DONE]\n\n',
    ]))

    const tokens: string[] = []
    const reply = await sendChatMessageStream(
      { character_id: 'char-1', message: 'hello' },
      (token) => tokens.push(token),
    )

    expect(tokens).toEqual(['hi'])
    expect(reply).toMatchObject({ conversation_id: 'conv-1' })
  })
})

// ----------------------------------------------------------------------
// "The character is still answering the previous message" (409)
// ----------------------------------------------------------------------

describe('chat API conversation-busy handling', () => {
  it('maps a 409 conversation_busy to a typed error carrying the conversation', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(409, {
      detail: {
        code: 'conversation_busy',
        message: 'a turn is already running',
        conversation_id: 'conv-7',
      },
    }))

    await expect(sendChatMessageStream({
      character_id: 'char-1',
      message: 'hello again',
    }, () => {})).rejects.toMatchObject({
      code: 'conversation_busy',
      statusCode: 409,
      conversationId: 'conv-7',
    })
  })

  it('is an instance of ConversationBusyError on the non-streaming send too', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(409, {
      detail: { code: 'conversation_busy', message: 'a turn is already running' },
    }))

    await expect(sendChatMessage({
      character_id: 'char-1',
      message: 'hello again',
    })).rejects.toBeInstanceOf(ConversationBusyError)
  })

  it('does not shadow the other 409 — a moved price still reads as one', async () => {
    // Both refusals share the status and are told apart by `code` only; a
    // busy branch that checked the status alone would swallow this one.
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(409, {
      detail: { code: 'price_changed', message: 'price moved', current_price_cr: 9 },
    }))

    await expect(sendChatMessageStream({
      character_id: 'char-1',
      message: 'hello',
    }, () => {})).rejects.toBeInstanceOf(PriceChangedError)
  })

  it('leaves an unrecognised 409 as the generic failure', async () => {
    mockedAuthedFetch.mockResolvedValueOnce(jsonResponse(409, {
      detail: { code: 'character_restoring', message: 'restore in progress' },
    }))

    const failure = sendChatMessageStream({
      character_id: 'char-1',
      message: 'hello',
    }, () => {})
    await expect(failure).rejects.not.toBeInstanceOf(ConversationBusyError)
    await expect(failure).rejects.toThrow('409')
  })
})

/**
 * A stream the test writes into by hand, so an abort can be timed to land
 * between two frames — which is the whole point of the abort tests.
 *
 * `push` tolerates a closed controller: cancelling the reader closes the
 * stream from the other end, and "the server kept sending" is exactly the
 * case being exercised after that happens.
 */
function controllableStreamResponse(): {
  response: Response
  push: (frame: string) => void
  cancelled: () => boolean
} {
  const encoder = new TextEncoder()
  let captured!: ReadableStreamDefaultController<Uint8Array>
  let wasCancelled = false
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      captured = controller
    },
    cancel() {
      wasCancelled = true
    },
  })
  return {
    response: { ok: true, status: 200, body } as unknown as Response,
    push: (frame: string) => {
      try {
        captured.enqueue(encoder.encode(frame))
      } catch { /* the reader closed the stream — that is the point */ }
    },
    cancelled: () => wasCancelled,
  }
}

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return {
    ok: true,
    status: 200,
    body,
  } as unknown as Response
}

function streamResponseWithoutFinalEvent(): Response {
  // Emits one token event then closes — never sends `"done": true`, so
  // the reader loop exits with `finalResponse` still null.
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode('data: {"token":"hi"}\n\n'))
      controller.close()
    },
  })
  return {
    ok: true,
    status: 200,
    body,
  } as unknown as Response
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response
}
