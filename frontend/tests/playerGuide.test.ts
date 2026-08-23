import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, ref, type Ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import {
  PLAYER_GUIDE_CHAPTERS,
  PLAYER_GUIDE_COACHMARK_KEY,
  isPlayerGuideCoachmarkDismissed,
  rememberPlayerGuideCoachmarkDismissed,
  visiblePlayerGuideChapters,
} from '@/utils/playerGuide'
import {
  ARC_DISCOVERY_STUDIO_COACHMARK_KEY,
  STUDIO_EXIT_HUB_COACHMARK_KEY,
  STUDIO_GUIDE_COACHMARK_KEY,
} from '@/utils/arcDiscovery'

/**
 * PG1 — the player-side "how to play" overview.
 *
 * Three things are pinned here: the chapter skeleton matches its i18n keys
 * one-for-one (a stale key or a renamed chapter would otherwise surface as
 * a raw key in front of a player), the chapter- and sentence-level
 * conditionalisation actually swaps content between billing modes, and the
 * one-shot coachmark's storage logic.
 *
 * No DOM test infra exists in this repo (@vue/test-utils / jsdom are not
 * installed), so component coverage is via SSR, the same pattern as
 * studioGuide.test.ts.
 */

const holder = vi.hoisted(() => ({
  cloudMode: null as Ref<boolean> | null,
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({ cloudMode: holder.cloudMode }),
}))

const cloudMode = ref(true)
holder.cloudMode = cloudMode

const PlayerGuideModal
  = (await import('@/components/playerGuide/PlayerGuideModal.vue')).default

interface ChapterCopy {
  title: string
  lead?: string
  items?: Record<string, string>
}

const L = (zhTW as unknown as {
  playerGuide: {
    title: string
    intro: string
    close: string
    entry: Record<string, string>
    coachmark: Record<string, string>
    chapters: Record<string, ChapterCopy>
  }
}).playerGuide

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  })
}

