import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h, ref, type Ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'
import { createMemoryHistory, createRouter } from 'vue-router'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import {
  STUDIO_EXIT_HUB_COACHMARK_KEY,
  STUDIO_GUIDE_COACHMARK_KEY,
  ARC_DISCOVERY_STUDIO_COACHMARK_KEY,
  isStudioGuideCoachmarkDismissed,
  rememberStudioGuideCoachmarkDismissed,
} from '@/utils/arcDiscovery'

/**
 * BD12 — the studio's newcomer explanation.
 *
 * Two things are pinned here: the one-shot coachmark's storage logic
 * (user-wide, fails soft, does not collide with the two coachmarks that
 * already live in the same module), and the guide page's existence —
 * every section it promises actually renders, in both billing modes.
 *
 * No DOM test infra exists in this repo (@vue/test-utils / jsdom are not
 * installed), so component coverage is via SSR, the same pattern as
 * fusionStoryExitHub.test.ts.
 */

const holder = vi.hoisted(() => ({
  cloudMode: null as Ref<boolean> | null,
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({ cloudMode: holder.cloudMode }),
}))

const cloudMode = ref(true)
holder.cloudMode = cloudMode

const StudioGuideModal
  = (await import('@/components/studio/StudioGuideModal.vue')).default
const StudioTabCard
  = (await import('@/components/studio/StudioTabCard.vue')).default

interface ChainStepCopy {
  title: string
  body: string
  forks?: Record<string, string>
}

const L = (zhTW as unknown as {
  studio: {
    tabs: Record<string, string>
    guide: {
      title: string
      intro: string
      featuresTitle: string
      close: string
      features: Record<string, string>
      chain: {
        title: string
        lead: string
        steps: Record<string, ChainStepCopy>
      }
      credits: Record<string, string>
    }
  }
}).studio

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  })
}

/** Stub routes so `<RouterLink :to="{ name }">` resolves during SSR. */
const Blank = defineComponent({ render: () => h('div') })

function router() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/studio', name: 'studio-authoring', component: Blank },
      { path: '/studio/fusion', name: 'studio-fusion-stories', component: Blank },
      { path: '/studio/drama', name: 'studio-branching-dramas', component: Blank },
      { path: '/studio/cards', name: 'studio-character-cards', component: Blank },
    ],
  })
}

async function render(
  component: unknown,
  props: Record<string, unknown> = {},
): Promise<string> {
  const app = createSSRApp(
    component as Parameters<typeof createSSRApp>[0],
    props,
  )
  app.use(i18n())
  const r = router()
  app.use(r)
  await r.push('/studio')
  await r.isReady()
  return renderToString(app)
}

function fakeStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  } satisfies Pick<Storage, 'getItem' | 'setItem'>
}

const throwingStorage = {
  getItem: () => {
    throw new Error('blocked')
  },
  setItem: () => {
    throw new Error('blocked')
  },
} satisfies Pick<Storage, 'getItem' | 'setItem'>

beforeEach(() => {
  cloudMode.value = true
})

afterEach(() => {
  delete (globalThis as { window?: unknown }).window
})

