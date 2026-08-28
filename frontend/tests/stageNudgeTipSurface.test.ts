/**
 * 首輪「讓{name}先開口」引導 (plan TR4).
 *
 * Four layers, matching how the feature is actually split:
 *   1. `shouldShowStageNudgeTip` + the per-character dismiss-storage
 *      helpers — the whole visibility matrix (mode × loading × message
 *      count × the PP modal's own visibility × dismissed) is pure, so it
 *      is pinned directly, no rendering. D-TR4-1 (stage-only) is pinned
 *      here as its own case.
 *   2. `ChatFirstTurnGuide` — the new nudge option, SSR-rendered (this
 *      repo has no DOM test infra: no jsdom, no @vue/test-utils), so what
 *      is observable is the emitted markup for the props given. D-TR4-1
 *      (no option in `dm` mode) is pinned here too, on the component that
 *      actually renders it.
 *   3. `StageNudgeTipHint` — SSR-rendered the same way.
 *   4. `ChatPanel`'s wiring — the file is too large and dependency-heavy
 *      to SSR-render directly (same reasoning `stageNudgeSurface.test.ts`
 *      and `playerPersonaNoteSurface.test.ts` give), so the contract is
 *      pinned by scanning its source for the pieces that make the feature
 *      true: the nudge option reuses the existing SN submit path, the tip
 *      is built from the pure function above with the right inputs
 *      (including D-TR4-2, the PP-modal-open gate), and the per-character
 *      dismissal resets on character change.
 *
 * Interaction this repo cannot reach at all (a click actually firing
 * `requestNudge` / `dismiss`, the popover's own submit) is covered on the
 * emitting side only — `StageNudgeControl`'s own click wiring already has
 * that same limitation documented in `stageNudgeSurface.test.ts`.
 */

// `ChatFirstTurnGuide` now mounts `ActionPriceHint`, which pulls in
// `useAuth`, which reads `localStorage` at module scope — import the shared
// stub first (ESM evaluates imports in declaration order) same as
// `dramaExitHub.test.ts`.
import './fixtures/browserGlobals'

import { readFileSync } from 'node:fs'

import { describe, expect, it, vi } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

// `ActionPriceHint` -> `useActionPricing` -> `utils/api/cloudPricing` ->
// `authedFetch`, which imports the router — and `createWebHistory()` at
// module scope needs `window` (same reasoning `dramaExitHub.test.ts` gives).
vi.mock('@/utils/authedFetch', () => ({ authedFetch: vi.fn() }))

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { findJargon } from './fixtures/cloud-jargon-denylist'

import {
  STAGE_NUDGE_TIP_DISMISS_KEY_PREFIX,
  isStageNudgeTipDismissed,
  rememberStageNudgeTipDismissed,
  shouldShowStageNudgeTip,
  stageNudgeTipDismissKey,
  type StageNudgeTipVisibilityInput,
} from '@/utils/stageNudgeTip'
import ChatFirstTurnGuide from '@/components/ChatFirstTurnGuide.vue'
import StageNudgeTipHint from '@/components/StageNudgeTipHint.vue'

const CATALOGS = {
  'zh-TW': zhTW,
  'en-US': enUS,
  'ja-JP': jaJP,
} as const

type Locale = keyof typeof CATALOGS

async function render(
  component: unknown,
  props: Record<string, unknown> = {},
  locale: Locale = 'zh-TW',
): Promise<string> {
  const app = createSSRApp(component as Parameters<typeof createSSRApp>[0], props)
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

function fakeStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  } satisfies Pick<Storage, 'getItem' | 'setItem'>
}

/** 隱私模式：碰 localStorage 直接 throw。 */
function throwingStorage() {
  return {
    getItem: () => {
      throw new Error('denied')
    },
    setItem: () => {
      throw new Error('denied')
    },
  } satisfies Pick<Storage, 'getItem' | 'setItem'>
}

// ----------------------------------------------------------------------
// shouldShowStageNudgeTip — the visibility matrix
// ----------------------------------------------------------------------

const READY: StageNudgeTipVisibilityInput = {
  mode: 'stage',
  loadingHistory: false,
  historyFailed: false,
  messageCount: 0,
  noteLoaded: true,
  personaNoteModalOpen: false,
  dismissed: false,
}

