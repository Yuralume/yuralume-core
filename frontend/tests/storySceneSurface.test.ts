import { beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, createSSRApp, h, ref, type ComputedRef, type Ref } from 'vue'
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
 * Rendering guard for the story-scene player surface (plan SC, ticket SC2).
 *
 * Three promises, the same three every player surface in this repo carries:
 *   1. a scene reads as a scene — the frame is not a chat bubble wearing a
 *      label, and the control tells the truth about the scene's state;
 *   2. self-host is complete on its own: this whole surface is identical in
 *      both modes, because nothing here is billing (SC3-C adds that, into a
 *      slot, without touching any of it);
 *   3. none of it speaks in operator jargon.
 *
 * SSR rather than DOM, per the repo convention (no jsdom / test-utils here).
 */
// `cloudMode` is derived from the real deployment-mode module rather than a
// second switch this file owns: in production the component's `v-if` and the
// composable's request gate read the same fact, and a test able to set them
// apart would prove nothing about the shipped behaviour.
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

vi.mock('@/utils/api/cloudPricing', () => ({ fetchCloudPricing: vi.fn() }))

holder.cloudMode = computed(() => isCloudDeployment())
holder.portalUrl = ref<string | null>(null)
holder.currentUser = ref<Record<string, unknown> | null>({ id: 'user-1' })

const SceneFrame = (await import('@/components/SceneFrame.vue')).default
const StorySceneControl
  = (await import('@/components/StorySceneControl.vue')).default
const StorySceneChips
  = (await import('@/components/StorySceneChips.vue')).default
const ActionPriceHint
  = (await import('@/components/ActionPriceHint.vue')).default
const { fetchCloudPricing } = await import('@/utils/api/cloudPricing')
const { useActionPricing, ACTION_STORY_SCENE_OPEN }
  = await import('@/composables/useActionPricing')

const mockedPricing = vi.mocked(fetchCloudPricing)

/**
 * The control as `ChatPanel` actually composes it: the hosted price chip
 * mounted into the `#price` slot the component leaves empty (SC3-C).
 */
const PricedControl = {
  render: () => h(StorySceneControl, { sceneOpen: false }, {
    price: () => h(ActionPriceHint, {
      actionKey: ACTION_STORY_SCENE_OPEN,
      tooltipKey: 'credits.price.storySceneTooltip',
      variant: 'chip',
    }),
  }),
}

async function seedScenePrice(priceCr: number): Promise<void> {
  mockedPricing.mockResolvedValueOnce({
    kind: 'ok',
    snapshot: {
      stale: false,
      tiers: [{
        tier_name: 'standard',
        billing_shape: 'action_fixed',
        actions: [{
          action_key: 'story_scene_open',
          unit: 'per_scene',
          price_cr: priceCr,
          overage: false,
        }],
      }],
    },
  })
  await useActionPricing().ensureLoaded()
}

const CATALOGS = {
  'zh-TW': zhTW,
  'en-US': enUS,
  'ja-JP': jaJP,
} as const

type Locale = keyof typeof CATALOGS

const L = zhTW.chat.storyScene

