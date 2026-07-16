import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authedFetch } from '@/utils/authedFetch'
import {
  ChatRuntimeLimitError,
  ChatStreamProtocolError,
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
