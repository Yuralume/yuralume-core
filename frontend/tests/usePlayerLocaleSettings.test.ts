import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { OperatorProfile } from '@/types/operator'

// Auth is a module-scope singleton; a hoisted handle lets each test choose
// the deployment mode / identity before the composable reads it.
const authState = vi.hoisted(() => ({
  cloudMode: true,
  currentUser: null as Record<string, unknown> | null,
  locationHint: null as Record<string, unknown> | null,
  refreshMe: vi.fn(async () => {}),
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    cloudMode: { value: authState.cloudMode },
    currentUser: { value: authState.currentUser },
    locationHint: { value: authState.locationHint },
    refreshMe: authState.refreshMe,
  }),
}))

vi.mock('@/utils/api/operatorProfile', () => ({
  updateOperatorProfile: vi.fn(),
}))

vi.mock('@/utils/api/playerLocale', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/utils/api/playerLocale')>()
  return {
    ...actual,
    changeLocale: vi.fn(),
    confirmLocale: vi.fn(),
    searchPlaces: vi.fn(),
  }
})

const { updateOperatorProfile } = await import('@/utils/api/operatorProfile')
const {
  LocaleChangeInProgressError,
  LocaleChangeLimitError,
  changeLocale,
  confirmLocale,
} = await import('@/utils/api/playerLocale')
const { usePlayerLocaleSettings } = await import('@/composables/usePlayerLocaleSettings')

const mockedUpdateProfile = vi.mocked(updateOperatorProfile)
const mockedChangeLocale = vi.mocked(changeLocale)
const mockedConfirmLocale = vi.mocked(confirmLocale)

/** Message keys come back verbatim so assertions read as intent. */
function translate(key: string, params?: Record<string, unknown>): string {
  return params ? `${key}:${JSON.stringify(params)}` : key
}

interface ConfirmOptions {
  title: string
  content: string
  okText: string
}

const confirmDialog = vi.fn<(options: ConfirmOptions) => Promise<boolean>>(
  async () => true,
)

function build() {
  return usePlayerLocaleSettings({
    translate,
    confirm: confirmDialog,
    formatRetryAt: (iso) => `civil(${iso})`,
  })
}

function profileFixture(overrides: Partial<OperatorProfile> = {}): OperatorProfile {
  return {
    id: 'u1',
    display_name: 'Alex',
    aliases: [],
    pronouns: null,
    timezone_id: 'Asia/Taipei',
    has_real_name: true,
    display_name_locked: false,
    country_code: 'TW',
    latitude: 25.05,
    longitude: 121.53,
    location_label: 'Taipei',
    ...overrides,
  }
}

function localeState(remaining: number) {
  return {
    user: authState.currentUser as never,
    remaining_changes: remaining,
    change_limit: 2,
    window_days: 30,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  confirmDialog.mockResolvedValue(true)
  authState.cloudMode = true
  authState.locationHint = null
  authState.currentUser = {
    id: 'u1',
    timezone_id: 'Asia/Taipei',
    primary_language: 'zh-TW',
  }
})