describe('shouldShowStageNudgeTip', () => {
  it('shows on a first visit to the stage: zero messages, nothing open, never dismissed', () => {
    expect(shouldShowStageNudgeTip(READY)).toBe(true)
  })

  it('D-TR4-1: never shows in DM mode — TR2 pre-message proactive covers that side', () => {
    expect(shouldShowStageNudgeTip({ ...READY, mode: 'dm' })).toBe(false)
  })

  it('stays quiet while the history is still loading', () => {
    // Mid-load messageCount is 0 for everyone (same trap PP's own gate
    // documents) — without this the tip flashes before the thread is
    // confirmed empty.
    expect(shouldShowStageNudgeTip({
      ...READY,
      loadingHistory: true,
      messageCount: 0,
    })).toBe(false)
  })

  it('stays quiet once the conversation has started', () => {
    expect(shouldShowStageNudgeTip({ ...READY, messageCount: 1 })).toBe(false)
  })

  it('stays quiet when the history load failed — a network blip must not burn the dismiss for an old player', () => {
    // Same trap PP's own `historyFailed` gate documents: on failure
    // `messageCount` is 0 same as a genuinely-empty thread, and
    // `loadingHistory` has already settled back to false.
    expect(shouldShowStageNudgeTip({ ...READY, historyFailed: true })).toBe(false)
  })

  it('D-TR4-2: stays quiet until the persona note has resolved, before the PP modal gets a chance to open', () => {
    // `personaNoteModalOpen` only flips true once `shouldPromptPlayerPersonaNote`
    // has actually decided to open it. Before the note finishes loading there is
    // a window where both `noteLoaded` and `personaNoteModalOpen` read false —
    // without this gate the tip flashes there and gets covered by the PP modal
    // a beat later, instead of yielding to it up front.
    expect(shouldShowStageNudgeTip({ ...READY, noteLoaded: false })).toBe(false)
  })

  it('D-TR4-2: yields to the player-persona-note modal while it is open', () => {
    expect(shouldShowStageNudgeTip({ ...READY, personaNoteModalOpen: true })).toBe(false)
  })

  it('D-TR4-2: shows once the persona-note modal has closed, all else unchanged', () => {
    expect(shouldShowStageNudgeTip({ ...READY, personaNoteModalOpen: false })).toBe(true)
  })

  it('stays quiet once the player has dismissed it on this device', () => {
    expect(shouldShowStageNudgeTip({ ...READY, dismissed: true })).toBe(false)
  })
})

// ----------------------------------------------------------------------
// dismissal — per-character, fail-soft storage (same shape as PP's)
// ----------------------------------------------------------------------

describe('stage nudge tip dismissal', () => {
  it('keys the memory per character', () => {
    expect(stageNudgeTipDismissKey('c1')).toBe(`${STAGE_NUDGE_TIP_DISMISS_KEY_PREFIX}c1`)
    expect(stageNudgeTipDismissKey('c1')).not.toBe(stageNudgeTipDismissKey('c2'))
  })

  it('remembers a dismissal for one character without silencing the others', () => {
    const storage = fakeStorage()

    expect(rememberStageNudgeTipDismissed(storage, 'c1')).toBe(true)

    expect(isStageNudgeTipDismissed(storage, 'c1')).toBe(true)
    expect(isStageNudgeTipDismissed(storage, 'c2')).toBe(false)
  })

  it('reads false rather than throwing when storage is unavailable', () => {
    expect(isStageNudgeTipDismissed(throwingStorage(), 'c1')).toBe(false)
    expect(rememberStageNudgeTipDismissed(throwingStorage(), 'c1')).toBe(false)
    expect(isStageNudgeTipDismissed(null, 'c1')).toBe(false)
    expect(rememberStageNudgeTipDismissed(null, 'c1')).toBe(false)
  })

  it('treats a missing character id as nothing to remember', () => {
    const storage = fakeStorage()
    expect(rememberStageNudgeTipDismissed(storage, null)).toBe(false)
    expect(isStageNudgeTipDismissed(storage, null)).toBe(false)
  })
})

// ----------------------------------------------------------------------
// ChatFirstTurnGuide — the new nudge option
// ----------------------------------------------------------------------

