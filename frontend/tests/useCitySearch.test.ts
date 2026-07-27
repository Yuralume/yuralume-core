import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { searchPlaces } from '@/utils/api/playerLocale'
import {
  CITY_SEARCH_DEBOUNCE_MS,
  CITY_SEARCH_MIN_LENGTH,
  useCitySearch,
} from '@/composables/useCitySearch'

vi.mock('@/utils/api/playerLocale', () => ({
  searchPlaces: vi.fn(),
}))

const mockedSearch = vi.mocked(searchPlaces)

function place(label: string) {
  return {
    label,
    latitude: 1,
    longitude: 2,
    country_code: 'TW',
    timezone_id: 'Asia/Taipei',
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useCitySearch debounce', () => {
  it('waits out the debounce window before asking the backend', async () => {
    const city = useCitySearch()
    mockedSearch.mockResolvedValue([place('Taipei')])

    city.search('taipei')
    expect(city.searching.value).toBe(true)

    vi.advanceTimersByTime(CITY_SEARCH_DEBOUNCE_MS - 1)
    expect(mockedSearch).not.toHaveBeenCalled()

    vi.advanceTimersByTime(1)
    await vi.runAllTimersAsync()

    expect(mockedSearch).toHaveBeenCalledExactlyOnceWith('taipei')
    expect(city.results.value.map((r) => r.label)).toEqual(['Taipei'])
    expect(city.searching.value).toBe(false)
    expect(city.searched.value).toBe(true)
  })

  it('debounce is at least the 300ms the plan asks for', () => {
    expect(CITY_SEARCH_DEBOUNCE_MS).toBeGreaterThanOrEqual(300)
  })

  it('coalesces a burst of keystrokes into one request', async () => {
    const city = useCitySearch()
    mockedSearch.mockResolvedValue([place('Tokyo')])

    city.search('t')
    city.search('to')
    city.search('tok')
    city.search('tokyo')
    await vi.runAllTimersAsync()

    expect(mockedSearch).toHaveBeenCalledExactlyOnceWith('tokyo')
  })

  it('never lets a superseded response overwrite a newer one', async () => {
    const city = useCitySearch()
    let resolveSlow: (value: ReturnType<typeof place>[]) => void = () => {}
    mockedSearch.mockImplementationOnce(
      () => new Promise((resolve) => { resolveSlow = resolve }),
    )

    city.search('osak')
    vi.advanceTimersByTime(CITY_SEARCH_DEBOUNCE_MS)

    // The player keeps typing while the first request is still in flight.
    mockedSearch.mockResolvedValueOnce([place('Osaka')])
    city.search('osaka')
    await vi.runAllTimersAsync()

    resolveSlow([place('STALE')])
    await vi.runAllTimersAsync()

    expect(city.results.value.map((r) => r.label)).toEqual(['Osaka'])
  })

  it('does not query for a single character', async () => {
    const city = useCitySearch()

    city.search('t')
    await vi.runAllTimersAsync()

    expect(CITY_SEARCH_MIN_LENGTH).toBeGreaterThan(1)
    expect(mockedSearch).not.toHaveBeenCalled()
    expect(city.searching.value).toBe(false)
  })

  it('reset drops results and cancels a pending request', async () => {
    const city = useCitySearch()
    mockedSearch.mockResolvedValue([place('Kyoto')])

    city.search('kyoto')
    city.reset()
    await vi.runAllTimersAsync()

    expect(mockedSearch).not.toHaveBeenCalled()
    expect(city.results.value).toEqual([])
    expect(city.searched.value).toBe(false)
  })
})
