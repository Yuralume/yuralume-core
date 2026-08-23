/**
 * 示意——讓角色先開口 (plan SN, ticket SN2).
 *
 * Three layers, matching how the feature is actually split:
 *   1. `buildStageNudgeTurn` / `wrapStageNudgeAsterisks` — the one piece of
 *      real branching (empty vs. not, wrap vs. don't) — pinned directly as
 *      pure functions, no rendering needed to reach it.
 *   2. `StageNudgeControl` — SSR-rendered (this repo has no DOM test infra:
 *      no jsdom, no @vue/test-utils), so what is observable is the emitted
 *      markup at whatever `open`/`disabled` state the props imply.
 *   3. `ChatPanel`'s wiring — the file is too large and dependency-heavy to
 *      SSR-render directly (same reasoning `actionPricingSurface.test.ts`
 *      gives for the composer's picture-price line), so the contract is
 *      pinned by scanning its source for the specific pieces that make the
 *      feature true: stage-only visibility, the `stage_nudge` flag on the
 *      wire, and the shared disable/price wiring.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { findJargon } from './fixtures/cloud-jargon-denylist'

import { buildStageNudgeTurn, wrapStageNudgeAsterisks } from '@/utils/stageNudge'
import StageNudgeControl from '@/components/StageNudgeControl.vue'

const CATALOGS = {
  'zh-TW': zhTW,
  'en-US': enUS,
  'ja-JP': jaJP,
} as const

type Locale = keyof typeof CATALOGS

const L = zhTW.chat.stageNudge

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

// ----------------------------------------------------------------------
// wrapStageNudgeAsterisks / buildStageNudgeTurn — the pure branching
// ----------------------------------------------------------------------

describe('wrapStageNudgeAsterisks', () => {
  it('wraps plain text in a matched pair of asterisks', () => {
    expect(wrapStageNudgeAsterisks('我推開門進來')).toBe('*我推開門進來*')
  })

  it('leaves text alone once it already carries an asterisk anywhere', () => {
    // The backend rule this mirrors: one pass only, or the player's own
    // `*knocks on the door*` would come back doubled.
    expect(wrapStageNudgeAsterisks('*敲了敲門*')).toBe('*敲了敲門*')
    expect(wrapStageNudgeAsterisks('走進來，順手把燈打開*')).toBe('走進來，順手把燈打開*')
  })
})

describe('buildStageNudgeTurn', () => {
  it('sends the raw (un-wrapped) trimmed text on the wire for a real supplement', () => {
    // The backend owns the `*…*` persistence rule — sending it pre-wrapped
    // would ask the backend to wrap something it is about to wrap again.
    const turn = buildStageNudgeTurn('  我推開門進來  ')

    expect(turn.message).toBe('我推開門進來')
  })

  it('renders the wrapped text as the optimistic player bubble', () => {
    const turn = buildStageNudgeTurn('我推開門進來')

    expect(turn.optimisticMessage).toEqual({
      role: 'user',
      content: '*我推開門進來*',
    })
  })

  it('produces no player-side bubble at all for a wordless press', () => {
    // Plan SN §2, verbatim: leaving it empty must not land a bubble — only
    // the character's own turn follows.
    expect(buildStageNudgeTurn('').optimisticMessage).toBeNull()
    expect(buildStageNudgeTurn('   ').optimisticMessage).toBeNull()
  })

  it('sends an empty message string for a wordless press, not a placeholder', () => {
    const turn = buildStageNudgeTurn('   ')

    expect(turn.message).toBe('')
  })

  it('never double-wraps a supplement the player already starred', () => {
    const turn = buildStageNudgeTurn('*敲了敲門*')

    expect(turn.optimisticMessage).toEqual({
      role: 'user',
      content: '*敲了敲門*',
    })
  })
})

// ----------------------------------------------------------------------
// StageNudgeControl — markup
// ----------------------------------------------------------------------

describe('StageNudgeControl', () => {
  it('renders a closed icon trigger naming the character', async () => {
    const html = await render(StageNudgeControl, { characterName: '芊璃' })

    expect(html).toContain('stage-nudge__trigger')
    expect(html).not.toContain('stage-nudge__popover')
    expect(html).toContain(L.title.replace('{name}', '芊璃'))
  })

  it('disables the trigger while the composer is busy', async () => {
    const html = await render(StageNudgeControl, {
      characterName: '芊璃',
      disabled: true,
    })

    expect(html).toContain('disabled')
  })

  it('leaves a clean, empty mount point for the hosted price', () => {
    // Same contract as StorySceneControl's `#price` slot: the control stays
    // free of billing knowledge, and self-host slots in nothing. The mount
    // point only exists inside the popover, which SSR never opens (no click
    // simulation in this repo — see the file header), so this is asserted
    // on the component's own source rather than a render.
    const src = readFileSync(
      new URL('../src/components/StageNudgeControl.vue', import.meta.url),
      'utf8',
    )
    expect(src).toMatch(/class="stage-nudge__price">\s*<slot name="price"/)
  })

  it('speaks all three languages', async () => {
    // The popover itself only renders once opened, which SSR cannot reach
    // (no click simulation here), but the trigger's `title`/`aria-label`
    // carry the same localized string and are present at rest.
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const html = await render(
        StageNudgeControl,
        { characterName: 'Yun' },
        locale as Locale,
      )
      const expected = catalog.chat.stageNudge.title.replace('{name}', 'Yun')

      expect(html, locale).toContain(`title="${expected}"`)
      expect(html, locale).toContain(`aria-label="${expected}"`)
    }
  })

  it('never says "示意" to the player, in any language', () => {
    // Plan SN §5: the internal code name stays internal; the player only
    // ever sees "let {name} speak first" phrasing.
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const copy = Object.values(catalog.chat.stageNudge)
      for (const value of copy) {
        expect(value, locale).not.toContain('示意')
      }
    }
  })

  it('discloses no operator jargon, in any language', async () => {
    for (const locale of Object.keys(CATALOGS) as Locale[]) {
      const html = await render(StageNudgeControl, { characterName: '芊璃' }, locale)

      expect(findJargon(visibleText(html)), locale).toEqual([])
    }
  })
})

// ----------------------------------------------------------------------
// ChatPanel wiring — source-scan (the file itself is too large / too
// dependency-heavy to SSR-render, same reasoning as the composer's
// picture-price line in actionPricingSurface.test.ts)
// ----------------------------------------------------------------------

function chatPanelSource(): string {
  return readFileSync(
    new URL('../src/components/ChatPanel.vue', import.meta.url),
    'utf8',
  )
}

describe('ChatPanel wiring', () => {
  const src = chatPanelSource()

  it('renders the control only in stage mode', () => {
    const match = src.match(
      /<StageNudgeControl\b[^>]*v-if="interactionMode === 'stage'"/,
    )
    expect(match).not.toBeNull()
  })

  it('opens in stage mode, so the button is visible without the player switching', () => {
    // The scene-access judge call that once made stage costlier to open by
    // default was retired 2026-07-30 (SA series); there is no remaining
    // cost reason to start narrower in DM. See owner ruling in the SN plan.
    expect(src).toContain("const interactionMode = ref<ChatInteractionMode>('stage')")
  })

  it('does not snap the player back to DM when the character changes', () => {
    // A forced `interactionMode.value = 'dm'` inside the character-id
    // watcher would undo the player's own mid-session choice every time
    // they switch characters — pin its absence directly in that watcher's
    // body, not just "somewhere in the file".
    const watcherMatch = src.match(
      /watch\(\(\) => props\.character\?\.id[\s\S]*?\}, \{ immediate: true \}\)/,
    )
    expect(watcherMatch).not.toBeNull()
    expect(watcherMatch![0]).not.toContain("interactionMode.value = 'dm'")
  })

  it('shares the story-scene busy guard rather than inventing a second one', () => {
    // "sending／串流中禁用" — the same computed StorySceneControl already
    // uses (`!character || sending || undoing`), not a parallel definition
    // that could drift from it.
    const match = src.match(
      /<StageNudgeControl\b[\s\S]*?:disabled="storySceneControlDisabled"/,
    )
    expect(match).not.toBeNull()
  })

  it('marks the request as a stage nudge', () => {
    expect(src).toContain('stage_nudge: true')
  })

  it('mounts the same ACTION_CHAT price hint the composer already shows', () => {
    const priceSlot = src.slice(
      src.indexOf('<StageNudgeControl'),
      src.indexOf('</StageNudgeControl>'),
    )
    expect(priceSlot).toContain('template #price')
    expect(priceSlot).toContain(':action-key="ACTION_CHAT"')
  })

  it('builds the turn through the shared pure function, not ad-hoc branching', () => {
    expect(src).toContain('buildStageNudgeTurn(rawText)')
  })

  it('imports the type mirror rather than a hand-rolled request shape', () => {
    expect(src).toContain("import type { ChatMessage, SendChatMessageRequest } from '@/types/chat'")
  })
})

describe('hosted jargon sweep — copy no render reaches', () => {
  it('covers the placeholder and confirm strings directly', () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const scene = catalog.chat.stageNudge
      for (const value of [scene.placeholder, scene.confirm]) {
        expect(findJargon(value), `${locale}: ${value}`).toEqual([])
      }
    }
  })
})
