import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'

// No DOM test infra exists in this repo (@vue/test-utils / jsdom are not
// installed), so component coverage is done via SSR — enough to assert the
// cloud gating and, crucially, the self-host parity promise. Interaction
// behaviour is covered in usePlayerLocaleSettings.test.ts.
// See cloudCreditsSurface.test.ts for the same pattern.
const authState = vi.hoisted(() => ({
  cloudMode: true,
  needsLocaleConfirmation: true,
  currentUser: null as Record<string, unknown> | null,
  locationHint: null as Record<string, unknown> | null,
  refreshMe: vi.fn(async () => {}),
}))

// Real computeds, not `{ value }` stand-ins: these are read straight from
// templates, where Vue only auto-unwraps genuine refs — a plain object
// would be truthy even when its value is null and silently render the
// cloud branch on self-host.
vi.mock('@/composables/useAuth', async () => {
  const { computed } = await import('vue')
  return {
    useAuth: () => ({
      cloudMode: computed(() => authState.cloudMode),
      needsLocaleConfirmation: computed(() => authState.needsLocaleConfirmation),
      currentUser: computed(() => authState.currentUser),
      locationHint: computed(() => authState.locationHint),
      refreshMe: authState.refreshMe,
    }),
  }
})

// The real one pulls in ant-design-vue's Modal, which wants a document.
vi.mock('@/composables/useConfirmDialog', () => ({
  useConfirmDialog: () => vi.fn(async () => true),
}))

vi.mock('@/utils/api/operatorProfile', () => ({
  updateOperatorProfile: vi.fn(),
  getOperatorProfile: vi.fn(),
}))

vi.mock('@/utils/api/playerLocale', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/utils/api/playerLocale')>()
  return {
    ...actual,
    changeLocale: vi.fn(),
    confirmLocale: vi.fn(),
    searchPlaces: vi.fn(async () => []),
  }
})

const FirstLoginLocaleGate
  = (await import('@/components/FirstLoginLocaleGate.vue')).default
const PlayerPlaceLocaleSettings
  = (await import('@/components/PlayerPlaceLocaleSettings.vue')).default
const LocationHintBanner
  = (await import('@/components/LocationHintBanner.vue')).default
const CitySearchField
  = (await import('@/components/CitySearchField.vue')).default

const L = zhTW.playerLocale
const LEGACY = zhTW.locale

async function render(component: unknown, props: Record<string, unknown> = {}) {
  const app = createSSRApp(
    component as Parameters<typeof createSSRApp>[0],
    props,
  )
  app.use(createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  }))
  return renderToString(app)
}

const PROFILE = {
  id: 'u1',
  display_name: 'Alex',
  aliases: [],
  pronouns: null,
  timezone_id: 'Asia/Taipei',
  has_real_name: true,
  display_name_locked: false,
  current_status: null,
  current_status_set_at: null,
  country_code: 'TW',
  latitude: 25.05,
  longitude: 121.53,
  location_label: 'Taipei',
}

beforeEach(() => {
  vi.clearAllMocks()
  authState.cloudMode = true
  authState.needsLocaleConfirmation = true
  authState.locationHint = null
  authState.currentUser = {
    id: 'u1',
    timezone_id: 'Asia/Taipei',
    primary_language: 'zh-TW',
  }
})

describe('FirstLoginLocaleGate', () => {
  it('blocks a hosted player who has not confirmed their locale', async () => {
    const html = await render(FirstLoginLocaleGate)

    expect(html).toContain(L.confirm.title)
    expect(html).toContain(L.confirm.intro)
    // The three things the plan says must be confirmable, and the city
    // search rather than a coordinate form.
    expect(html).toContain('locale-gate-city')
    expect(html).toContain('locale-gate-timezone')
    expect(html).toContain('locale-gate-language')
    expect(html).not.toContain(LEGACY.location.latitudePlaceholder)
  })

  it('renders nothing on self-host', async () => {
    authState.cloudMode = false

    const html = await render(FirstLoginLocaleGate)

    expect(html).not.toContain(L.confirm.title)
    expect(html).not.toContain('locale-gate')
  })

  it('renders nothing once the player has confirmed', async () => {
    authState.needsLocaleConfirmation = false

    const html = await render(FirstLoginLocaleGate)

    expect(html).not.toContain('locale-gate')
  })

  it('stays out of the login / setup / callback routes', async () => {
    const html = await render(FirstLoginLocaleGate, { publicRoute: true })

    expect(html).not.toContain('locale-gate')
  })

  it('waits for a resolved identity before covering the app', async () => {
    authState.currentUser = null

    const html = await render(FirstLoginLocaleGate)

    expect(html).not.toContain('locale-gate')
  })

  it('prefills the timezone suggested by the login location hint', async () => {
    authState.currentUser = {
      id: 'u1', timezone_id: 'UTC', primary_language: 'zh-TW',
    }
    authState.locationHint = {
      country_code: 'JP',
      label: 'Osaka, Japan',
      latitude: 34.69,
      longitude: 135.5,
      timezone_id: 'Asia/Tokyo',
      detected_at: null,
    }

    const html = await render(FirstLoginLocaleGate)

    expect(html).toContain('Osaka, Japan')
    expect(html).toContain('Asia/Tokyo')
  })
})

