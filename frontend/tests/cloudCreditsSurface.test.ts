import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'

// No DOM test infra exists in this repo (@vue/test-utils / jsdom are not
// installed), so component coverage is done via SSR — enough to assert the
// cloud gating, the self-host parity promise, and the low-balance / stale
// affordances. See fusionStoryExitHub.test.ts for the same pattern.
const authState = vi.hoisted(() => ({
  cloudMode: true,
  portalUrl: null as string | null,
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    cloudMode: { value: authState.cloudMode },
    portalUrl: { value: authState.portalUrl },
  }),
}))

vi.mock('@/utils/api/cloudCredits', () => ({
  fetchCloudCredits: vi.fn(),
}))

const { fetchCloudCredits } = await import('@/utils/api/cloudCredits')
const { useCloudCredits } = await import('@/composables/useCloudCredits')
const CloudCreditsBadge = (await import('@/components/CloudCreditsBadge.vue')).default
const InsufficientCreditsNotice
  = (await import('@/components/InsufficientCreditsNotice.vue')).default
const PortalAccountLink = (await import('@/components/PortalAccountLink.vue')).default

const mockedFetch = vi.mocked(fetchCloudCredits)
const L = zhTW.credits

async function render(component: unknown): Promise<string> {
  const app = createSSRApp(component as Parameters<typeof createSSRApp>[0])
  app.use(createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  }))
  return renderToString(app)
}

async function seedBalance(
  balance: { gift_cr: number; purchased_cr: number; total_cr: number; stale?: boolean },
): Promise<void> {
  mockedFetch.mockResolvedValueOnce({
    kind: 'ok',
    snapshot: { stale: false, ...balance },
  })
  await useCloudCredits().refresh()
}

beforeEach(() => {
  vi.clearAllMocks()
  useCloudCredits().reset()
  authState.cloudMode = true
  authState.portalUrl = null
})

describe('CloudCreditsBadge', () => {
  it('renders the total with the localized credit unit', async () => {
    await seedBalance({ gift_cr: 800, purchased_cr: 4540, total_cr: 5340 })

    const html = await render(CloudCreditsBadge)

    expect(html).toContain('5,340')
    expect(html).toContain('螢火')
  })

  it('renders nothing on self-host', async () => {
    // Hard requirement: the self-host sidebar output must not change at all.
    await seedBalance({ gift_cr: 10, purchased_cr: 10, total_cr: 20 })
    authState.cloudMode = false

    const html = await render(CloudCreditsBadge)

    expect(html).not.toContain('螢火')
    expect(html).not.toContain('credits-badge')
  })

  it('renders nothing when the balance is unknown', async () => {
    // Never a fabricated zero: an unknown balance reads as "you are broke".
    mockedFetch.mockResolvedValueOnce({ kind: 'degraded' })
    await useCloudCredits().refresh()

    const html = await render(CloudCreditsBadge)

    expect(html).not.toContain('credits-badge__amount')
  })

  it('marks a low balance for the warning treatment', async () => {
    await seedBalance({ gift_cr: 0, purchased_cr: 120, total_cr: 120 })

    const html = await render(CloudCreditsBadge)

    expect(html).toContain('is-low')
    expect(html).toContain(L.badge.lowTooltip)
  })

  it('dims a stale balance and says so', async () => {
    await seedBalance({
      gift_cr: 500, purchased_cr: 500, total_cr: 1000, stale: true,
    })

    const html = await render(CloudCreditsBadge)

    expect(html).toContain('is-stale')
    expect(html).toContain(L.badge.staleTooltip)
  })
})

describe('InsufficientCreditsNotice', () => {
  it('states the promise: nothing ran, daily life is unaffected', async () => {
    const html = await render(InsufficientCreditsNotice)

    expect(html).toContain(L.insufficient.title)
    expect(html).toContain(L.insufficient.body)
  })

  it('links the top-up CTA at the portal credits page', async () => {
    authState.portalUrl = 'https://app.yuralume.com/'

    const html = await render(InsufficientCreditsNotice)

    expect(html).toContain('https://app.yuralume.com/credits')
    expect(html).toContain(L.insufficient.cta)
  })

  it('drops the CTA rather than rendering a dead link without a portal', async () => {
    const html = await render(InsufficientCreditsNotice)

    expect(html).toContain(L.insufficient.body)
    expect(html).not.toContain(L.insufficient.cta)
  })
})

describe('PortalAccountLink', () => {
  it('links back to the account centre in cloud mode', async () => {
    authState.portalUrl = 'https://app.yuralume.com'

    const html = await render(PortalAccountLink)

    expect(html).toContain('https://app.yuralume.com')
    expect(html).toContain(L.portalEntry.title)
  })

  it('renders nothing on self-host even if a portal url leaked into config', async () => {
    authState.cloudMode = false
    authState.portalUrl = 'https://app.yuralume.com'

    const html = await render(PortalAccountLink)

    expect(html).not.toContain('app.yuralume.com')
    expect(html).not.toContain(L.portalEntry.title)
  })

  it('renders nothing in cloud mode without a configured portal', async () => {
    const html = await render(PortalAccountLink)

    expect(html).not.toContain(L.portalEntry.title)
  })
})
