import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import type { OperatorProfile } from '@/types/operator'

/**
 * Both place surfaces must react to the field's `deselect`, or a label typed
 * over a picked city is saved at that city's invisible coordinates. The
 * behaviour of each handler is covered without a DOM in
 * `useFirstLoginLocaleDraft.test.ts` / `usePlayerLocaleSettings.test.ts`;
 * what those cannot see is whether the template ever calls them.
 *
 * So the field is replaced by a probe that prints the handlers it was
 * handed — SSR does not fire events, but it does pass listeners down as
 * props, which is exactly the wiring under test.
 */
vi.mock('@/components/CitySearchField.vue', () => ({
  default: defineComponent({
    name: 'CitySearchFieldProbe',
    inheritAttrs: false,
    setup(_props, { attrs }) {
      return () => h(
        'span',
        { class: 'probe' },
        Object.keys(attrs).filter((key) => key.startsWith('on')).sort().join(' '),
      )
    },
  }),
}))

const authState = vi.hoisted(() => ({
  cloudMode: true,
  needsLocaleConfirmation: true,
  currentUser: { id: 'u1', timezone_id: 'Asia/Taipei', primary_language: 'zh-TW' } as
    Record<string, unknown> | null,
  locationHint: null as Record<string, unknown> | null,
  refreshMe: vi.fn(async () => {}),
}))

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
  latitude: 25.03,
  longitude: 121.56,
  location_label: '臺北市',
} as unknown as OperatorProfile

describe('city search invalidation is wired on every place surface', () => {
  it('the first-login gate listens for deselect', async () => {
    const html = await render(FirstLoginLocaleGate)

    expect(html).toContain('onSelect')
    expect(html).toContain('onDeselect')
  })

  it('the settings panel listens for deselect', async () => {
    const html = await render(PlayerPlaceLocaleSettings, { profile: PROFILE })

    expect(html).toContain('onSelect')
    expect(html).toContain('onDeselect')
  })
})