async function render(
  component: unknown,
  props: Record<string, unknown> = {},
  locale: Locale = 'zh-TW',
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

const OPENING = {
  text: '電梯門在頂樓打開，風先一步撲了過來。\n\n她坐在水塔的陰影裡，沒有回頭。',
  title: '頂樓的獨奏',
  location: '頂樓天台',
  mood: '欲言又止',
}

beforeEach(() => {
  vi.clearAllMocks()
  setDeploymentMode('cloud')
  useActionPricing().reset()
})

describe('SceneFrame', () => {
  it('frames the narration with the scene it belongs to', async () => {
    const html = await render(SceneFrame, OPENING)

    expect(html).toContain(L.label)
    expect(html).toContain(OPENING.title)
    expect(html).toContain(OPENING.location)
    expect(html).toContain(OPENING.mood)
    expect(html).toContain(L.meta.location)
    expect(html).toContain(L.meta.mood)
    expect(visibleText(html)).toContain('她坐在水塔的陰影裡')
  })

  it('is not a chat bubble', async () => {
    // The whole promise of the feature is "visibly different from chatting".
    // Bubbles hug one side and cap at 90% width; the frame spans the thread.
    const html = await render(SceneFrame, OPENING)

    expect(html).toContain('scene-frame')
    expect(html).not.toContain('chat-bubble')
    expect(html).toMatch(/<section[^>]*role="note"/)
  })

  it('keeps the narration’s paragraphs apart', async () => {
    const html = await render(SceneFrame, OPENING)

    expect(html.match(/<p[^>]*>/g) ?? []).toHaveLength(2)
  })

  it('renders a bare narration with no heading at all', async () => {
    // Older scenes in the thread have no metadata we can vouch for; an
    // invented or blank heading would be worse than none.
    const html = await render(SceneFrame, { text: 'The roof empties out.' })

    expect(html).toContain(L.label)
    expect(html).not.toContain('scene-frame__head')
    expect(html).toContain('The roof empties out.')
  })

  it('marks the send-off as a send-off', async () => {
    const html = await render(SceneFrame, {
      text: 'The roof empties out.',
      closing: true,
    })

    expect(html).toContain(L.closingLabel)
    expect(html).not.toContain(`>${L.label}<`)
    expect(html).toContain('scene-frame--closing')
  })

  it('speaks all three languages', async () => {
    const en = await render(SceneFrame, OPENING, 'en-US')
    const ja = await render(SceneFrame, OPENING, 'ja-JP')

    expect(visibleText(en)).toContain(enUS.chat.storyScene.label)
    expect(visibleText(en)).toContain(enUS.chat.storyScene.meta.location)
    expect(visibleText(ja)).toContain(jaJP.chat.storyScene.label)
    expect(visibleText(ja)).toContain(jaJP.chat.storyScene.meta.mood)
  })
})

describe('StorySceneControl', () => {
  it('offers the press, and the reason to press it', async () => {
    // "You do not have to think of a topic" is the whole pitch; burying the
    // button in the `⋯` menu would hide it from exactly the player it is for.
    const html = await render(StorySceneControl, { sceneOpen: false })

    expect(visibleText(html)).toContain(L.action)
    expect(visibleText(html)).toContain(L.actionHint)
    expect(html).not.toContain(L.end)
  })

  it('says the request is under way', async () => {
    const html = await render(StorySceneControl, {
      sceneOpen: false,
      opening: true,
    })

    expect(visibleText(html)).toContain(L.opening)
    expect(html).toContain('disabled')
    expect(html).toContain('aria-busy')
  })

  it('becomes the scene’s status line and its exit', async () => {
    const html = await render(StorySceneControl, {
      sceneOpen: true,
      sceneTitle: OPENING.title,
    })

    expect(visibleText(html)).toContain(L.inProgress)
    expect(visibleText(html)).toContain(OPENING.title)
    expect(visibleText(html)).toContain(L.end)
    // ...and there is no second way to open a scene on top of this one.
    expect(visibleText(html)).not.toContain(L.actionHint)
  })

  it('says the exit is under way', async () => {
    const html = await render(StorySceneControl, {
      sceneOpen: true,
      ending: true,
    })

    expect(visibleText(html)).toContain(L.ending)
  })

  it('cannot be pressed while a turn is in flight', async () => {
    const html = await render(StorySceneControl, {
      sceneOpen: false,
      disabled: true,
    })

    expect(html).toContain('disabled')
  })

  it('states a refusal in the player’s language, not the server’s', async () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const html = await render(
        StorySceneControl,
        {
          sceneOpen: false,
          errorMessage: catalog.chat.storyScene.errors.inProgress,
        },
        locale as Locale,
      )

      expect(visibleText(html), locale)
        .toContain(catalog.chat.storyScene.errors.inProgress)
      expect(html, locale).toContain('role="alert"')
    }
  })

  it('leaves a clean, empty mount point for the hosted price', async () => {
    // The control itself stays free of billing knowledge; the number is
    // slotted in from outside, and self-host slots in nothing.
    const html = await render(StorySceneControl, { sceneOpen: false })

    expect(html).toContain('story-scene-control__price')
    expect(visibleText(html)).not.toContain('螢火')
  })

  it('says how much of today’s allowance is left, before the press', async () => {
    const html = await render(StorySceneControl, {
      sceneOpen: false,
      quotaNote: L.quota.remaining.replace('{remaining}', '2'),
    })

    expect(visibleText(html)).toContain('2')
    expect(html).toContain('story-scene-control__quota')
    // Still an advisory: a note beside a button that can still be pressed.
    expect(html).not.toContain('disabled')
  })

  it('keeps the button pressable even when the allowance reads as spent', async () => {
    // A rolling 24h counter read once per session decays the moment it is
    // taken, and every refresh trigger lives behind this very button — a
    // disable would lock itself shut past the point the server would allow
    // the press. Exhaustion only warms the note; the server owns the "no".
    const html = await render(StorySceneControl, {
      sceneOpen: false,
      quotaNote: L.quota.exhausted,
      quotaExhausted: true,
    })

    expect(visibleText(html)).toContain(L.quota.exhausted)
    expect(html).not.toContain('disabled')
    expect(html).toContain('story-scene-control__quota--exhausted')
  })

  it('yields the allowance note to an error line', async () => {
    // After a 429 both would say "you are out for today" one above the
    // other; the error is the one that also promises nothing was charged.
    const html = await render(StorySceneControl, {
      sceneOpen: false,
      quotaNote: L.quota.exhausted,
      quotaExhausted: true,
      errorMessage: L.errors.dailyLimitReached,
    })

    expect(visibleText(html)).toContain(L.errors.dailyLimitReached)
    expect(visibleText(html)).not.toContain(L.quota.exhausted)
  })

  it('renders no allowance node when there is nothing certain to say', async () => {
    // Self-host, an uncapped plan, and limits we could not read all land
    // here — and all three must be indistinguishable from before SC3-B.
    const html = await render(StorySceneControl, {
      sceneOpen: false,
      quotaNote: null,
    })

    expect(html).not.toContain('story-scene-control__quota')
  })

  it('drops the allowance note once the curtain is up', async () => {
    // Inside a scene there is no button for it to qualify; leaving it
    // would be a running commentary on a press that is not on offer.
    const html = await render(StorySceneControl, {
      sceneOpen: true,
      sceneTitle: OPENING.title,
      quotaNote: L.quota.exhausted,
      quotaExhausted: true,
    })

    expect(visibleText(html)).not.toContain(L.quota.exhausted)
    // ...and the way out of the scene is never withheld.
    expect(visibleText(html)).toContain(L.end)
  })

  it('speaks all three languages', async () => {
    const en = await render(StorySceneControl, { sceneOpen: false }, 'en-US')
    const ja = await render(StorySceneControl, { sceneOpen: false }, 'ja-JP')
    const enOpen = await render(
      StorySceneControl,
      { sceneOpen: true, sceneTitle: 'A rooftop solo' },
      'en-US',
    )
    const jaOpen = await render(
      StorySceneControl,
      { sceneOpen: true, sceneTitle: 'A rooftop solo' },
      'ja-JP',
    )

    expect(visibleText(en)).toContain('Start a Scene')
    expect(visibleText(ja)).toContain('幕開け')
    expect(visibleText(enOpen)).toContain(enUS.chat.storyScene.end)
    expect(visibleText(jaOpen)).toContain(jaJP.chat.storyScene.end)
  })
})