describe('locale change guardrail', () => {
  it('shows the static limit wording until the server reports a count', () => {
    const settings = build()
    expect(settings.localeLimitHint.value).toContain('playerLocale.change.limitHint')
    expect(settings.localeLimitHint.value).toContain('"limit":2')
    expect(settings.localeLimitHint.value).toContain('"days":30')
  })

  it('never asks and never spends a change when nothing moved', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())

    await settings.saveLocale()

    expect(confirmDialog).not.toHaveBeenCalled()
    expect(mockedChangeLocale).not.toHaveBeenCalled()
    expect(settings.localeFeedback.value).toBe('playerLocale.change.unchanged')
  })

  it('warns about irreversibility before touching the endpoint', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())
    settings.timezoneDraft.value = 'Asia/Tokyo'
    mockedChangeLocale.mockResolvedValueOnce(localeState(1))

    await settings.saveLocale()

    const dialog = confirmDialog.mock.calls[0][0]
    expect(dialog.content).toContain('playerLocale.change.confirmBody')
    // The wording must name *which* setting is moving.
    expect(dialog.content).toContain('playerLocale.change.subjectTimezone')
    expect(mockedChangeLocale).toHaveBeenCalledWith({ timezone_id: 'Asia/Tokyo' })
    expect(settings.localeFeedback.value).toBe('playerLocale.change.saved')
  })

  it('does nothing when the player backs out of the dialog', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())
    settings.languageDraft.value = 'ja-JP'
    confirmDialog.mockResolvedValueOnce(false)

    await settings.saveLocale()

    expect(mockedChangeLocale).not.toHaveBeenCalled()
    expect(settings.localeFeedback.value).toBeNull()
  })

  it('names both settings when both move', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())
    settings.timezoneDraft.value = 'Asia/Tokyo'
    settings.languageDraft.value = 'ja-JP'
    mockedChangeLocale.mockResolvedValueOnce(localeState(0))

    await settings.saveLocale()

    const dialog = confirmDialog.mock.calls[0][0]
    expect(dialog.content).toContain('playerLocale.change.subjectBoth')
    expect(mockedChangeLocale).toHaveBeenCalledWith({
      timezone_id: 'Asia/Tokyo',
      primary_language: 'ja-JP',
    })
    expect(settings.localeLimitHint.value).toContain('playerLocale.change.exhausted')
  })

  it('reports the remaining budget from the response', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())
    settings.timezoneDraft.value = 'Asia/Tokyo'
    mockedChangeLocale.mockResolvedValueOnce(localeState(1))

    await settings.saveLocale()

    expect(settings.localeLimitHint.value).toContain('playerLocale.change.remaining')
    expect(settings.localeLimitHint.value).toContain('"count":1')
  })

  it('answers a 429 with when, not just no', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())
    settings.timezoneDraft.value = 'Asia/Tokyo'
    mockedChangeLocale.mockRejectedValueOnce(new LocaleChangeLimitError({
      retryAt: '2026-08-20T03:00:00+00:00',
      limit: 2,
      windowDays: 30,
    }))

    await settings.saveLocale()

    expect(settings.localeFeedback.value).toContain('playerLocale.change.limitReached')
    expect(settings.localeFeedback.value).toContain('civil(2026-08-20T03:00:00+00:00)')
    expect(settings.localeLimitHint.value).toContain('playerLocale.change.exhausted')
  })

  it('falls back to a vaguer message when retry_at is missing', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())
    settings.timezoneDraft.value = 'Asia/Tokyo'
    mockedChangeLocale.mockRejectedValueOnce(new LocaleChangeLimitError({
      retryAt: null,
      limit: 2,
      windowDays: 30,
    }))

    await settings.saveLocale()

    expect(settings.localeFeedback.value)
      .toBe('playerLocale.change.limitReachedUnknown')
  })

  it('asks for a retry when the claim is held, without spending anything', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())
    settings.timezoneDraft.value = 'Asia/Tokyo'
    mockedChangeLocale.mockRejectedValueOnce(new LocaleChangeInProgressError())

    await settings.saveLocale()

    expect(settings.localeFeedback.value).toBe('playerLocale.change.inProgress')
    // A transient claim conflict is not a spent budget: the counter must not
    // move, and the message must not read as a failure with a raw detail.
    expect(settings.remainingChanges.value).toBeNull()
    expect(settings.localeLimitHint.value)
      .toContain('playerLocale.change.limitHint')
  })
})

describe('location hint', () => {
  beforeEach(() => {
    authState.locationHint = {
      country_code: 'JP',
      label: 'Osaka, Japan',
      latitude: 34.69,
      longitude: 135.5,
      timezone_id: 'Asia/Tokyo',
      detected_at: '2026-07-26T00:00:00Z',
    }
  })

  it('accepting writes the place only — never the guarded timezone', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())
    mockedUpdateProfile.mockResolvedValueOnce(profileFixture({
      country_code: 'JP',
      location_label: 'Osaka, Japan',
      latitude: 34.69,
      longitude: 135.5,
    }))

    await settings.acceptHint()

    expect(mockedUpdateProfile).toHaveBeenCalledWith({
      country_code: 'JP',
      location_label: 'Osaka, Japan',
      latitude: 34.69,
      longitude: 135.5,
    })
    expect(mockedChangeLocale).not.toHaveBeenCalled()
    // Timezone stays where the player left it: a trip must not silently
    // spend one of two changes per 30 days.
    expect(settings.timezoneDraft.value).toBe('Asia/Taipei')
    expect(authState.refreshMe).toHaveBeenCalled()
  })

  it('ignoring costs nothing — it rides the free confirm endpoint', async () => {
    const settings = build()
    mockedConfirmLocale.mockResolvedValueOnce(localeState(2))

    await settings.dismissHint()

    expect(mockedConfirmLocale)
      .toHaveBeenCalledWith({ dismiss_location_hint: true })
    expect(mockedChangeLocale).not.toHaveBeenCalled()
    expect(settings.locationFeedback.value).toBe('playerLocale.hint.dismissed')
  })

  it('tells the player to retry when a dismissal races another write', async () => {
    const settings = build()
    mockedConfirmLocale.mockRejectedValueOnce(new LocaleChangeInProgressError())

    await settings.dismissHint()

    expect(settings.locationFeedback.value)
      .toBe('playerLocale.change.inProgress')
  })

  it('surfaces a dismissal failure instead of pretending it worked', async () => {
    const settings = build()
    mockedConfirmLocale.mockRejectedValueOnce(new Error('boom'))

    await settings.dismissHint()

    expect(settings.locationFeedback.value)
      .toContain('playerLocale.hint.failed')
  })
})