async function render(props: Record<string, unknown>): Promise<string> {
  const app = createSSRApp(
    PlayerGuideModal as Parameters<typeof createSSRApp>[0],
    props,
  )
  app.use(i18n())
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

describe('player guide chapter skeleton', () => {
  it('pins the chapter order and which ones carry content', () => {
    // The order is product-decided (plan PG §3) and must not drift with a
    // reorder of the i18n object; `ready` is what keeps an unwritten
    // chapter out of the player's face instead of a placeholder string.
    expect(PLAYER_GUIDE_CHAPTERS.map(c => c.key)).toEqual([
      'basics',
      'story',
      'life',
      'lumegram',
      // LB8: enlarging a picture is one interaction shared by six panels, so
      // it is one chapter rather than the same three mechanics repeated in
      // four. It sits after the feed because chat and LumeGram are where a
      // player meets a picture first.
      'images',
      'memory',
      'studio',
      'channels',
      'portability',
      'credits',
    ])
    // PG2 filled in chapters 5–9, so every chapter now carries content.
    expect(
      PLAYER_GUIDE_CHAPTERS.filter(c => c.ready).map(c => c.key),
    ).toEqual(PLAYER_GUIDE_CHAPTERS.map(c => c.key))
  })

  it('maps every chapter key to a catalogue entry, and nothing else', () => {
    expect(Object.keys(L.chapters).sort())
      .toEqual(PLAYER_GUIDE_CHAPTERS.map(c => c.key).sort())
  })

  it('maps every item id to a catalogue string, and nothing else', () => {
    // A key in the constant with no string renders the raw key path; a
    // string with no key in the constant is copy nobody will ever read.
    for (const chapter of PLAYER_GUIDE_CHAPTERS) {
      if (!chapter.ready) continue
      const copy = L.chapters[chapter.key]
      expect(copy.lead, `${chapter.key} has no lead`).toBeTruthy()
      const declared = chapter.items.map(item => item.id).sort()
      const written = Object.keys(copy.items ?? {})
        .filter(id => !id.endsWith('SelfHost'))
        .sort()
      expect(written, `${chapter.key} item keys`).toEqual(declared)
    }
  })

  it('gives every site-aware item its self-host twin in all three locales', async () => {
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const catalogues = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP }

    const siteAware = PLAYER_GUIDE_CHAPTERS.flatMap(chapter =>
      chapter.items.filter(item => item.siteAware)
        .map(item => [chapter.key, item.id] as const),
    )
    expect(siteAware.length).toBeGreaterThan(0)

    for (const [locale, catalogue] of Object.entries(catalogues)) {
      const chapters = (catalogue as unknown as {
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }).playerGuide.chapters
      for (const [chapterKey, itemId] of siteAware) {
        const items = chapters[chapterKey].items ?? {}
        expect(
          items[`${itemId}SelfHost`],
          `${locale} ${chapterKey}.${itemId} has no self-host twin`,
        ).toBeTruthy()
        // Identical twins mean the conditional is decorative — the whole
        // point is that one of them drops a promise the other site cannot
        // keep.
        expect(items[`${itemId}SelfHost`]).not.toBe(items[itemId])
      }
    }
  })

  it('leaves no chapter as a bare title', () => {
    // The `ready` flag keeps an unwritten chapter out of a player's face;
    // nothing written may be left half-written either — a chapter with a
    // title and one line is a heading, not a chapter. The studio chapter
    // is the deliberate exception (plan §3): one line plus a button that
    // opens the studio's own guide, so its content lives in exactly one
    // place.
    for (const chapter of PLAYER_GUIDE_CHAPTERS) {
      expect(chapter.ready, `${chapter.key} is not ready`).toBe(true)
      const floor = chapter.opensStudioGuide ? 0 : 1
      expect(chapter.items.length, `${chapter.key} is too thin`)
        .toBeGreaterThan(floor)
    }
  })

  it('sends the studio chapter to the existing studio guide, not a copy', () => {
    // The studio's own guide is the single place that copy is maintained;
    // this chapter only opens it. A second chapter growing the flag would
    // mean two buttons claiming to open the same page.
    expect(
      PLAYER_GUIDE_CHAPTERS.filter(c => c.opensStudioGuide).map(c => c.key),
    ).toEqual(['studio'])
    // …and it must stay a pointer: a second line here is where a rewritten
    // copy of the studio guide starts growing.
    const studio = PLAYER_GUIDE_CHAPTERS.find(c => c.key === 'studio')
    expect(studio?.items.map(i => i.id)).toEqual(['entry'])
  })

  it('hides hosted-only chapters entirely off cloud', () => {
    // Chapter-level conditionalisation is a hard gate, not a softened
    // sentence: a chapter about an ability this deployment does not have
    // teaches the player something that is not there.
    const hostedOnly = PLAYER_GUIDE_CHAPTERS.filter(c => c.hostedOnly)
    expect(hostedOnly.map(c => c.key)).toEqual(['credits'])

    const cloudKeys = visiblePlayerGuideChapters(true).map(c => c.key)
    const selfHostKeys = visiblePlayerGuideChapters(false).map(c => c.key)
    for (const chapter of hostedOnly) {
      expect(selfHostKeys).not.toContain(chapter.key)
      // …and once it is written, cloud must actually get it.
      if (chapter.ready) expect(cloudKeys).toContain(chapter.key)
    }
  })
})

