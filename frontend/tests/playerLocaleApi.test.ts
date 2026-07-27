import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'

import {
  LOCALE_CHANGE_LIMIT,
  LOCALE_CHANGE_WINDOW_DAYS,
  LocaleAlreadyConfirmedError,
  LocaleChangeInProgressError,
  LocaleChangeLimitError,
  changeLocale,
  confirmLocale,
  searchPlaces,
} from '@/utils/api/playerLocale'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

function limitResponse(retryAt: string | null) {
  return {
    response: {
      status: 429,
      data: {
        detail: {
          code: 'locale_change_limit',
          limit: 2,
          window_days: 30,
          retry_at: retryAt,
          remaining_changes: 0,
        },
      },
    },
  }
}

function conflictResponse(detail: Record<string, unknown>) {
  return { response: { status: 409, data: { detail } } }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('searchPlaces', () => {
  it('asks the backend proxy rather than the third party directly', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: {
        results: [{
          label: 'Taipei, Taiwan',
          latitude: 25.05,
          longitude: 121.53,
          country_code: 'TW',
          timezone_id: 'Asia/Taipei',
        }],
      },
    })

    const results = await searchPlaces(' taipei ')

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/geo/search',
      { params: { q: 'taipei' } },
    )
    expect(results).toHaveLength(1)
    expect(results[0].timezone_id).toBe('Asia/Taipei')
  })

  it('never turns a mid-typing outage into an error', async () => {
    mockedAxios.get.mockRejectedValueOnce(new Error('upstream down'))

    await expect(searchPlaces('osaka')).resolves.toEqual([])
  })

  it('skips a blank query entirely', async () => {
    await expect(searchPlaces('   ')).resolves.toEqual([])
    expect(mockedAxios.get).not.toHaveBeenCalled()
  })

  it('drops malformed rows instead of rendering a place with no coordinates', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: {
        results: [
          { label: 'Nowhere' },
          { label: '', latitude: 1, longitude: 2 },
          { label: 'Kyoto', latitude: 35, longitude: 135.7 },
        ],
      },
    })

    const results = await searchPlaces('k')

    expect(results.map((place) => place.label)).toEqual(['Kyoto'])
    expect(results[0].country_code).toBeNull()
  })
})

describe('changeLocale', () => {
  it('patches the guarded endpoint and returns the remaining budget', async () => {
    mockedAxios.patch.mockResolvedValueOnce({
      data: {
        user: { id: 'u1', timezone_id: 'Asia/Tokyo' },
        remaining_changes: 1,
        change_limit: 2,
        window_days: 30,
      },
    })

    const state = await changeLocale({ timezone_id: 'Asia/Tokyo' })

    expect(mockedAxios.patch).toHaveBeenCalledWith(
      '/api/v1/auth/me/locale',
      { timezone_id: 'Asia/Tokyo' },
    )
    expect(state.remaining_changes).toBe(1)
  })

  it('turns the structured 429 into a typed error carrying retry_at', async () => {
    mockedAxios.patch.mockRejectedValueOnce(limitResponse('2026-08-20T03:00:00+00:00'))

    const error = await changeLocale({ primary_language: 'ja-JP' }).catch((e) => e)

    expect(error).toBeInstanceOf(LocaleChangeLimitError)
    expect(error.retryAt).toBe('2026-08-20T03:00:00+00:00')
    expect(error.limit).toBe(LOCALE_CHANGE_LIMIT)
    expect(error.windowDays).toBe(LOCALE_CHANGE_WINDOW_DAYS)
  })

  it('tolerates a limit body without a usable retry_at', async () => {
    mockedAxios.patch.mockRejectedValueOnce(limitResponse(null))

    const error = await changeLocale({ primary_language: 'ja-JP' }).catch((e) => e)

    expect(error).toBeInstanceOf(LocaleChangeLimitError)
    expect(error.retryAt).toBeNull()
  })

  it('lets unrelated failures bubble untouched', async () => {
    // A proxy-issued 429 with no structured body must not be mis-read as
    // "you changed this too often" — that would be a lie about the cause.
    mockedAxios.patch.mockRejectedValueOnce({
      response: { status: 429, data: 'Too Many Requests' },
    })

    const error = await changeLocale({ timezone_id: 'UTC' }).catch((e) => e)

    expect(error).not.toBeInstanceOf(LocaleChangeLimitError)
  })

  it('reads the 409 claim conflict as "retry", not as a spent budget', async () => {
    mockedAxios.patch.mockRejectedValueOnce(conflictResponse({
      code: 'locale_change_in_progress',
      message: 'another locale change is in progress; retry shortly',
    }))

    const error = await changeLocale({ timezone_id: 'UTC' }).catch((e) => e)

    expect(error).toBeInstanceOf(LocaleChangeInProgressError)
    // Nothing was spent, so it must never be confused with the guardrail.
    expect(error).not.toBeInstanceOf(LocaleChangeLimitError)
  })

  it('leaves a 409 that is not this contract alone', async () => {
    mockedAxios.patch.mockRejectedValueOnce(conflictResponse({
      code: 'something_else',
    }))

    const error = await changeLocale({ timezone_id: 'UTC' }).catch((e) => e)

    expect(error).not.toBeInstanceOf(LocaleChangeInProgressError)
    expect(error).not.toBeInstanceOf(LocaleAlreadyConfirmedError)
  })
})

