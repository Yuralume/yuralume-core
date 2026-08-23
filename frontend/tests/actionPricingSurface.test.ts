import { beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, createSSRApp, ref, type ComputedRef, type Ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import {
  isCloudDeployment,
  setDeploymentMode,
} from '@/composables/deploymentMode'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { findJargon } from './fixtures/cloud-jargon-denylist'

/**
 * Rendering guard for the AP2/AP3/AP4 player surfaces: the price hints, the
 * pre-check card and the quota-overage switches.
 *
 * Same SSR approach as `cloudCreditsSurface.test.ts` (no DOM test infra in
 * this repo). Three promises are asserted throughout:
 *   1. a number is only ever shown when it is the number the ledger will use;
 *   2. self-host renders none of this at all;
 *   3. none of it speaks in operator jargon.
 */
const holder = vi.hoisted(() => ({
  cloudMode: null as ComputedRef<boolean> | null,
  portalUrl: null as Ref<string | null> | null,
  currentUser: null as Ref<Record<string, unknown> | null> | null,
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    cloudMode: holder.cloudMode,
    portalUrl: holder.portalUrl,
    currentUser: holder.currentUser,
  }),
}))

vi.mock('@/utils/api/cloudPricing', () => ({
  fetchCloudPricing: vi.fn(),
}))

vi.mock('@/utils/api/overageSettings', () => ({
  fetchOverageSettings: vi.fn(),
  updateOverageSettings: vi.fn(),
}))

vi.mock('@/utils/api/cloudCredits', () => ({
  fetchCloudCredits: vi.fn(async () => ({ kind: 'degraded' as const })),
}))

// One switch, not two: in production a component's `v-if="cloudMode"` and a
// composable's request gate read the same deployment fact, so the tests move
// `setDeploymentMode` and let both follow.
holder.cloudMode = computed(() => isCloudDeployment())
holder.portalUrl = ref<string | null>(null)
holder.currentUser = ref<Record<string, unknown> | null>({ id: 'user-1' })

const { fetchCloudPricing } = await import('@/utils/api/cloudPricing')
const { fetchOverageSettings } = await import('@/utils/api/overageSettings')
const { useActionPricing, ACTION_CHAT, ACTION_IMAGE_PORTRAIT }
  = await import('@/composables/useActionPricing')
const { useOverageSettings } = await import('@/composables/useOverageSettings')
const ActionPriceHint = (await import('@/components/ActionPriceHint.vue')).default
const InsufficientCreditsNotice
  = (await import('@/components/InsufficientCreditsNotice.vue')).default
const QuotaOverageSettings
  = (await import('@/components/QuotaOverageSettings.vue')).default
const DramaExitHub
  = (await import('@/components/branching-drama/DramaExitHub.vue')).default

const mockedPricing = vi.mocked(fetchCloudPricing)
const mockedOverage = vi.mocked(fetchOverageSettings)

const L = zhTW.credits

const CATALOGS = {
  'zh-TW': zhTW,
  'en-US': enUS,
  'ja-JP': jaJP,
} as const

async function render(
  component: unknown,
  props: Record<string, unknown> = {},
  locale: keyof typeof CATALOGS = 'zh-TW',
): Promise<string> {
  const app = createSSRApp(
    component as Parameters<typeof createSSRApp>[0],
    props,
  )
  app.use(createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'zh-TW',
    messages: CATALOGS,
  }))
  return renderToString(app)
}

function visibleText(html: string): string {
  return html
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<[^>]*>/g, ' ')
}

async function seedPricing(
  tiers: Array<{
    tier_name: string
    billing_shape: string
    actions: Array<{
      action_key: string
      unit: string
      price_cr: number
      overage: boolean
    }>
  }>,
): Promise<void> {
  mockedPricing.mockResolvedValueOnce({
    kind: 'ok',
    snapshot: { tiers, stale: false },
  })
  await useActionPricing().ensureLoaded()
}

function fixedTier(actions: Array<[string, string, number]>, name = 'standard') {
  return {
    tier_name: name,
    billing_shape: 'action_fixed',
    actions: actions.map(([action_key, unit, price_cr]) => ({
      action_key,
      unit,
      price_cr,
      overage: action_key.endsWith('_overage'),
    })),
  }
}

