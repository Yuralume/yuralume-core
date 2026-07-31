/**
 * R9 — the charge is bound to the price the *player* had on screen.
 *
 * The server already refuses a charge whose quote no longer matches, but until
 * now "the quote" was read from the server's own process-local cache. With
 * several hosted replicas, or right after a back-office edit, that is not
 * necessarily the number this screen showed. These tests pin the client half:
 * every chat send carries what it was quoting, and carries nothing when it has
 * nothing unambiguous to quote.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/utils/authedFetch', () => ({ authedFetch: vi.fn() }))
vi.mock('@/utils/api/cloudPricing', () => ({ fetchCloudPricing: vi.fn() }))

const { authedFetch } = await import('@/utils/authedFetch')
const { fetchCloudPricing } = await import('@/utils/api/cloudPricing')
const { useActionPricing } = await import('@/composables/useActionPricing')
const { sendChatMessage, sendChatMessageStream }
  = await import('@/utils/api/chat')

const mockedFetch = vi.mocked(authedFetch)
const mockedPricing = vi.mocked(fetchCloudPricing)

const REQUEST = { character_id: 'char-1', message: 'hello' }

beforeEach(() => {
  vi.clearAllMocks()
  useActionPricing().reset()
})

async function seedPrices(chatCr: number, imageCr: number): Promise<void> {
  mockedPricing.mockResolvedValueOnce({
    kind: 'ok',
    snapshot: {
      stale: false,
      tiers: [{
        tier_name: 'standard',
        billing_shape: 'action_fixed',
        actions: [
          { action_key: 'chat', unit: 'per_turn', price_cr: chatCr, overage: false },
          {
            action_key: 'image_chat_tool',
            unit: 'per_image',
            price_cr: imageCr,
            overage: false,
          },
        ],
      }],
    },
  })
  await useActionPricing().ensureLoaded()
}

function sentBody(): Record<string, unknown> {
  const init = mockedFetch.mock.calls[0]?.[1] as RequestInit
  return JSON.parse(String(init.body))
}

function okJson(): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({ conversation_id: 'conv-1' }),
    text: async () => '{}',
  } as Response
}

function okStream(): Response {
  const encoder = new TextEncoder()
  return {
    ok: true,
    status: 200,
    body: new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(
          'data: {"done":true,"response":{"conversation_id":"conv-1"}}\n\n',
        ))
        controller.enqueue(encoder.encode('data: [DONE]\n\n'))
        controller.close()
      },
    }),
  } as unknown as Response
}

describe('chat sends carry the price this screen quoted', () => {
  it('attaches both quotes to a non-streaming send', async () => {
    await seedPrices(3, 12)
    mockedFetch.mockResolvedValueOnce(okJson())

    await sendChatMessage(REQUEST)

    expect(sentBody()).toMatchObject({
      quoted_price_cr: 3,
      quoted_image_price_cr: 12,
    })
  })

  it('attaches both quotes to a streaming send', async () => {
    await seedPrices(3, 12)
    mockedFetch.mockResolvedValueOnce(okStream())

    await sendChatMessageStream(REQUEST, () => {})

    expect(sentBody()).toMatchObject({
      quoted_price_cr: 3,
      quoted_image_price_cr: 12,
    })
  })

  it('sends no quote at all when nothing is quotable', async () => {
    // Self-host, a token-billed deployment, a price list that never loaded —
    // the server then falls back to its own cache, exactly as before.
    mockedFetch.mockResolvedValueOnce(okJson())

    await sendChatMessage(REQUEST)

    const body = sentBody()
    expect('quoted_price_cr' in body).toBe(false)
    expect('quoted_image_price_cr' in body).toBe(false)
  })

  it('leaves the caller-supplied fields of the request untouched', async () => {
    await seedPrices(3, 12)
    mockedFetch.mockResolvedValueOnce(okJson())

    await sendChatMessage({ ...REQUEST, attachment_urls: ['/uploads/a.png'] })

    expect(sentBody()).toMatchObject({
      character_id: 'char-1',
      message: 'hello',
      attachment_urls: ['/uploads/a.png'],
    })
  })
})
