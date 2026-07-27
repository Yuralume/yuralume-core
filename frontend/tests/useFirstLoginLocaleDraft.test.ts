import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { GeoSearchResult } from '@/types/playerLocale'

// Auth is a module-scope singleton; a hoisted handle lets each test choose
// the identity and the GeoIP hint the gate is seeded from.
const authState = vi.hoisted(() => ({
  currentUser: null as Record<string, unknown> | null,
  locationHint: null as Record<string, unknown> | null,
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    currentUser: { value: authState.currentUser },
    locationHint: { value: authState.locationHint },
  }),
}))

vi.mock('@/composables/useTimezone', () => ({
  useTimezone: () => ({ browserTimezone: () => 'Etc/UTC' }),
}))

const { useFirstLoginLocaleDraft }
  = await import('@/composables/useFirstLoginLocaleDraft')

const TAIPEI: GeoSearchResult = {
  label: '臺北市',
  latitude: 25.033,
  longitude: 121.5654,
  country_code: 'TW',
  timezone_id: 'Asia/Taipei',
}

const TOKYO: GeoSearchResult = {
  label: '東京都',
  latitude: 35.6762,
  longitude: 139.6503,
  country_code: 'JP',
  timezone_id: 'Asia/Tokyo',
}

beforeEach(() => {
  authState.currentUser = {
    id: 'u1',
    primary_language: 'zh-TW',
    timezone_id: 'Asia/Taipei',
    country_code: null,
    latitude: null,
    longitude: null,
    location_label: null,
  }
  authState.locationHint = null
})

describe('first-login place confirmation', () => {
  it('a picked city travels with its coordinates and country', () => {
    const draft = useFirstLoginLocaleDraft()
    draft.seed()

    draft.onPlaceSelected(TAIPEI)

    expect(draft.confirmPayload()).toMatchObject({
      location_label: '臺北市',
      country_code: 'TW',
      latitude: TAIPEI.latitude,
      longitude: TAIPEI.longitude,
    })
  })

  it('typing over a picked city never saves the old coordinates', () => {
    const draft = useFirstLoginLocaleDraft()
    draft.seed()
    draft.onPlaceSelected(TOKYO)

    // The player keeps typing — the field reports the text stopped standing
    // for Tokyo, and then the text becomes something else entirely.
    draft.onPlaceCleared()
    draft.placeQuery.value = '我家巷口'

    const payload = draft.confirmPayload()
    expect(payload.location_label).toBe('我家巷口')
    expect(payload.latitude).toBeNull()
    expect(payload.longitude).toBeNull()
    expect(payload.country_code).toBeNull()
  })

  it('invalidating a pick takes back the timezone that pick applied', () => {
    const draft = useFirstLoginLocaleDraft()
    draft.seed()
    expect(draft.timezoneId.value).toBe('Asia/Taipei')

    draft.onPlaceSelected(TOKYO)
    expect(draft.timezoneId.value).toBe('Asia/Tokyo')

    draft.onPlaceCleared()
    expect(draft.timezoneId.value).toBe('Asia/Taipei')
  })

  it('but never takes back a timezone the player chose themselves', () => {
    const draft = useFirstLoginLocaleDraft()
    draft.seed()
    draft.onPlaceSelected(TOKYO)

    draft.timezoneId.value = 'Europe/Berlin'
    draft.onPlaceCleared()

    expect(draft.timezoneId.value).toBe('Europe/Berlin')
  })

  it('hand-typed text is saved as a name, with the coordinates cleared', () => {
    // A GeoIP guess is on file, so the profile already holds coordinates —
    // exactly the ones that must not survive a hand-typed label.
    authState.locationHint = {
      country_code: 'JP',
      label: '東京都',
      latitude: 35.6762,
      longitude: 139.6503,
      timezone_id: 'Asia/Tokyo',
      detected_at: null,
    }
    const draft = useFirstLoginLocaleDraft()
    draft.seed()

    draft.onPlaceCleared()
    draft.placeQuery.value = '外婆家'

    const payload = draft.confirmPayload()
    expect(payload.location_label).toBe('外婆家')
    expect(payload.latitude).toBeNull()
    expect(payload.longitude).toBeNull()
    expect(payload.country_code).toBeNull()
  })

  it('accepting the GeoIP guess untouched keeps the seed it was shown', () => {
    authState.locationHint = {
      country_code: 'JP',
      label: '東京都',
      latitude: 35.6762,
      longitude: 139.6503,
      timezone_id: 'Asia/Tokyo',
      detected_at: null,
    }
    const draft = useFirstLoginLocaleDraft()
    draft.seed()

    expect(draft.confirmPayload()).toMatchObject({
      location_label: '東京都',
      country_code: 'JP',
      latitude: 35.6762,
      longitude: 139.6503,
      timezone_id: 'Asia/Tokyo',
      dismiss_location_hint: true,
    })
  })

  it('leaves an untouched place out of the body entirely', () => {
    // Tri-state keys: omitted leaves the stored value alone, `null` erases
    // it. With no hint and no edit there is nothing to say about the place.
    const draft = useFirstLoginLocaleDraft()
    draft.seed()

    const payload = draft.confirmPayload()
    expect(payload).not.toHaveProperty('location_label')
    expect(payload).not.toHaveProperty('latitude')
    expect(payload).not.toHaveProperty('country_code')
    expect(payload.dismiss_location_hint).toBe(true)
  })

  it('emptying the field clears the stored place instead of omitting it', () => {
    authState.currentUser = {
      id: 'u1',
      primary_language: 'zh-TW',
      timezone_id: 'Asia/Taipei',
      country_code: 'TW',
      latitude: null,
      longitude: null,
      location_label: '臺北市',
    }
    const draft = useFirstLoginLocaleDraft()
    draft.seed()

    draft.onPlaceCleared()
    draft.placeQuery.value = ''

    const payload = draft.confirmPayload()
    expect(payload.location_label).toBeNull()
    expect(payload.latitude).toBeNull()
  })
})