describe('studio guide coachmark dismissal storage', () => {
  it('flips the predicate once dismissed, user-wide', () => {
    const storage = fakeStorage()

    expect(isStudioGuideCoachmarkDismissed(storage)).toBe(false)
    expect(rememberStudioGuideCoachmarkDismissed(storage)).toBe(true)
    expect(isStudioGuideCoachmarkDismissed(storage)).toBe(true)
    expect(storage.getItem(STUDIO_GUIDE_COACHMARK_KEY)).toBe('1')
  })

  it('uses a key distinct from the other two studio coachmarks', () => {
    // A shared key would make reading the exit hub's hint silently
    // suppress this one (and vice versa) — different lessons, different
    // one-shots.
    expect(STUDIO_GUIDE_COACHMARK_KEY).not.toBe(STUDIO_EXIT_HUB_COACHMARK_KEY)
    expect(STUDIO_GUIDE_COACHMARK_KEY).not.toBe(
      ARC_DISCOVERY_STUDIO_COACHMARK_KEY,
    )
  })

  it('treats any value other than the dismissal marker as "not dismissed"', () => {
    const storage = fakeStorage()
    storage.setItem(STUDIO_GUIDE_COACHMARK_KEY, '0')

    expect(isStudioGuideCoachmarkDismissed(storage)).toBe(false)
  })

  it('fails soft when storage is unavailable or throws', () => {
    // Privacy mode / SSR: the coachmark should show rather than crash,
    // and a failed write must report itself instead of pretending.
    expect(isStudioGuideCoachmarkDismissed(null)).toBe(false)
    expect(rememberStudioGuideCoachmarkDismissed(null)).toBe(false)
    expect(isStudioGuideCoachmarkDismissed(throwingStorage)).toBe(false)
    expect(rememberStudioGuideCoachmarkDismissed(throwingStorage)).toBe(false)
  })
})