describe('the hosted price on the button (SC3-C)', () => {
  it('states what the press costs, before it is pressed', async () => {
    await seedScenePrice(6)

    const html = await render(PricedControl)

    expect(html).toContain('story-scene-control__price')
    expect(visibleText(html)).toContain('6')
    expect(visibleText(html)).toContain('螢火')
    // The unit is the scene, not the message: this is one price for
    // raising the curtain.
    expect(visibleText(html)).toContain(zhTW.credits.price.perScene.split(' ')[0])
    // ...and the disclosure says the turns inside are charged separately,
    // so the number beside the button is never read as "the whole scene".
    expect(html).toContain(zhTW.credits.price.storySceneTooltip)
  })

  it('renders no price node at all on self-host', async () => {
    // Not a hidden element, not a dash — nothing. Self-host has no wallet
    // and must not carry the shape of one.
    await seedScenePrice(6)
    setDeploymentMode('self_host')

    const html = await render(PricedControl)

    expect(visibleText(html)).not.toContain('螢火')
    expect(html).not.toContain('action-price')
    // the button and its promise are untouched
    expect(visibleText(html)).toContain(L.action)
    expect(visibleText(html)).toContain(L.actionHint)
  })

  it('renders no price when no price can be stated honestly', async () => {
    // Hosted, but the back office has not set a 起幕 price yet (the plan's
    // zero-seed rule). A guessed number would be a promise the ledger
    // breaks, so the chip simply is not there.
    await seedScenePrice(6)
    useActionPricing().reset()

    const html = await render(PricedControl)

    expect(html).not.toContain('action-price')
    expect(visibleText(html)).toContain(L.action)
  })

  it('speaks all three languages', async () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      useActionPricing().reset()
      await seedScenePrice(6)

      const html = await render(PricedControl, {}, locale as Locale)

      expect(visibleText(html), locale).toContain('6')
      expect(html, locale).toContain(catalog.credits.price.storySceneTooltip)
    }
  })

  it('discloses the money without operator jargon, in any language', async () => {
    for (const locale of Object.keys(CATALOGS) as Locale[]) {
      useActionPricing().reset()
      await seedScenePrice(6)

      const html = await render(PricedControl, {}, locale)

      expect(findJargon(visibleText(html)), locale).toEqual([])
    }
  })

  it('keeps the money refusal copy jargon-free too', () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const copy = [
        catalog.credits.price.storySceneTooltip,
        catalog.credits.price.perScene,
        catalog.chat.storyScene.errors.priceChanged,
      ]
      for (const value of copy) {
        expect(findJargon(value), `${locale}: ${value}`).toEqual([])
      }
    }
  })
})