describe('PlayerGuideModal (SSR render)', () => {
  it('renders nothing while hidden', async () => {
    const html = await render({ visible: false })

    expect(html).not.toContain(L.intro)
  })

  it('renders every written chapter, lead and line', async () => {
    const html = await render({ visible: true })

    expect(html).toContain(L.title)
    expect(html).toContain(L.intro)
    expect(html).toContain(L.close)

    for (const chapter of PLAYER_GUIDE_CHAPTERS) {
      if (!chapter.ready) continue
      const copy = L.chapters[chapter.key]
      expect(html).toContain(copy.title)
      expect(html).toContain(copy.lead ?? '')
      for (const item of chapter.items) {
        // Rendered on cloud, so the self-host-only lines are absent by
        // design — their own test below covers both directions.
        if (item.selfHostOnly) continue
        expect(html).toContain((copy.items ?? {})[item.id])
      }
    }
  })

  it('keeps the hosted-only chapter out of a self-host guide entirely', async () => {
    // Not softened, not greyed out: off cloud the lumes chapter has no
    // section and no entry in the table of contents, because none of the
    // buttons it describes exist on that deployment.
    const credits = L.chapters.credits
    const cloudHtml = await render({ visible: true })
    expect(cloudHtml).toContain(credits.title)
    expect(cloudHtml).toContain(credits.lead ?? '')
    for (const line of Object.values(credits.items ?? {})) {
      expect(cloudHtml).toContain(line)
    }

    cloudMode.value = false
    const selfHostHtml = await render({ visible: true })

    expect(selfHostHtml).not.toContain(credits.title)
    expect(selfHostHtml).not.toContain(credits.lead ?? '')
    for (const line of Object.values(credits.items ?? {})) {
      expect(selfHostHtml).not.toContain(line)
    }
  })

  it('offers the studio guide from the studio chapter', async () => {
    const html = await render({ visible: true })

    // The label is borrowed from the studio guide's own entry point so the
    // two buttons that open that page cannot drift apart.
    const studio = (zhTW as unknown as {
      studio: { guide: { openLabel: string } }
    }).studio.guide.openLabel
    expect(html).toContain(studio)
  })

  it('teaches the rename-in-chat trick, the most hidden one', async () => {
    // The whole reason this guide exists: a player who never reads the
    // settings page has no other way to discover that saying it in chat
    // works. If this line ever goes missing the chapter loses its point.
    const line = (L.chapters.memory.items ?? {}).callMeThat
    expect(line).toContain('叫我')
  })

  it('states the backup password has no recovery, in every locale', async () => {
    // A red line that only holds in zh-TW is not a red line: a player who
    // reads the en or ja guide, loses the password and expects support to
    // help has been told something false by omission.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const expected = {
      'zh-TW': [zhTW, '永久無法解開', '無法為你救援'],
      'en-US': [enUS, 'never be opened again', 'cannot recover it'],
      'ja-JP': [jaJP, '二度と復号できなく', '復旧できません'],
    } as const

    for (const [locale, [catalogue, ...phrases]] of Object.entries(expected)) {
      const line = (catalogue as unknown as {
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }).playerGuide.chapters.portability.items?.password ?? ''
      for (const phrase of phrases) {
        expect(line, `${locale} password line`).toContain(phrase)
      }
    }
  })

  it('swaps the site-aware lines instead of showing both', async () => {
    const cloudHtml = await render({ visible: true })
    const scene = L.chapters.story.items ?? {}
    expect(cloudHtml).toContain(scene.scenePrice)
    expect(cloudHtml).not.toContain(scene.scenePriceSelfHost)

    cloudMode.value = false
    const selfHostHtml = await render({ visible: true })

    expect(selfHostHtml).toContain(scene.scenePriceSelfHost)
    expect(selfHostHtml).not.toContain(scene.scenePrice)
  })

  it('drops the self-host-only lines on cloud instead of softening them', async () => {
    // Sentence-level hard gate (FX1): the NSFW mode switch is rendered
    // only off cloud (`PersonalSettingsSection` hides the whole section),
    // so on cloud the sentence must not exist at all — there is no cloud
    // wording for it and no `SelfHost` twin to fall back to. This flag is
    // orthogonal to `siteAware`: that one swaps a sentence, this one
    // removes it.
    const selfHostOnly = PLAYER_GUIDE_CHAPTERS.flatMap(chapter =>
      chapter.items.filter(item => item.selfHostOnly)
        .map(item => [chapter.key, item.id] as const),
    )
    expect(selfHostOnly).toEqual([['basics', 'nsfw']])
    for (const [, id] of selfHostOnly) {
      // A twin would be dead copy: the cloud branch never renders.
      expect((L.chapters.basics.items ?? {})[`${id}SelfHost`]).toBeUndefined()
    }

    const cloudHtml = await render({ visible: true })
    const nsfw = (L.chapters.basics.items ?? {}).nsfw
    expect(nsfw).toBeTruthy()
    expect(cloudHtml).not.toContain(nsfw)

    cloudMode.value = false
    const selfHostHtml = await render({ visible: true })

    expect(selfHostHtml).toContain(nsfw)
  })

  it('never freezes a price number into the copy, in any locale', async () => {
    // Prices belong next to the button they apply to, where they are read
    // from the live table — a number frozen into guide copy is a promise
    // the ledger can break. Digits that are part of a feature's own shape
    // (three ways a scene can end) are fine; a credit amount is not.
    //
    // FX4: a red line held only in zh-TW is not a red line — each locale
    // gets the currency words it actually uses.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const digits = '[0-9０-９]'
    const priced = {
      'zh-TW': new RegExp(`${digits}+\\s*(螢火|點)`),
      'en-US': new RegExp(`${digits}+\\s*(Lumes?|credits?)\\b`, 'i'),
      'ja-JP': new RegExp(`${digits}+\\s*蛍火`),
    } as const

    for (const [locale, catalogue] of Object.entries({
      'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP,
    })) {
      const chapters = (catalogue as unknown as {
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }).playerGuide.chapters
      const pattern = priced[locale as keyof typeof priced]
      for (const chapter of PLAYER_GUIDE_CHAPTERS) {
        const items = chapters[chapter.key].items ?? {}
        for (const [id, copy] of Object.entries(items)) {
          expect(copy, `${locale} ${chapter.key}.${id} names a credit amount`)
            .not.toMatch(pattern)
        }
      }
    }
  })

  it('teaches the single-asterisk convention, not the bold one', async () => {
    // `ChatBubble.splitActionSegments` and the prompt convention in
    // `infrastructure/prompt/default.py` both match `*...*`. Teaching
    // `**...**` would render as literal asterisks and never split.
    const action = (L.chapters.basics.items ?? {}).action
    expect(action).toContain('*倒了杯茶*')
    expect(action).not.toContain('**')
  })

  it('says the assist and scene chips only fill the input box', async () => {
    // Both surfaces hand the text to the input without sending it; copy
    // that implied auto-send would make players stop using them.
    expect((L.chapters.basics.items ?? {}).assist).toContain('不會自動送出')
    expect((L.chapters.story.items ?? {}).sceneChips).toContain('不會自動送出')
  })
})