async function seedOverage(settings: {
  chat_image_enabled?: boolean
  feed_post_enabled?: boolean
  tier_overage_enabled?: boolean
  daily_limit?: number
  chat_image_price_cr?: number | null
  feed_post_price_cr?: number | null
}): Promise<void> {
  mockedOverage.mockResolvedValueOnce({
    kind: 'ok',
    settings: {
      chat_image_enabled: false,
      feed_post_enabled: false,
      tier_overage_enabled: true,
      daily_limit: 5,
      chat_image_price_cr: 8,
      feed_post_price_cr: 6,
      ...settings,
    },
  })
  await useOverageSettings().ensureLoaded()
}

beforeEach(() => {
  vi.clearAllMocks()
  setDeploymentMode('cloud')
  useActionPricing().reset()
  useOverageSettings().reset()
})

describe('ActionPriceHint', () => {
  it('quotes the published price with the unit the player counts in', async () => {
    await seedPricing([fixedTier([['chat', 'per_message', 3]])])

    const html = await render(ActionPriceHint, {
      actionKey: ACTION_CHAT,
      tooltipKey: 'credits.price.chatTooltip',
    })

    expect(html).toContain('3')
    expect(html).toContain('螢火')
    expect(visibleText(html)).toContain('每則')
    expect(html).toContain(L.price.chatTooltip)
  })

  it('says "per picture" for an image action', async () => {
    await seedPricing([fixedTier([['image_portrait', 'per_portrait', 12]])])

    const html = await render(ActionPriceHint, { actionKey: ACTION_IMAGE_PORTRAIT })

    expect(visibleText(html)).toContain('每次')
    expect(html).toContain('12')
  })

  it('says "per rewrite" for a fusion iteration', async () => {
    // `per_iteration` is the second-wave Creator Studio unit: re-running one
    // stage is a separate, cheaper action from the one-price story.
    await seedPricing([fixedTier([['fusion_story_iterate', 'per_iteration', 5]])])

    const html = await render(ActionPriceHint, {
      actionKey: 'fusion_story_iterate',
      tooltipKey: 'credits.price.fusionIterateTooltip',
    })

    expect(visibleText(html)).toContain('每次')
    expect(html).toContain('5')
    expect(html).toContain(L.price.fusionIterateTooltip)
  })

  it('renders nothing on self-host', async () => {
    // Hard requirement: the self-host composer and image panel are unchanged.
    await seedPricing([fixedTier([['chat', 'per_message', 3]])])
    setDeploymentMode('self_host')

    const html = await render(ActionPriceHint, { actionKey: ACTION_CHAT })

    expect(html).not.toContain('螢火')
    expect(html).not.toContain('action-price')
  })

  it('renders nothing while the price list is unreadable', async () => {
    mockedPricing.mockResolvedValueOnce({ kind: 'degraded' })
    await useActionPricing().ensureLoaded()

    const html = await render(ActionPriceHint, { actionKey: ACTION_CHAT })

    expect(html).not.toContain('action-price')
  })

  it('renders nothing rather than one tier’s price to another tier’s player', async () => {
    await seedPricing([
      fixedTier([['chat', 'per_message', 3]], 'standard'),
      fixedTier([['chat', 'per_message', 2]], 'pro'),
    ])

    const html = await render(ActionPriceHint, { actionKey: ACTION_CHAT })

    expect(html).not.toContain('action-price')
    expect(html).not.toContain('3')
    expect(html).not.toContain('2')
  })

  it('renders nothing while the deployment still bills by usage', async () => {
    await seedPricing([{
      tier_name: 'standard',
      billing_shape: 'token_floating',
      actions: [{
        action_key: 'chat', unit: 'per_message', price_cr: 3, overage: false,
      }],
    }])

    const html = await render(ActionPriceHint, { actionKey: ACTION_CHAT })

    expect(html).not.toContain('action-price')
  })

  it('speaks all three languages', async () => {
    await seedPricing([fixedTier([['chat', 'per_message', 3]])])

    const en = await render(ActionPriceHint, { actionKey: ACTION_CHAT }, 'en-US')
    const ja = await render(ActionPriceHint, { actionKey: ACTION_CHAT }, 'ja-JP')

    expect(visibleText(en)).toContain('per message')
    expect(visibleText(en)).toContain('Lumes')
    expect(visibleText(ja)).toContain('1通あたり')
    expect(visibleText(ja)).toContain('蛍火')
  })
})

/**
 * FX2 — 「換條路再走」 is a *press that costs money*, and every other such
 * press in the drama already says so before it is pressed. Starting a
 * playthrough writes the opening beat and is billed as one advance (FX1), so
 * this quotes the advance price, not the (already paid) build price.
 */