describe('confirmLocale', () => {
  it('posts the confirmation body verbatim', async () => {
    mockedAxios.post.mockResolvedValueOnce({
      data: {
        user: { id: 'u1' },
        remaining_changes: 2,
        change_limit: 2,
        window_days: 30,
      },
    })

    const state = await confirmLocale({
      timezone_id: 'Asia/Taipei',
      dismiss_location_hint: true,
    })

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/auth/me/locale/confirm',
      { timezone_id: 'Asia/Taipei', dismiss_location_hint: true },
    )
    // Confirming is free: the full budget is still there afterwards.
    expect(state.remaining_changes).toBe(2)
  })

  it('types the 409 that says the free ride is over', async () => {
    mockedAxios.post.mockRejectedValueOnce(conflictResponse({
      code: 'locale_already_confirmed',
      message: 'locale is already confirmed; use PATCH /auth/me/locale',
      limit: 2,
      window_days: 30,
    }))

    const error = await confirmLocale({ timezone_id: 'UTC' }).catch((e) => e)

    expect(error).toBeInstanceOf(LocaleAlreadyConfirmedError)
    // The budget rides along so the message can name the guardrail it
    // is sending the player to.
    expect(error.limit).toBe(2)
    expect(error.windowDays).toBe(30)
  })

  it('falls back to the documented budget when the 409 omits it', async () => {
    mockedAxios.post.mockRejectedValueOnce(conflictResponse({
      code: 'locale_already_confirmed',
    }))

    const error = await confirmLocale({ timezone_id: 'UTC' }).catch((e) => e)

    expect(error.limit).toBe(LOCALE_CHANGE_LIMIT)
    expect(error.windowDays).toBe(LOCALE_CHANGE_WINDOW_DAYS)
  })

  it('types the transient claim conflict on the free endpoint too', async () => {
    mockedAxios.post.mockRejectedValueOnce(conflictResponse({
      code: 'locale_change_in_progress',
    }))

    const error = await confirmLocale({ dismiss_location_hint: true })
      .catch((e) => e)

    expect(error).toBeInstanceOf(LocaleChangeInProgressError)
  })

  it('lets an unstructured confirm failure bubble untouched', async () => {
    mockedAxios.post.mockRejectedValueOnce(new Error('network down'))

    const error = await confirmLocale({ dismiss_location_hint: true })
      .catch((e) => e)

    expect(error).toBeInstanceOf(Error)
    expect(error).not.toBeInstanceOf(LocaleChangeInProgressError)
    expect(error.message).toBe('network down')
  })
})