describe('player guide coachmark dismissal storage', () => {
  it('flips the predicate once dismissed, user-wide', () => {
    const storage = fakeStorage()

    expect(isPlayerGuideCoachmarkDismissed(storage)).toBe(false)
    expect(rememberPlayerGuideCoachmarkDismissed(storage)).toBe(true)
    expect(isPlayerGuideCoachmarkDismissed(storage)).toBe(true)
    expect(storage.getItem(PLAYER_GUIDE_COACHMARK_KEY)).toBe('1')
  })

  it('uses a key distinct from the studio coachmarks', () => {
    // A shared key would make reading the studio guide silently suppress
    // this one (and vice versa) — different lessons, different one-shots.
    for (const other of [
      STUDIO_GUIDE_COACHMARK_KEY,
      STUDIO_EXIT_HUB_COACHMARK_KEY,
      ARC_DISCOVERY_STUDIO_COACHMARK_KEY,
    ]) {
      expect(PLAYER_GUIDE_COACHMARK_KEY).not.toBe(other)
    }
  })

  it('treats any value other than the dismissal marker as "not dismissed"', () => {
    const storage = fakeStorage()
    storage.setItem(PLAYER_GUIDE_COACHMARK_KEY, '0')

    expect(isPlayerGuideCoachmarkDismissed(storage)).toBe(false)
  })

  it('fails soft when storage is unavailable or throws', () => {
    // Privacy mode / SSR: the coachmark should show rather than crash,
    // and a failed write must report itself instead of pretending.
    expect(isPlayerGuideCoachmarkDismissed(null)).toBe(false)
    expect(rememberPlayerGuideCoachmarkDismissed(null)).toBe(false)
    expect(isPlayerGuideCoachmarkDismissed(throwingStorage)).toBe(false)
    expect(rememberPlayerGuideCoachmarkDismissed(throwingStorage)).toBe(false)
  })
})

