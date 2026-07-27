import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import {
  ChatRuntimeLimitError,
  ChatStreamProtocolError,
  InsufficientCreditsError,
  sendChatMessage,
  sendChatMessageStream,
} from '@/utils/api/chat'

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('chat API runtime limit errors', () => {
  it('maps non-streaming demo max_messages 429 to a typed error', async () => {
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

  it('maps streaming demo max_messages 429 to a typed error', async () => {
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
