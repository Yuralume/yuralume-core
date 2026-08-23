import { beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, createSSRApp, ref, type ComputedRef, type Ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import {
  isCloudDeployment,
  setDeploymentMode,
} from '@/composables/deploymentMode'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { messages as zhTW } from '@/i18n/locales/zh-TW'

// No DOM test infra exists in this repo (@vue/test-utils / jsdom are not
// installed), so component coverage is done via SSR — the same pattern as
// cloudCreditsSurface.test.ts. `useRuntimeLimits` is exercised for real
// through its transport seam (`fetchRuntimeLimits`), same as
// useRuntimeLimits.test.ts exercises it directly.
//
// `cloudMode` is derived from the real deployment-mode module rather than a
// second switch this file owns: in production the component's `v-if` and the
// composable's request gate read the same fact, and a test able to set them
// apart would prove nothing about the shipped behaviour.
const holder = vi.hoisted(() => ({
  cloudMode: null as ComputedRef<boolean> | null,
  portalUrl: null as Ref<string | null> | null,
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({ cloudMode: holder.cloudMode, portalUrl: holder.portalUrl }),
}))

const portalUrlRef = ref<string | null>(null)
holder.cloudMode = computed(() => isCloudDeployment())
holder.portalUrl = portalUrlRef

vi.mock('@/utils/api/cloudLimits', () => ({
  fetchRuntimeLimits: vi.fn(),
}))

const { fetchRuntimeLimits } = await import('@/utils/api/cloudLimits')
const { useRuntimeLimits } = await import('@/composables/useRuntimeLimits')
const BackgroundDormancyAdvisory
  = (await import('@/components/BackgroundDormancyAdvisory.vue')).default

const mockedFetch = vi.mocked(fetchRuntimeLimits)
const L = zhTW.backgroundDormancy
// The copy red lines are per-locale properties, so they are checked on all
// three source strings, not just the one the SSR render happens to use.
const locales = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP }

async function render(): Promise<string> {
  const app = createSSRApp(BackgroundDormancyAdvisory)
  app.use(createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  }))
  return renderToString(app)
}

function snapshot(overrides: Record<string, unknown> = {}) {
  return {
    kind: 'ok' as const,
    snapshot: {
      character_slots: null,
      character_daily_creates: null,
      story_scenes_daily: null,
      session_message_limit: null,
      album_generation_enabled: true,
      tts_enabled: true,
      video_generation_enabled: true,
      background: null,
      ...overrides,
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useRuntimeLimits().reset()
  setDeploymentMode('cloud')
  portalUrlRef.value = null
})

describe('BackgroundDormancyAdvisory', () => {
  it('renders nothing before a snapshot has loaded', async () => {
    // `onMounted` fires `ensureLoaded()` but SSR does not run mounted
    // hooks, so this pins the "unloaded" branch directly.
    const html = await render()

    expect(html).not.toContain('dormancy-advisory')
  })

  it('says the dormancy window once a snapshot names one', async () => {
    mockedFetch.mockResolvedValueOnce(snapshot({ background: { dormancyDays: 14 } }))
    await useRuntimeLimits().ensureLoaded()

    const html = await render()

    expect(html).toContain('dormancy-advisory')
    expect(html).toContain('14')
  })

  it('carries the Portal entry when one is configured', async () => {
    portalUrlRef.value = 'https://app.yuralume.com'
    mockedFetch.mockResolvedValueOnce(snapshot({ background: { dormancyDays: 14 } }))
    await useRuntimeLimits().ensureLoaded()

    const html = await render()

    expect(html).toContain('https://app.yuralume.com')
    expect(html).toContain(L.advisory.body.split('{days}')[0])
  })

  it('renders nothing when the tier never goes dormant', async () => {
    mockedFetch.mockResolvedValueOnce(snapshot({ background: { dormancyDays: null } }))
    await useRuntimeLimits().ensureLoaded()

    const html = await render()

    expect(html).not.toContain('dormancy-advisory')
  })

  it('renders nothing when the read degrades', async () => {
    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })
    await useRuntimeLimits().ensureLoaded()

    const html = await render()

    expect(html).not.toContain('dormancy-advisory')
  })

  it('renders nothing on self-host — byte-identical output', async () => {
    mockedFetch.mockResolvedValueOnce(snapshot({ background: { dormancyDays: 14 } }))
    await useRuntimeLimits().ensureLoaded()
    setDeploymentMode('self_host')

    const html = await render()

    expect(html).not.toContain('dormancy-advisory')
    expect(html).not.toContain('14')
  })

  it('names both rungs of the ladder: LINE delivers, paid runs full pace (plan §2 D5)', () => {
    // D5: the core free-vs-試玩 difference is the DELIVERY CHANNEL, not
    // background speed — a proactive message with nowhere to be pushed is no
    // message, true under any cadence knob. Speed is the third rung. Copy
    // that names only the speed difference anchors the tier gap on the wrong
    // axis, so LINE is required here, and so is the paid-plan clause.
    for (const [locale, m] of Object.entries(locales)) {
      const body = m.backgroundDormancy.advisory.body
      expect(body, locale).toContain('LINE')
      // The upgrade clause survives too (zh 升級付費 / en paid plan / ja 有料).
      expect(body, locale).toMatch(/付費|有料|paid plan/)
    }
  })

  it('never calls any rung below paid the "完全體" (plan §5 red line)', () => {
    // This component faces the free / LINE rungs, so the word that is only
    // honest about the paid tier must not appear in any locale. Guards the
    // source strings directly so a future copy edit fails loudly here.
    for (const [locale, m] of Object.entries(locales)) {
      expect(m.backgroundDormancy.advisory.body, locale).not.toContain('完全體')
    }
  })
})