describe('player guide catalogue', () => {
  it('carries every new key in all three locales', async () => {
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')

    function keysOf(value: unknown, prefix = ''): string[] {
      if (!value || typeof value !== 'object') return [prefix]
      return Object.entries(value as Record<string, unknown>).flatMap(
        ([key, child]) => keysOf(child, prefix ? `${prefix}.${key}` : key),
      )
    }

    const zhKeys = keysOf(zhTW.playerGuide).sort()
    expect(keysOf(enUS.playerGuide).sort()).toEqual(zhKeys)
    expect(keysOf(jaJP.playerGuide).sort()).toEqual(zhKeys)
  })

  it('names the attach-image button by its own label and teaches no drag gesture', async () => {
    // PG review F1: the app has no drop target at all (no `@drop` /
    // `dragover` handler anywhere in `src/`). The two paths that exist are
    // the "⋯" menu item `chat.input.attachImage` and `ChatPanel.onPaste`
    // on the textarea, so those are the only two the guide may teach.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const dragWords = {
      'zh-TW': /拖/,
      'en-US': /\bdrag/i,
      'ja-JP': /ドラッグ/,
    } as const

    for (const [locale, catalogue] of Object.entries({
      'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP,
    })) {
      const typed = catalogue as unknown as {
        chat: { input: Record<string, string> }
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }
      const line = typed.playerGuide.chapters.basics.items?.attachImage ?? ''
      expect(line, `${locale} attachImage names the button`)
        .toContain(typed.chat.input.attachImage)
      expect(line, `${locale} attachImage keeps the paste path`)
        .toContain('Ctrl/⌘+V')
      expect(
        dragWords[locale as keyof typeof dragWords].test(line),
        `${locale} attachImage teaches a drag gesture that does not exist`,
      ).toBe(false)
    }
  })

  it('quotes the viewer’s own buttons and keeps the candidate tap semantics', async () => {
    // LB8: chapter 5 is the second player-facing description of the
    // lightbox, so it is where a label can drift away from the button it
    // names. Three are load-bearing:
    //
    //  - "open original" is the only remaining route to the untouched file
    //    (the `target="_blank"` anchors this series replaced are gone), so a
    //    renamed button with stale copy leaves no path to it at all.
    //  - the album is reached through a sidebar tab whose label is *not* the
    //    word the rest of the copy uses for it, so the line has to carry the
    //    tab's own label or the player goes looking for a tab that is not
    //    there.
    //  - tapping a candidate tile still cycles where that picture goes.
    //    Copy that read "tap to enlarge" there would have players silently
    //    changing stage/album/discard while trying to look at something —
    //    the one place in this chapter where wrong copy destroys work.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')

    for (const [locale, catalogue] of Object.entries({
      'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP,
    })) {
      const typed = catalogue as unknown as {
        lightbox: Record<string, string>
        playerSidebar: { tabs: Record<string, string> }
        characterImagesPanel: {
          candidates: { targets: Record<string, string> }
        }
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }
      const items = typed.playerGuide.chapters.images.items ?? {}
      expect(items.original, `${locale} original names the button`)
        .toContain(typed.lightbox.openOriginal)
      expect(items.open, `${locale} open names the album tab`)
        .toContain(typed.playerSidebar.tabs.album)
      for (const target of Object.values(
        typed.characterImagesPanel.candidates.targets,
      )) {
        expect(items.candidates, `${locale} candidates drops a cycle target`)
          .toContain(target)
      }
    }
  })

  it('teaches no drag gesture for the viewer either', async () => {
    // Same failure mode as `attachImage`: the lightbox listens for pointer
    // *swipes* (`classifyLightboxSwipe`) and has no drop target and no
    // drag-to-reorder. A player told to drag would press, hold, get nothing,
    // and conclude the picture is stuck.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const dragWords = {
      'zh-TW': /拖/,
      'en-US': /\bdrag/i,
      'ja-JP': /ドラッグ/,
    } as const

    for (const [locale, catalogue] of Object.entries({
      'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP,
    })) {
      const items = (catalogue as unknown as {
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }).playerGuide.chapters.images.items ?? {}
      for (const [id, copy] of Object.entries(items)) {
        expect(
          dragWords[locale as keyof typeof dragWords].test(copy),
          `${locale} images.${id} teaches a drag gesture that does not exist`,
        ).toBe(false)
      }
    }
  })

  it('only claims the /pic marker disappears when it is mixed into a sentence', async () => {
    // PG review F3: `_resolve_image_trigger` keeps the original message when
    // stripping would leave nothing, so a bare "/pic" does stay in context.
    // The copy may only promise the mixed-in case.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const qualifier = {
      'zh-TW': '混著打',
      'en-US': 'alongside',
      'ja-JP': '混ぜて打った',
    } as const

    for (const [locale, catalogue] of Object.entries({
      'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP,
    })) {
      const items = (catalogue as unknown as {
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }).playerGuide.chapters.basics.items ?? {}
      for (const id of ['pic', 'picSelfHost']) {
        expect(items[id], `${locale} ${id} qualifies the marker claim`)
          .toContain(qualifier[locale as keyof typeof qualifier])
      }
    }
  })

  it('tells hosted players that voice playback is charged and a replay is not', async () => {
    // PG review F2: the play button on a chat bubble synthesises on first
    // press (charged) and reuses the cached audio afterwards
    // (`billable_quantity=0` server-side). It carries no `ActionPriceHint`,
    // so this line is the only place a player can learn the cost exists.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const unitWord = { 'zh-TW': '扣點', 'en-US': 'Lumes', 'ja-JP': '蛍火' } as const
    const freeReplay = {
      'zh-TW': '不會再扣',
      'en-US': 'never charged twice',
      'ja-JP': '二重にかかることはありません',
    } as const

    for (const [locale, catalogue] of Object.entries({
      'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP,
    })) {
      const typed = catalogue as unknown as {
        chat: { bubble: Record<string, string> }
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }
      const items = typed.playerGuide.chapters.basics.items ?? {}
      const unit = unitWord[locale as keyof typeof unitWord]
      expect(items.voice, `${locale} voice names the play button`)
        .toContain(typed.chat.bubble.ttsPlay)
      expect(items.voice, `${locale} voice hides the charge`).toContain(unit)
      expect(items.voice, `${locale} voice promises a free replay`)
        .toContain(freeReplay[locale as keyof typeof freeReplay])
      // Self-host has no ledger at all, so its twin must not talk about one.
      expect(items.voiceSelfHost, `${locale} voiceSelfHost invents a charge`)
        .not.toContain(unit)
    }
  })

  it('keeps the price-tag line from promising a tag on every charge', async () => {
    // PG review F2c: a universal "everything that charges shows its price"
    // is falsified by the voice button, which is metered by length and has
    // no price hint. The line must carry the exception and point at ch.1.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const voiceWord = { 'zh-TW': '語音', 'en-US': 'voice', 'ja-JP': '音声' } as const
    const playbackWord = { 'zh-TW': '播放', 'en-US': 'play', 'ja-JP': '再生' } as const

    for (const [locale, catalogue] of Object.entries({
      'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP,
    })) {
      const items = (catalogue as unknown as {
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }).playerGuide.chapters.credits.items ?? {}
      expect(items.priceTag, `${locale} priceTag drops the voice exception`)
        .toContain(voiceWord[locale as keyof typeof voiceWord])
      // …and the "only when you press a button" list has to include it too.
      expect(items.whenCharged, `${locale} whenCharged omits voice playback`)
        .toContain(playbackWord[locale as keyof typeof playbackWord])
    }
  })

  it('quotes the surfaces it points at, verbatim, in every locale', async () => {
    // PG review F4/F6/F7/F8: every one of these lines named a label that
    // does not exist on screen (an English "Create" launcher, a "showcase"
    // that is a back-office feature key, a mistranslated cloud-only chip,
    // a renamed balance pool). Pin each to the key it is quoting.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')

    for (const [locale, catalogue] of Object.entries({
      'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP,
    })) {
      const typed = catalogue as unknown as {
        stage: { launchers: Record<string, string> }
        playerSidebar: {
          characterCards: {
            gallery: Record<string, string>
            cloudOnly: Record<string, string>
          }
        }
        credits: { badge: Record<string, string> }
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }
      const chapters = typed.playerGuide.chapters
      expect(chapters.studio.lead, `${locale} studio lead`)
        .toContain(typed.stage.launchers.studio)
      const showcase = chapters.portability.items?.showcase ?? ''
      expect(showcase, `${locale} showcase line`)
        .toContain(typed.playerSidebar.characterCards.gallery.title)
      expect(showcase, `${locale} cloud-only chip`)
        .toContain(typed.playerSidebar.characterCards.cloudOnly.chip)
      expect(chapters.credits.items?.balance, `${locale} balance pools`)
        .toContain(typed.credits.badge.gift)
    }
  })

  it('warns hosted players that repeated exports are throttled, and only them', async () => {
    // FX5: `character_backup_export_service._enforce_hosted_rate_limit`
    // only runs under `cloud_mode`, counts per operator (not per
    // character) over a rolling window, and refuses the export outright.
    // Chapter 8 taught the import-side slots and daily limit but not this,
    // so a hosted player following the guide walks into a wall. Self-host
    // has no such throttle, so its twin must not invent one.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const throttleWord = {
      'zh-TW': '暫時擋下',
      'en-US': 'turned away',
      'ja-JP': '断られる',
    } as const

    for (const [locale, catalogue] of Object.entries({
      'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP,
    })) {
      const items = (catalogue as unknown as {
        playerGuide: { chapters: Record<string, ChapterCopy> }
      }).playerGuide.chapters.portability.items ?? {}
      const word = throttleWord[locale as keyof typeof throttleWord]
      expect(items.backup, `${locale} backup hides the export throttle`)
        .toContain(word)
      expect(items.backupSelfHost, `${locale} backupSelfHost invents a throttle`)
        .not.toContain(word)
    }
  })

  it('keeps the player copy free of implementation vocabulary', async () => {
    // Hosted wording baseline: the guide is the second player-facing
    // description of the whole product, so it is the easiest place for
    // internal nouns to leak back in.
    const { messages: enUS } = await import('@/i18n/locales/en-US')
    const { messages: jaJP } = await import('@/i18n/locales/ja-JP')
    const banned = [
      'ComfyUI', 'LoRA', 'LLM', 'YAML', 'webhook', 'VAPID', 'API key',
      'ArcTemplate', 'arc ', 'beat', 'prompt', 'metadata', 'runtime',
    ]
    const flatten = (value: unknown): string[] =>
      typeof value === 'string'
        ? [value]
        : Object.values(value as Record<string, unknown>).flatMap(flatten)

    for (const catalogue of [zhTW, enUS, jaJP]) {
      for (const copy of flatten(catalogue.playerGuide)) {
        for (const word of banned) {
          expect(copy.includes(word), `"${word}" in: ${copy}`).toBe(false)
        }
      }
    }
  })
})