describe('StorySceneChips', () => {
  const actions = [
    { text: '在她旁邊坐下' },
    { text: '什麼都不說' },
  ]

  it('offers a starting line, and says it is only that', async () => {
    const html = await render(StorySceneChips, { actions })

    expect(visibleText(html)).toContain(actions[0].text)
    expect(visibleText(html)).toContain(actions[1].text)
    // The chips must never read as the only moves available.
    expect(visibleText(html)).toContain(L.chipsHint)
    expect(html).toContain(`aria-label="${L.chipsAria}"`)
  })

  it('renders nothing when the turn produced none', async () => {
    // Chips fail soft on the backend; an empty strip above the composer
    // would be a permanent reminder of a feature that did not run.
    const html = await render(StorySceneChips, { actions: [] })

    expect(html).not.toContain('scene-chips')
  })

  it('cannot be pressed while the turn they belong to is being replaced', async () => {
    const html = await render(StorySceneChips, { actions, disabled: true })

    expect(html).toContain('disabled')
  })

  it('speaks all three languages', async () => {
    const en = await render(StorySceneChips, { actions }, 'en-US')
    const ja = await render(StorySceneChips, { actions }, 'ja-JP')

    expect(visibleText(en)).toContain(enUS.chat.storyScene.chipsHint)
    expect(visibleText(ja)).toContain(jaJP.chat.storyScene.chipsHint)
  })
})

describe('self-host parity', () => {
  it('is byte-identical in both modes', async () => {
    // The plan's line: self-host is complete the moment SC2 lands. Nothing
    // on this surface may depend on the deployment.
    const surfaces: Array<[string, unknown, Record<string, unknown>]> = [
      ['SceneFrame', SceneFrame, OPENING],
      ['StorySceneControl', StorySceneControl, { sceneOpen: false }],
      ['StorySceneControlOpen', StorySceneControl, {
        sceneOpen: true, sceneTitle: OPENING.title,
      }],
      ['StorySceneChips', StorySceneChips, {
        actions: [{ text: '在她旁邊坐下' }],
      }],
    ]

    for (const [name, component, props] of surfaces) {
      setDeploymentMode('cloud')
      const hosted = await render(component, props)
      setDeploymentMode('self_host')
      const selfHost = await render(component, props)

      expect(selfHost, name).toEqual(hosted)
    }
  })
})

describe('hosted jargon sweep', () => {
  it('no scene surface uses a denied term, in any language', async () => {
    for (const locale of Object.keys(CATALOGS) as Locale[]) {
      const rendered = [
        await render(SceneFrame, OPENING, locale),
        await render(SceneFrame, {
          text: 'The roof empties out.', closing: true,
        }, locale),
        await render(StorySceneControl, { sceneOpen: false }, locale),
        await render(StorySceneControl, {
          sceneOpen: true, sceneTitle: OPENING.title,
        }, locale),
        await render(StorySceneChips, {
          actions: [{ text: 'Sit beside her' }],
        }, locale),
      ]
      for (const html of rendered) {
        expect(findJargon(visibleText(html)), locale).toEqual([])
      }
    }
  })

  it('covers the copy no render reaches', async () => {
    // The confirmation modal and every refusal branch are player-facing copy
    // behind a click or a failure, so the catalogue is scanned directly.
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const scene = catalog.chat.storyScene
      const values = [
        scene.endConfirmTitle,
        scene.endConfirm,
        scene.endConfirmAction,
        ...Object.values(scene.errors),
        // The allowance notes are hosted-only copy by construction, so
        // they are exactly where operator vocabulary would leak first.
        ...Object.values(scene.quota),
      ]
      for (const value of values) {
        expect(findJargon(value), `${locale}: ${value}`).toEqual([])
      }
    }
  })
})