describe('place editing', () => {
  it('a picked city carries its coordinates and country silently', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture({
      country_code: null, latitude: null, longitude: null, location_label: null,
    }))

    settings.onPlaceSelected({
      label: 'Kyoto, Japan',
      latitude: 35.02,
      longitude: 135.75,
      country_code: 'JP',
      timezone_id: 'Asia/Tokyo',
    })
    mockedUpdateProfile.mockResolvedValueOnce(profileFixture({
      country_code: 'JP', location_label: 'Kyoto, Japan',
    }))
    await settings.saveLocation()

    expect(mockedUpdateProfile).toHaveBeenCalledWith({
      country_code: 'JP',
      location_label: 'Kyoto, Japan',
      latitude: 35.02,
      longitude: 135.75,
    })
    // Choosing a city must not move the guarded timezone by itself.
    expect(mockedChangeLocale).not.toHaveBeenCalled()
  })

  it('typing over a picked city drops that city\'s coordinates', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture({
      country_code: null, latitude: null, longitude: null, location_label: null,
    }))
    settings.onPlaceSelected({
      label: 'Kyoto, Japan',
      latitude: 35.02,
      longitude: 135.75,
      country_code: 'JP',
      timezone_id: 'Asia/Tokyo',
    })

    // The field reports that the text stopped standing for Kyoto, and the
    // player finishes typing a name of their own.
    settings.onPlaceCleared()
    settings.locationLabelDraft.value = '我家附近的咖啡店'
    mockedUpdateProfile.mockResolvedValueOnce(profileFixture({
      country_code: null,
      latitude: null,
      longitude: null,
      location_label: '我家附近的咖啡店',
    }))
    await settings.saveLocation()

    // A name is a name — it must never be stored at the last city's
    // coordinates, which are invisible in this UI.
    expect(mockedUpdateProfile).toHaveBeenCalledWith({
      country_code: null,
      latitude: null,
      longitude: null,
      location_label: '我家附近的咖啡店',
    })
  })

  it('editing the label drops coordinates that came from the profile too', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture({
      country_code: 'TW',
      latitude: 25.03,
      longitude: 121.56,
      location_label: '臺北市',
    }))

    settings.onPlaceCleared()
    settings.locationLabelDraft.value = '臺北市信義區'
    mockedUpdateProfile.mockResolvedValueOnce(profileFixture({
      country_code: null,
      latitude: null,
      longitude: null,
      location_label: '臺北市信義區',
    }))
    await settings.saveLocation()

    expect(mockedUpdateProfile).toHaveBeenCalledWith({
      country_code: null,
      latitude: null,
      longitude: null,
      location_label: '臺北市信義區',
    })
  })

  it('clearing sends explicit nulls', async () => {
    const settings = build()
    settings.seedFromProfile(profileFixture())
    mockedUpdateProfile.mockResolvedValueOnce(profileFixture({
      country_code: null, latitude: null, longitude: null, location_label: null,
    }))

    await settings.clearLocation()

    expect(mockedUpdateProfile).toHaveBeenCalledWith({
      country_code: null,
      latitude: null,
      longitude: null,
      location_label: null,
    })
    expect(settings.locationFeedback.value)
      .toBe('playerSidebar.location.cleared')
  })

  it('a declined geolocation prompt is a note, not an error', () => {
    const settings = build()
    const getCurrentPosition = vi.fn(
      (_ok: PositionCallback, fail?: PositionErrorCallback | null) => {
        fail?.({ code: 1, message: 'denied' } as GeolocationPositionError)
      },
    )
    vi.stubGlobal('navigator', { geolocation: { getCurrentPosition } })

    settings.useCurrentLocation()

    expect(settings.locationFeedback.value)
      .toBe('playerLocale.location.useCurrentDenied')
    vi.unstubAllGlobals()
  })

  it('says so when the browser cannot share a location at all', () => {
    const settings = build()
    vi.stubGlobal('navigator', {})

    settings.useCurrentLocation()

    expect(settings.locationFeedback.value)
      .toBe('playerLocale.location.useCurrentUnsupported')
    vi.unstubAllGlobals()
  })
})