describe('PlayerPlaceLocaleSettings — cloud', () => {
  it('offers a city search and an editable timezone, never coordinates', async () => {
    const html = await render(PlayerPlaceLocaleSettings, { profile: PROFILE })

    expect(html).toContain('operator-city-search')
    expect(html).toContain(L.city.label)
    expect(html).toContain('operator-timezone')
    expect(html).toContain('operator-primary-language')
    expect(html).toContain(L.location.useCurrent)
    // Latitude / longitude are a technical artefact, not a place.
    expect(html).not.toContain(LEGACY.location.latitudePlaceholder)
    expect(html).not.toContain(LEGACY.location.longitudePlaceholder)
    expect(html).not.toContain(LEGACY.timezone.readonlyExplain)
  })

  it('states the guardrail before anything has been changed', async () => {
    const html = await render(PlayerPlaceLocaleSettings, { profile: PROFILE })

    expect(html).toContain('30')
    expect(html).toContain(L.change.save)
  })

  it('asks about a relocation next to the field the answer would change', async () => {
    authState.locationHint = {
      country_code: 'JP',
      label: 'Osaka, Japan',
      latitude: 34.69,
      longitude: 135.5,
      timezone_id: 'Asia/Tokyo',
      detected_at: null,
    }

    const html = await render(PlayerPlaceLocaleSettings, { profile: PROFILE })

    expect(html).toContain('Osaka, Japan')
    expect(html).toContain(L.hint.accept)
    expect(html).toContain(L.hint.dismiss)
  })

  it('uses the dark-theme form classes for both native controls', async () => {
    const html = await render(PlayerPlaceLocaleSettings, { profile: PROFILE })

    expect(html).toContain('field-select')
    expect(html).toContain('field-input')
  })
})

describe('PlayerPlaceLocaleSettings — self-host parity', () => {
  beforeEach(() => {
    authState.cloudMode = false
  })

  it('keeps the read-only locale rows and the manual coordinate form', async () => {
    const html = await render(PlayerPlaceLocaleSettings, { profile: PROFILE })

    expect(html).toContain(LEGACY.timezone.readonlyExplain)
    expect(html).toContain(LEGACY.location.latitudePlaceholder)
    expect(html).toContain(LEGACY.location.longitudePlaceholder)
    expect(html).toContain(LEGACY.location.countryPlaceholder)
    expect(html).toContain('Asia/Taipei')
    expect(html).toContain(zhTW.playerSidebar.location.label)
  })

  it('grows no city search, no editable timezone and no hint bar', async () => {
    authState.locationHint = {
      country_code: 'JP', label: 'Osaka, Japan', latitude: null,
      longitude: null, timezone_id: null, detected_at: null,
    }

    const html = await render(PlayerPlaceLocaleSettings, { profile: PROFILE })

    expect(html).not.toContain('operator-city-search')
    expect(html).not.toContain('operator-timezone')
    expect(html).not.toContain('operator-primary-language')
    expect(html).not.toContain(L.city.label)
    expect(html).not.toContain(L.location.useCurrent)
    // A hosted-only suggestion must never leak into the self-host panel.
    expect(html).not.toContain(L.hint.accept)
  })
})

describe('LocationHintBanner', () => {
  it('falls back to the country when the geocoder gave no city', async () => {
    const html = await render(LocationHintBanner, {
      hint: {
        country_code: 'JP',
        label: null,
        latitude: null,
        longitude: null,
        timezone_id: null,
        detected_at: null,
      },
    })

    expect(html).toContain('JP')
    expect(html).toContain(L.hint.accept)
    expect(html).toContain(L.hint.dismiss)
  })
})

describe('CitySearchField invalidation contract', () => {
  it('declares the deselect emit both place surfaces depend on', () => {
    // The behaviour itself needs a DOM this repo does not have, so lock the
    // boundary instead: if the emit disappears, the two `@deselect` handlers
    // that drop stale coordinates would silently stop firing and a typed
    // label would again be saved at the last picked city's latitude.
    const emits = (CitySearchField as { emits?: string[] }).emits ?? []
    expect(emits).toContain('deselect')
    expect(emits).toContain('select')
  })
})