describe('the ending’s replay CTA quotes what pressing it costs', () => {
  it('shows the advance price beside 「換條路再走」', async () => {
    await seedPricing([fixedTier([
      ['branching_drama_advance', 'per_iteration', 6],
      ['branching_drama_create', 'per_drama', 40],
    ])])

    const html = await render(DramaExitHub, { dramaTitle: 'Glass Signal' })

    expect(html).toContain(zhTW.branchingDrama.exitHub.replay)
    expect(html).toContain('action-price')
    expect(html).toContain('6')
    expect(html).toContain(L.price.dramaStartTooltip)
    // Emphatically not the build price — that press already happened.
    expect(html).not.toContain('40')
  })

  it('renders no chip at all on self-host', async () => {
    await seedPricing([fixedTier([['branching_drama_advance', 'per_iteration', 6]])])
    setDeploymentMode('self_host')

    const html = await render(DramaExitHub, { dramaTitle: 'Glass Signal' })

    expect(html).toContain(zhTW.branchingDrama.exitHub.replay)
    expect(html).not.toContain('action-price')
  })
})

describe('InsufficientCreditsNotice pre-check line', () => {
  // The stem of the extra line, minus its interpolation — present only when
  // the card is stating a price.
  const REQUIRED_STEM = L.insufficient.required.split('{amount}')[0].trim()

  it('states what the refused action would have cost', async () => {
    const html = await render(InsufficientCreditsNotice, { requiredCr: 12 })

    expect(html).toContain(L.insufficient.body)
    expect(html).toContain(REQUIRED_STEM)
    expect(html).toContain('12')
  })

  it('is unchanged for the server-side refusal', async () => {
    // The 402 path has no price to state; the card must render exactly as
    // it always has.
    const withoutPrice = await render(InsufficientCreditsNotice)
    const withPrice = await render(InsufficientCreditsNotice, { requiredCr: 12 })

    expect(withoutPrice).toContain(L.insufficient.body)
    expect(withoutPrice).not.toContain(REQUIRED_STEM)
    // ...and the stem really is what distinguishes the two renders.
    expect(withPrice).not.toEqual(withoutPrice)
  })

  it('never renders a zero or negative price as a real number', async () => {
    for (const requiredCr of [0, -5, Number.NaN]) {
      const html = await render(InsufficientCreditsNotice, { requiredCr })

      expect(html).not.toContain(REQUIRED_STEM)
    }
  })
})

describe('QuotaOverageSettings', () => {
  it('shows both switches with their price and daily ceiling', async () => {
    await seedPricing([fixedTier([
      ['chat_image_overage', 'per_image', 8],
      ['feed_post_overage', 'per_post', 6],
    ])])
    await seedOverage({ chat_image_enabled: true })

    const html = await render(QuotaOverageSettings)

    expect(html).toContain(L.overage.title)
    expect(html).toContain(L.overage.chatImage.label)
    expect(html).toContain(L.overage.chatImage.hint)
    expect(html).toContain(L.overage.feedPost.label)
    expect(html).toContain(L.overage.feedPost.hint)
    expect(html).toContain('8')
    expect(html).toContain('6')
    expect(html).toContain(L.overage.dailyLimit.replace('{count}', '5'))
    // The switch the player already turned on renders as on.
    expect(html).toContain('checked')
  })

  it('says the price is unknown instead of inventing one', async () => {
    mockedPricing.mockResolvedValueOnce({ kind: 'degraded' })
    await useActionPricing().ensureLoaded()
    await seedOverage({ chat_image_price_cr: null, feed_post_price_cr: null })

    const html = await render(QuotaOverageSettings)

    expect(html).toContain(L.overage.priceUnknown)
  })

  it('cannot be armed while there is no price to agree to', async () => {
    // C6: a switch stored without a price is consent to an open tab, so the
    // control is shut and the reason is on screen — not a toggle that springs
    // back after a refused write.
    mockedPricing.mockResolvedValueOnce({ kind: 'degraded' })
    await useActionPricing().ensureLoaded()
    await seedOverage({ chat_image_price_cr: null, feed_post_price_cr: null })

    const html = await render(QuotaOverageSettings)

    expect(html).toContain('disabled')
    expect(html).toContain(L.overage.blockedNoPrice)
  })

  it('warns when the current plan does not offer overage at all', async () => {
    await seedOverage({ tier_overage_enabled: false })

    const html = await render(QuotaOverageSettings)

    expect(html).toContain(L.overage.tierClosed)
    expect(html).toContain(L.overage.blockedTierClosed)
    expect(html).toContain('disabled')
  })

  it('treats a zero daily ceiling as nothing to arm', async () => {
    await seedOverage({ daily_limit: 0 })

    const html = await render(QuotaOverageSettings)

    expect(html).toContain(L.overage.blockedTierClosed)
  })

  it('leaves a switch that is already on switchable off', async () => {
    // Revoking consent is never blocked, whatever the plan or the price list
    // are doing — the tier closing under a player must not trap their spend.
    await seedOverage({
      chat_image_enabled: true,
      tier_overage_enabled: false,
      chat_image_price_cr: null,
      feed_post_price_cr: null,
    })

    const html = await render(QuotaOverageSettings)

    const chatImageInput = html.slice(
      html.indexOf('<input'), html.indexOf('</label>'),
    )
    expect(chatImageInput).toContain('checked')
    expect(chatImageInput).not.toContain('disabled')
  })

  it('renders nothing on self-host', async () => {
    await seedOverage({})
    setDeploymentMode('self_host')

    const html = await render(QuotaOverageSettings)

    expect(html).not.toContain(L.overage.title)
    expect(html).not.toContain('overage-settings')
  })

  it('renders nothing when the switches cannot be read', async () => {
    // Never draw consent switches from defaults: "off" would be a claim
    // about this player that we have not verified.
    mockedOverage.mockResolvedValueOnce({ kind: 'degraded' })
    await useOverageSettings().ensureLoaded()

    const html = await render(QuotaOverageSettings)

    expect(html).not.toContain('overage-settings')
  })

  it('speaks all three languages', async () => {
    await seedPricing([fixedTier([['feed_post_overage', 'per_post', 6]])])
    await seedOverage({})

    const en = await render(QuotaOverageSettings, {}, 'en-US')
    const ja = await render(QuotaOverageSettings, {}, 'ja-JP')

    expect(en).toContain(enUS.credits.overage.feedPost.hint)
    expect(ja).toContain(jaJP.credits.overage.feedPost.hint)
  })
})