describe('ChatFirstTurnGuide nudge option', () => {
  it('offers "let {name} speak first" in stage mode', async () => {
    const html = await render(ChatFirstTurnGuide, {
      characterName: '芊璃',
      mode: 'stage',
      context: '',
    })

    expect(html).toContain('first-turn-guide__nudge')
    expect(html).toContain(zhTW.chat.onboarding.nudgeOption.replace('{name}', '芊璃'))
  })

  it('D-TR4-1: does not offer it in dm mode — TR2 covers that side instead', async () => {
    const html = await render(ChatFirstTurnGuide, {
      characterName: '芊璃',
      mode: 'dm',
      context: '',
    })

    expect(html).not.toContain('first-turn-guide__nudge')
    expect(html).not.toContain(zhTW.chat.onboarding.nudgeOption.replace('{name}', '芊璃'))
  })

  it('mounts the same ACTION_CHAT price hint the composer already shows', () => {
    const src = readFileSync(
      new URL('../src/components/ChatFirstTurnGuide.vue', import.meta.url),
      'utf8',
    )
    const nudgeBlock = src.slice(
      src.indexOf('first-turn-guide__nudge"'),
      src.indexOf('first-turn-guide__starters"'),
    )
    expect(nudgeBlock).toContain('<ActionPriceHint')
    expect(nudgeBlock).toContain(':action-key="ACTION_CHAT"')
  })

  it('emits a plain requestNudge event, not a hand-built payload', () => {
    // No click simulation in this repo (no jsdom) — pin the wiring on the
    // component's own source instead, same as StageNudgeControl's submit
    // button in stageNudgeSurface.test.ts.
    const src = readFileSync(
      new URL('../src/components/ChatFirstTurnGuide.vue', import.meta.url),
      'utf8',
    )
    expect(src).toContain("requestNudge: []")
    expect(src).toContain("@click=\"emit('requestNudge')\"")
  })

  it('speaks all three languages', async () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const html = await render(
        ChatFirstTurnGuide,
        { characterName: 'Yun', mode: 'stage', context: '' },
        locale as Locale,
      )
      const expected = catalog.chat.onboarding.nudgeOption.replace('{name}', 'Yun')
      expect(html, locale).toContain(expected)
    }
  })

  it('discloses no operator jargon, in any language', async () => {
    for (const locale of Object.keys(CATALOGS) as Locale[]) {
      const html = await render(
        ChatFirstTurnGuide,
        { characterName: '芊璃', mode: 'stage', context: '' },
        locale,
      )
      expect(findJargon(visibleText(html)), locale).toEqual([])
    }
  })
})

// ----------------------------------------------------------------------
// StageNudgeTipHint — markup
// ----------------------------------------------------------------------

describe('StageNudgeTipHint', () => {
  it('renders nothing when not visible', async () => {
    const html = await render(StageNudgeTipHint, {
      visible: false,
      characterName: '芊璃',
    })

    expect(html).not.toContain('stage-nudge-tip"')
  })

  it('renders the tip, naming the character, when visible', async () => {
    const html = await render(StageNudgeTipHint, {
      visible: true,
      characterName: '芊璃',
    })

    expect(html).toContain('stage-nudge-tip')
    expect(html).toContain(zhTW.chat.stageNudge.tip.replace('{name}', '芊璃'))
    expect(html).toContain(zhTW.chat.stageNudge.tipDismiss)
  })

  it('speaks all three languages', async () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const html = await render(
        StageNudgeTipHint,
        { visible: true, characterName: 'Yun' },
        locale as Locale,
      )
      expect(html, locale).toContain(catalog.chat.stageNudge.tip.replace('{name}', 'Yun'))
    }
  })

  it('points the arrow at StageNudgeControl, not the send button', () => {
    // No jsdom in this repo, so the actual rendered position can't be
    // measured — pin the derivation instead: `.input-row`'s siblings from
    // the right are send-btn (min-width 88px, the floor every shipped
    // locale's resting-state label sits on), an 8px flex gap, then
    // StageNudgeControl (fixed 44px, see StageNudgeControl.vue's
    // `.stage-nudge__trigger`). Centering on the icon is
    // 8 + 88 + 44/2 = 118px from the right edge. If any of those three
    // numbers change, this constant must be recomputed, not re-guessed.
    const src = readFileSync(
      new URL('../src/components/StageNudgeTipHint.vue', import.meta.url),
      'utf8',
    )
    const afterBlock = src.slice(
      src.indexOf('.stage-nudge-tip::after'),
      src.indexOf('.stage-nudge-tip::after') + 400,
    )
    expect(afterBlock).toContain('right: 118px;')
  })

  it('never says "示意" to the player, in any language', () => {
    // Plan SN §5's internal-code-name rule extends to everything this
    // ticket adds under the same namespace.
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      expect(catalog.chat.stageNudge.tip, locale).not.toContain('示意')
      expect(catalog.chat.stageNudge.tipDismiss, locale).not.toContain('示意')
    }
  })

  it('discloses no operator jargon, in any language', async () => {
    for (const locale of Object.keys(CATALOGS) as Locale[]) {
      const html = await render(
        StageNudgeTipHint,
        { visible: true, characterName: '芊璃' },
        locale,
      )
      expect(findJargon(visibleText(html)), locale).toEqual([])
    }
  })
})