describe('StudioGuideModal (SSR render)', () => {
  it('renders nothing while hidden', async () => {
    const html = await render(StudioGuideModal, { visible: false })

    expect(html).not.toContain(L.guide.intro)
  })

  it('renders the four-feature section', async () => {
    const html = await render(StudioGuideModal, { visible: true })

    expect(html).toContain(L.guide.title)
    expect(html).toContain(L.guide.intro)
    expect(html).toContain(L.guide.featuresTitle)
    for (const key of ['authoring', 'fusion', 'branching', 'cards'] as const) {
      expect(html).toContain(L.tabs[key])
      expect(html).toContain(L.guide.features[key])
    }
  })

  it('describes the drama tree as grown layer by layer, not built up front', async () => {
    // `BranchingDramaService` generates root + first children at create time
    // and the deeper layers lazily as the player advances (the advance price
    // covers the prefetch — see `credits.price.dramaAdvanceTooltip`). Copy
    // promising a finished tree told players to expect a wait that never
    // happens and a completeness the backend never delivers.
    const branching = L.guide.features.branching
    const how = L.tabs.branchingHow

    for (const copy of [branching, how]) {
      expect(copy).not.toContain('整棵')
      expect(copy).not.toContain('分支樹會先生成好')
    }
    // What the player actually gets: an opening now, one more layer per press.
    expect(branching).toContain('開場先備好')
    expect(branching).toContain('多長一層')
    expect(how).toContain('多長一層')
    // …and the advance price already covers that growth.
    expect(how).toContain('推進的價格已經含在裡面')
  })

  it('renders every link of the conversion chain, forks included', async () => {
    // The chain is the whole reason this page exists — a step silently
    // missing (a stale i18n key, a typo in the step list) would leave a
    // gap exactly where the product is least obvious.
    const html = await render(StudioGuideModal, { visible: true })

    expect(html).toContain(L.guide.chain.title)
    expect(html).toContain(L.guide.chain.lead)

    const steps = L.guide.chain.steps
    const stepKeys = [
      'chat',
      'fusion',
      'fusionExits',
      'arc',
      'drama',
      'dramaExits',
      'card',
    ] as const
    expect(Object.keys(steps).sort()).toEqual([...stepKeys].sort())

    for (const key of stepKeys) {
      expect(html).toContain(steps[key].title)
      expect(html).toContain(steps[key].body)
      for (const [id, fork] of Object.entries(steps[key].forks ?? {})) {
        // The `*SelfHost` twins are the other billing mode's copy; only one
        // of each pair is ever on screen at a time.
        if (id.endsWith('SelfHost')) continue
        expect(html).toContain(fork)
      }
    }
    // The two junction steps are the ones that carry fork lists; if the
    // component ever stopped iterating them the loop above would pass
    // vacuously, so the counts are pinned here too. `dramaExits` also
    // carries the self-host twin of its gallery line.
    expect(Object.keys(steps.fusionExits.forks ?? {})).toHaveLength(4)
    expect(Object.keys(steps.dramaExits.forks ?? {}).sort()).toEqual([
      'adapt', 'gallery', 'gallerySelfHost', 'replay', 'transcript',
    ])
  })

  it('names the gallery as an exit the ending screen actually carries', async () => {
    // `DramaExitHub` now has replay / adapt / export transcript / gallery /
    // back-to-list, so the fork is described from where the player is when
    // they read about it. The copy was previously worded around the button
    // NOT being there ("go back to the drama's page"), which is now the
    // implementation detail rather than the instruction.
    const gallery = L.guide.chain.steps.dramaExits.forks?.gallery ?? ''

    expect(gallery).toContain('結局畫面')
    expect(gallery).toContain('圖集')
  })

  it('states the hosted charging points without naming a number', async () => {
    const html = await render(StudioGuideModal, { visible: true })

    expect(html).toContain(L.guide.credits.title)
    expect(html).toContain(L.guide.credits.charged)
    expect(html).toContain(L.guide.credits.priced)
    expect(html).toContain(L.guide.credits.free)
    expect(html).not.toContain(L.guide.credits.selfHost)
    // Prices belong next to the button they apply to, where they are read
    // from the live table — a number frozen into guide copy is a promise
    // the ledger can break.
    expect(L.guide.credits.charged).not.toMatch(/\d/)
    expect(L.guide.credits.priced).not.toMatch(/\d/)
    expect(L.guide.credits.free).not.toMatch(/\d/)
  })

  it('names chat itself as a charged action', async () => {
    // The conversion chain opens on everyday chat, and `chat` is a billed
    // action on hosted. Listing only the studio buttons let a player read
    // the first link of the chain as free.
    expect(L.guide.credits.charged).toContain('聊天')
  })

  it('does not promise a price tag on every button', async () => {
    // `ActionPriceHint` renders nothing when the price table is missing, a
    // tier bills token-floating, or two tiers disagree — so "the price is
    // next to the button" cannot be stated unconditionally. The copy has to
    // say what happens when no price is shown.
    expect(L.guide.credits.priced).toContain('沒有標出價格')
    expect(L.guide.credits.priced).toContain('方案')
  })

  it('replaces the whole credits block with one free line on self-host', async () => {
    cloudMode.value = false

    const html = await render(StudioGuideModal, { visible: true })

    expect(html).toContain(L.guide.credits.selfHost)
    expect(html).not.toContain(L.guide.credits.charged)
    expect(html).not.toContain(L.guide.credits.free)
  })

  it('does not tell a self-host player that they run the site', async () => {
    // Most players on a self-hosted instance are guests of whoever runs it;
    // second-person "you set this up" is simply false for them. The reason
    // credits are free is the missing cloud billing, not the reader.
    expect(L.guide.credits.selfHost).toContain('沒有接雲端計費')
    expect(L.guide.credits.selfHost).not.toContain('自行架設')
  })

  it('makes the scene-art promise conditional off cloud', async () => {
    // Scene images need an image backend the SPA cannot see; a self-hosted
    // site without one never draws a single picture, so an unconditional
    // "your scenes get illustrated" is a promise the deployment may break.
    const cloudHtml = await render(StudioGuideModal, { visible: true })
    expect(cloudHtml).toContain(L.guide.features.branching)
    expect(cloudHtml).not.toContain(L.guide.features.branchingSelfHost)

    cloudMode.value = false
    const selfHostHtml = await render(StudioGuideModal, { visible: true })

    expect(selfHostHtml).toContain(L.guide.features.branchingSelfHost)
    expect(selfHostHtml).not.toContain(L.guide.features.branching)
    expect(selfHostHtml).toContain(
      L.guide.chain.steps.dramaExits.forks?.gallerySelfHost ?? '',
    )
    expect(L.guide.features.branchingSelfHost).toContain('若這個站有啟用場景圖')
  })
})