describe('composer picture disclosure', () => {
  // H1: the chat price covers one message, not a picture drawn inside it. The
  // line lives in ChatPanel (too large to render here), so its copy contract
  // is asserted directly: a real per-picture number, in every language.
  it('quotes a per-picture price in all three languages', () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const line = catalog.credits.price.chatImageExtra
      expect(line, locale).toContain('{amount}')
      expect(findJargon(line), locale).toEqual([])
    }
  })
})

describe('hosted jargon sweep', () => {
  it('none of the pricing surfaces uses a denied term', async () => {
    await seedPricing([fixedTier([
      ['chat', 'per_message', 3],
      ['chat_image_overage', 'per_image', 8],
      ['feed_post_overage', 'per_post', 6],
    ])])
    await seedOverage({ tier_overage_enabled: false })

    for (const locale of ['zh-TW', 'en-US', 'ja-JP'] as const) {
      const rendered = [
        await render(
          ActionPriceHint,
          { actionKey: ACTION_CHAT, tooltipKey: 'credits.price.chatTooltip' },
          locale,
        ),
        await render(QuotaOverageSettings, {}, locale),
        await render(InsufficientCreditsNotice, { requiredCr: 3 }, locale),
      ]
      for (const html of rendered) {
        expect(findJargon(visibleText(html)), locale).toEqual([])
      }
    }
  })

  it('covers the tooltips and refusal line no render reaches', async () => {
    // Tooltips live in a `title` attribute and the price-changed line behind a
    // failed request; both are player-facing copy, so they get the same sweep
    // — in all three languages.
    for (const catalog of Object.values(CATALOGS)) {
      const price = catalog.credits.price
      for (const value of [
        price.draftTooltip,
        price.fusionTooltip,
        price.fusionIterateTooltip,
        price.changed,
        price.perIteration,
      ]) {
        expect(findJargon(value)).toEqual([])
      }
    }
  })

  it('covers the confirmation copy no render reaches', async () => {
    // The background-spend confirmation lives in a modal behind a click, so
    // the catalogue is scanned directly for it.
    for (const catalog of Object.values(CATALOGS)) {
      const overage = catalog.credits.overage
      for (const value of [
        overage.confirmTitle,
        overage.confirmBody,
        overage.confirmAccept,
        overage.saveFailed,
        overage.intro,
        overage.blockedTierClosed,
        overage.blockedNoPrice,
      ]) {
        expect(findJargon(value)).toEqual([])
      }
    }
  })
})
