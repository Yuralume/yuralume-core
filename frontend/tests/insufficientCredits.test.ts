import { describe, expect, it } from 'vitest'

import {
  InsufficientCreditsError,
  apiErrorFromResponse,
  insufficientCreditsFromAxios,
  insufficientCreditsFromBody,
  insufficientCreditsFromStreamFrame,
  isInsufficientCreditsError,
} from '@/utils/api/insufficientCredits'

describe('402 body discrimination', () => {
  it('recognises the structured out-of-credits detail', () => {
    const error = insufficientCreditsFromBody(
      { detail: { code: 'insufficient_credits', message: 'no credits left' } },
      402,
    )

    expect(error).toBeInstanceOf(InsufficientCreditsError)
    expect(error?.code).toBe('insufficient_credits')
    expect(error?.statusCode).toBe(402)
    expect(error?.message).toBe('no credits left')
  })

  it('keeps the typed error out of non-402 responses carrying the same code', () => {
    // Defensive: only the payment-required status may be read as a billing
    // refusal, so an unrelated endpoint echoing the string can never route a
    // player to the top-up page.
    expect(
      insufficientCreditsFromBody(
        { detail: { code: 'insufficient_credits' } },
        400,
      ),
    ).toBeNull()
  })

  it('ignores other 402-shaped details and plain-string details', () => {
    expect(
      insufficientCreditsFromBody({ detail: { code: 'entitlement_denied' } }, 402),
    ).toBeNull()
    expect(insufficientCreditsFromBody({ detail: 'nope' }, 402)).toBeNull()
    expect(insufficientCreditsFromBody(null, 402)).toBeNull()
  })
})

describe('SSE terminal error frame', () => {
  it('maps the streaming refusal frame to the same typed error', () => {
    const error = insufficientCreditsFromStreamFrame({
      error: { code: 'insufficient_credits', message: 'out of credits' },
    })

    expect(isInsufficientCreditsError(error)).toBe(true)
    expect(error?.message).toBe('out of credits')
  })

  it('leaves ordinary token / done frames alone', () => {
    expect(insufficientCreditsFromStreamFrame({ token: 'hi' })).toBeNull()
    expect(insufficientCreditsFromStreamFrame({ done: true })).toBeNull()
    expect(insufficientCreditsFromStreamFrame(null)).toBeNull()
  })
})

describe('axios rejections', () => {
  it('maps an axios 402 to the typed error', () => {
    const error = insufficientCreditsFromAxios({
      response: {
        status: 402,
        data: { detail: { code: 'insufficient_credits', message: 'top up' } },
      },
    })

    expect(isInsufficientCreditsError(error)).toBe(true)
  })

  it('returns null for network errors and other statuses', () => {
    expect(insufficientCreditsFromAxios(new Error('network'))).toBeNull()
    expect(
      insufficientCreditsFromAxios({ response: { status: 503, data: {} } }),
    ).toBeNull()
  })
})

describe('apiErrorFromResponse', () => {
  it('returns the typed error for a 402 body', async () => {
    const error = await apiErrorFromResponse(
      jsonResponse(402, {
        detail: { code: 'insufficient_credits', message: 'out of credits' },
      }),
    )

    expect(isInsufficientCreditsError(error)).toBe(true)
  })

  it('falls back to the server message for every other failure', async () => {
    const error = await apiErrorFromResponse(
      jsonResponse(409, { detail: 'story is already finished' }),
    )

    expect(isInsufficientCreditsError(error)).toBe(false)
    expect(error.message).toBe('story is already finished')
  })

  it('falls back to status text when the body is not JSON', async () => {
    const error = await apiErrorFromResponse({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: async () => {
        throw new Error('not json')
      },
    } as unknown as Response)

    expect(error.message).toBe('500 Internal Server Error')
  })
})

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    json: async () => body,
  } as Response
}