describe('StudioTabCard (SSR render)', () => {
  const props = {
    routeName: 'studio-fusion-stories',
    label: L.tabs.fusion,
    description: L.tabs.fusionHint,
    icon: '◇',
    accent: 'var(--color-primary-light)',
    what: L.tabs.fusionWhat,
    how: L.tabs.fusionHow,
    next: L.tabs.fusionNext,
    active: false,
  }

  it('keeps the scannable one-liner and adds the three-part explanation', async () => {
    const html = await render(StudioTabCard, props)

    expect(html).toContain(L.tabs.fusion)
    expect(html).toContain(L.tabs.fusionHint)
    expect(html).toContain(L.tabs.detailsWhat)
    expect(html).toContain(L.tabs.fusionWhat)
    expect(html).toContain(L.tabs.detailsHow)
    expect(html).toContain(L.tabs.fusionHow)
    expect(html).toContain(L.tabs.detailsNext)
    expect(html).toContain(L.tabs.fusionNext)
  })

  it('puts the expander outside the link, as a native <details>', async () => {
    // A toggle nested inside the <a> would be invalid HTML and would
    // hijack the card's own activation; a hover-only reveal would be
    // unreachable on touch. Both regressions show up here.
    const html = await render(StudioTabCard, props)

    expect(html).toContain('<details')
    expect(html).toContain('<summary')
    expect(html).toContain(L.tabs.detailsLabel)
    const linkEnd = html.indexOf('</a>')
    expect(linkEnd).toBeGreaterThan(-1)
    expect(html.indexOf('<details')).toBeGreaterThan(linkEnd)
  })
})

describe('ja-JP guide vocabulary', () => {
  /** Every guide/tab string, flattened, so nothing escapes the sweep. */
  async function jaGuideStrings(): Promise<string[]> {
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const flatten = (value: unknown): string[] =>
      typeof value === 'string'
        ? [value]
        : Object.values(value as Record<string, unknown>).flatMap(flatten)
    return [
      ...flatten(jaJP.studio.guide),
      ...flatten(jaJP.studio.tabs),
    ]
  }

  it('reuses the shipped nouns instead of inventing a fourth set', async () => {
    // The ja UI already names these things: 共演ストーリー (studio.tabs.fusion),
    // 分岐ドラマ (studio.tabs.branching), 脚本 (40× across the catalogue) and
    // 逐語録 (branchingDrama.exitHub.exportLabel). A guide that calls the same
    // features 共演短編 / 分岐劇場 / スクリプト / 台本 teaches a vocabulary the
    // rest of the product never uses, which is worse than no guide.
    const strings = await jaGuideStrings()
    for (const banned of ['共演短編', '分岐劇場', 'スクリプト', '台本']) {
      expect(strings.filter(s => s.includes(banned))).toEqual([])
    }
  })

  it('names the authoring button exactly as the button is labelled', async () => {
    // `studio.actions.newTemplate` is what the player sees; quoting a
    // different string sends them looking for a button that does not exist.
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const { messages: enUS } = await import('@/i18n/locales/en-US')

    expect(jaJP.studio.tabs.authoringHow)
      .toContain(jaJP.studio.actions.newTemplate)
    expect(zhTW.studio.tabs.authoringHow)
      .toContain(zhTW.studio.actions.newTemplate)
    expect(enUS.studio.tabs.authoringHow)
      .toContain(enUS.studio.actions.newTemplate)
  })
})

describe('studio guide catalogue', () => {
  it('carries every new key in all three locales', async () => {
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')

    function keysOf(value: unknown, prefix = ''): string[] {
      if (!value || typeof value !== 'object') return [prefix]
      return Object.entries(value as Record<string, unknown>).flatMap(
        ([key, child]) => keysOf(child, prefix ? `${prefix}.${key}` : key),
      )
    }

    const zhKeys = keysOf(zhTW.studio.guide).sort()
    expect(keysOf(enUS.studio.guide).sort()).toEqual(zhKeys)
    expect(keysOf(jaJP.studio.guide).sort()).toEqual(zhKeys)
  })
})