// ----------------------------------------------------------------------
// ChatPanel wiring — source-scan (the file itself is too large / too
// dependency-heavy to SSR-render, same reasoning as stageNudgeSurface's
// and playerPersonaNoteSurface's own ChatPanel sections)
// ----------------------------------------------------------------------

function chatPanelSource(): string {
  return readFileSync(
    new URL('../src/components/ChatPanel.vue', import.meta.url),
    'utf8',
  )
}

describe('ChatPanel wiring', () => {
  const src = chatPanelSource()

  it('routes the guide\'s nudge option through the existing SN submit path', () => {
    // Plan TR4 §1, verbatim: no new send pipeline, just the existing
    // blank-nudge call the icon-triggered popover already uses.
    expect(src).toContain('@request-nudge="handleStageNudgeSubmit(\'\')"')
  })

  it('computes tip visibility through the shared pure function', () => {
    const block = src.slice(
      src.indexOf('const stageNudgeTipVisible = computed('),
      src.indexOf('const modeStatusLabel = computed('),
    )
    expect(block).toContain('shouldShowStageNudgeTip({')
    expect(block).toContain('mode: interactionMode.value')
    expect(block).toContain('loadingHistory: props.loadingHistory ?? false')
    expect(block).toContain('historyFailed: props.historyFailed ?? false')
    expect(block).toContain('messageCount: localMessages.value.length')
    expect(block).toContain('noteLoaded: playerPersonaNoteLoaded.value')
    expect(block).toContain('personaNoteModalOpen: playerPersonaNoteOpen.value')
    expect(block).toContain('dismissed: stageNudgeTipDismissed.value')
  })

  it('re-reads the per-character dismissal when the character changes', () => {
    const watcherMatch = src.match(
      /watch\(\(\) => props\.character\?\.id[\s\S]*?\}, \{ immediate: true \}\)/,
    )
    expect(watcherMatch).not.toBeNull()
    expect(watcherMatch![0]).toContain('isStageNudgeTipDismissed(')
    expect(watcherMatch![0]).toContain('stageNudgeTipDismissed.value =')
  })

  it('records the dismissal instead of only hiding the tip', () => {
    const handler = src.slice(
      src.indexOf('function dismissStageNudgeTip()'),
      src.indexOf('function dismissStageNudgeTip()') + 300,
    )
    expect(handler).toContain('rememberStageNudgeTipDismissed(')
    expect(handler).toContain('stageNudgeTipDismissed.value = true')
  })

  it('reaches localStorage only through the throw-safe getter', () => {
    expect(src).toContain('rememberStageNudgeTipDismissed(getSafeLocalStorage()')
    const call = src.slice(
      src.indexOf('isStageNudgeTipDismissed('),
      src.indexOf('isStageNudgeTipDismissed(') + 80,
    )
    expect(call).toContain('getSafeLocalStorage()')
    expect(src).not.toContain('rememberStageNudgeTipDismissed(window.localStorage')
    expect(src).not.toContain('isStageNudgeTipDismissed(window.localStorage')
  })

  it('mounts the tip hint above the input row, naming the character it points at', () => {
    const mount = src.slice(
      src.indexOf('<StageNudgeTipHint'),
      src.indexOf('<div class="input-row">'),
    )
    expect(mount).toContain(':visible="stageNudgeTipVisible"')
    expect(mount).toContain(':character-name="characterDisplayName"')
    expect(mount).toContain('@dismiss="dismissStageNudgeTip"')
  })
})

describe('hosted jargon sweep — copy no render reaches', () => {
  it('covers the tip and dismiss strings directly', () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const scene = catalog.chat.stageNudge
      for (const value of [scene.tip, scene.tipDismiss]) {
        expect(findJargon(value), `${locale}: ${value}`).toEqual([])
      }
      expect(
        findJargon(catalog.chat.onboarding.nudgeOption),
        `${locale}: ${catalog.chat.onboarding.nudgeOption}`,
      ).toEqual([])
    }
  })
})
