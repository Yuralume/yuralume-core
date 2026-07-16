import { describe, expect, it } from 'vitest'

import { readErrorResponse } from '@/utils/api/httpError'

function jsonResponse(body: unknown, status = 422, statusText = 'Unprocessable Entity'): Response {
  return {
    status,
    statusText,
    json: async () => body,
  } as unknown as Response
}

function brokenResponse(status = 500, statusText = 'Internal Server Error'): Response {
  return {
    status,
    statusText,
    json: async () => {
      throw new Error('not json')
    },
  } as unknown as Response
}

describe('readErrorResponse', () => {
  it('returns a string detail unchanged', async () => {
    const res = jsonResponse({ detail: 'character not found' }, 404, 'Not Found')
    expect(await readErrorResponse(res)).toBe('character not found')
  })

  it('formats a pydantic 422 array detail into human-readable lines', async () => {
    const res = jsonResponse({
      detail: [
        {
          type: 'greater_than_equal',
          loc: ['body', 'duration_days'],
          msg: 'Input should be greater than or equal to 3',
          input: 2,
          ctx: { ge: 3 },
        },
      ],
    })

    expect(await readErrorResponse(res)).toBe(
      'duration_days: Input should be greater than or equal to 3',
    )
  })

  it('joins multiple array-detail entries with newlines and uses the last loc segment', async () => {
    const res = jsonResponse({
      detail: [
        {
          type: 'greater_than_equal',
          loc: ['body', 'duration_days'],
          msg: 'Input should be greater than or equal to 3',
        },
        {
          type: 'value_error',
          loc: ['body'],
          msg: 'duration_days must be greater than or equal to beat_count',
        },
      ],
    })

    expect(await readErrorResponse(res)).toBe(
      'duration_days: Input should be greater than or equal to 3\n'
      + 'body: duration_days must be greater than or equal to beat_count',
    )
  })

  it('falls back to msg only when loc is missing or empty', async () => {
    const res = jsonResponse({
      detail: [{ msg: 'something went wrong' }],
    })

    expect(await readErrorResponse(res)).toBe('something went wrong')
  })

  it('falls back to status/statusText when the body cannot be parsed as JSON', async () => {
    const res = brokenResponse()
    expect(await readErrorResponse(res)).toBe('500 Internal Server Error')
  })

  it('falls back to status/statusText when detail is neither string nor array', async () => {
    const res = jsonResponse({ detail: { unexpected: true } }, 400, 'Bad Request')
    expect(await readErrorResponse(res)).toBe('400 Bad Request')
  })
})
