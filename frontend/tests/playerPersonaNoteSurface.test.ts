/**
 * 玩家人設補充 (plan PP, ticket PP4).
 *
 * Four layers, matching how the feature is actually split:
 *   1. `shouldPromptPlayerPersonaNote` + the dismiss-storage helpers — the
 *      whole trigger matrix (note empty/filled × 0/N messages × dismissed ×
 *      still loading) is pure, so it is pinned directly, no rendering.
 *   2. `utils/api/playerPersonaNote` — the wire shape, including the one
 *      thing that is easy to get wrong: an empty string is *clear*, not
 *      "leave it alone", so it must still reach the server.
 *   3. `PlayerPersonaNoteChip` / `PlayerPersonaNoteModal` — SSR-rendered
 *      (this repo has no DOM test infra: no jsdom, no @vue/test-utils), so
 *      what is observable is the emitted markup for the props given.
 *   4. `ChatPanel` / `CharacterSettingsSection` wiring — ChatPanel is too
 *      large and dependency-heavy to SSR-render (same reasoning
 *      `stageNudgeSurface.test.ts` gives), so the contract is pinned by
 *      scanning the source for the pieces that make the feature true.
 */

import { readFileSync } from 'node:fs'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { findJargon } from './fixtures/cloud-jargon-denylist'

import {
  PLAYER_PERSONA_NOTE_DISMISS_KEY_PREFIX,
  isPlayerPersonaNoteDismissed,
  playerPersonaNoteDismissKey,
  rememberPlayerPersonaNoteDismissed,
  shouldPromptPlayerPersonaNote,
  type PlayerPersonaNotePromptInput,
} from '@/utils/playerPersonaNote'
import {
  PLAYER_PERSONA_NOTE_MAX_CHARS,
  getPlayerPersonaNote,
  updatePlayerPersonaNote,
} from '@/utils/api/playerPersonaNote'
import {
  PLAYER_PERSONA_NOTE_UPDATED_EVENT,
  publishPlayerPersonaNoteUpdated,
  usePlayerPersonaNote,
} from '@/composables/usePlayerPersonaNote'
import PlayerPersonaNoteChip from '@/components/PlayerPersonaNoteChip.vue'
import PlayerPersonaNoteModal from '@/components/PlayerPersonaNoteModal.vue'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    post: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
})

const CATALOGS = {
  'zh-TW': zhTW,
  'en-US': enUS,
  'ja-JP': jaJP,
} as const

type Locale = keyof typeof CATALOGS

const L = zhTW.playerPersonaNote

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
  // The modal teleports to <body>; SSR collects those fragments separately
  // instead of inlining them, so both halves are returned concatenated.
  const context: { teleports?: Record<string, string> } = {}
  const html = await renderToString(app, context)
  return html + Object.values(context.teleports ?? {}).join('')
}

function withName(template: string, name: string): string {
  return template.replace(/\{name\}/g, name)
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
// shouldPromptPlayerPersonaNote — the trigger matrix
// ----------------------------------------------------------------------

const READY: PlayerPersonaNotePromptInput = {
  characterId: 'c1',
  loadingHistory: false,
  historyFailed: false,
  noteLoaded: true,
  note: '',
  messageCount: 0,
  dismissed: false,
  alreadyPrompted: false,
}

describe('shouldPromptPlayerPersonaNote', () => {
  it('asks on a first visit: nothing written, nothing said, never dismissed', () => {
    expect(shouldPromptPlayerPersonaNote(READY)).toBe(true)
  })

  it('stays quiet once the player has written something', () => {
    expect(shouldPromptPlayerPersonaNote({ ...READY, note: '我是超能力者' })).toBe(false)
  })

  it('treats a whitespace-only note as nothing written', () => {
    expect(shouldPromptPlayerPersonaNote({ ...READY, note: '   \n ' })).toBe(true)
  })

  it('stays quiet once the conversation has started', () => {
    expect(shouldPromptPlayerPersonaNote({ ...READY, messageCount: 1 })).toBe(false)
  })

  it('stays quiet while the history is still loading', () => {
    // The one that matters: mid-load `messageCount` is 0 for *everyone*, so
    // without this gate a player with a thousand messages gets the popup.
    expect(shouldPromptPlayerPersonaNote({
      ...READY,
      loadingHistory: true,
      messageCount: 0,
    })).toBe(false)
  })

  it('stays quiet when the history could not be read at all', () => {
    // The failure collapses into `messages = []` upstream, so mid-failure
    // `messageCount` is 0 and `loadingHistory` is already back to false —
    // exactly the shape of a brand new thread. Without this gate one network
    // blip pops the box at a player with hundreds of messages, and "maybe
    // later" then writes a permanent dismissal for a question that should
    // never have been asked.
    expect(shouldPromptPlayerPersonaNote({
      ...READY,
      historyFailed: true,
      loadingHistory: false,
      messageCount: 0,
    })).toBe(false)
  })

  it('asks again once a retry actually loaded the (empty) thread', () => {
    // The flag is per-load, not sticky: a successful reload restores the
    // ordinary first-visit behaviour.
    expect(shouldPromptPlayerPersonaNote({ ...READY, historyFailed: false })).toBe(true)
  })

  it('stays quiet before the note itself is known', () => {
    // GET still in flight, or it failed — either way "no note" is not a fact
    // yet, and guessing wrong means popping up at someone who already wrote one.
    expect(shouldPromptPlayerPersonaNote({ ...READY, noteLoaded: false })).toBe(false)
  })

  it('stays quiet after "maybe later" on this device', () => {
    expect(shouldPromptPlayerPersonaNote({ ...READY, dismissed: true })).toBe(false)
  })

  it('does not re-open itself after the player closed it this visit', () => {
    expect(shouldPromptPlayerPersonaNote({ ...READY, alreadyPrompted: true })).toBe(false)
  })

  it('stays quiet with no character selected', () => {
    expect(shouldPromptPlayerPersonaNote({ ...READY, characterId: null })).toBe(false)
  })
})

// ----------------------------------------------------------------------
// "maybe later" — per-character, fail-soft storage
// ----------------------------------------------------------------------

describe('player persona note dismissal', () => {
  it('keys the memory per character', () => {
    expect(playerPersonaNoteDismissKey('c1')).toBe(
      `${PLAYER_PERSONA_NOTE_DISMISS_KEY_PREFIX}c1`,
    )
    expect(playerPersonaNoteDismissKey('c1')).not.toBe(playerPersonaNoteDismissKey('c2'))
  })

  it('remembers "maybe later" for one character without silencing the others', () => {
    const storage = fakeStorage()

    expect(rememberPlayerPersonaNoteDismissed(storage, 'c1')).toBe(true)

    expect(isPlayerPersonaNoteDismissed(storage, 'c1')).toBe(true)
    expect(isPlayerPersonaNoteDismissed(storage, 'c2')).toBe(false)
  })

  it('reads false rather than throwing when storage is unavailable', () => {
    // Private mode: touching localStorage throws. The chat panel must keep
    // working — at worst the popup shows again next time.
    expect(isPlayerPersonaNoteDismissed(throwingStorage(), 'c1')).toBe(false)
    expect(rememberPlayerPersonaNoteDismissed(throwingStorage(), 'c1')).toBe(false)
    expect(isPlayerPersonaNoteDismissed(null, 'c1')).toBe(false)
    expect(rememberPlayerPersonaNoteDismissed(null, 'c1')).toBe(false)
  })

  it('cannot be recorded without a character', () => {
    const storage = fakeStorage()
    expect(rememberPlayerPersonaNoteDismissed(storage, null)).toBe(false)
    expect(isPlayerPersonaNoteDismissed(storage, null)).toBe(false)
  })
})

// ----------------------------------------------------------------------
// API — the wire shape
// ----------------------------------------------------------------------

describe('player persona note API', () => {
  it('loads the declaration for one character', async () => {
    const data = {
      character_id: 'c1',
      operator_id: 'op1',
      note: '我是超能力者',
      updated_at: '2026-08-17T00:00:00Z',
    }
    mockedAxios.get.mockResolvedValueOnce({ data })

    await expect(getPlayerPersonaNote('c1')).resolves.toEqual(data)
    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/c1/player-persona-note',
    )
  })

  it('PUTs the note as a full replacement', async () => {
    mockedAxios.put.mockResolvedValueOnce({
      data: { character_id: 'c1', operator_id: 'op1', note: '我是超能力者' },
    })

    await updatePlayerPersonaNote('c1', '我是超能力者')

    expect(mockedAxios.put).toHaveBeenCalledWith(
      '/api/v1/characters/c1/player-persona-note',
      { note: '我是超能力者' },
    )
  })

  it('sends the empty string through rather than short-circuiting a clear', () => {
    // Clearing is a write, not a no-op: `note: ''` is what removes it.
    mockedAxios.put.mockResolvedValueOnce({
      data: { character_id: 'c1', operator_id: 'op1', note: '' },
    })

    void updatePlayerPersonaNote('c1', '')

    expect(mockedAxios.put).toHaveBeenCalledWith(
      '/api/v1/characters/c1/player-persona-note',
      { note: '' },
    )
  })

  it('encodes the character id in the path', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: {} })
    await getPlayerPersonaNote('a/b')
    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/a%2Fb/player-persona-note',
    )
  })

  it('mirrors the backend length ceiling', () => {
    expect(PLAYER_PERSONA_NOTE_MAX_CHARS).toBe(500)
  })
})

// ----------------------------------------------------------------------
// usePlayerPersonaNote — the one state the chip and the popup share
// ----------------------------------------------------------------------

describe('usePlayerPersonaNote', () => {
  it('exposes the loaded note as a fact only once the GET returned', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: { character_id: 'c1', operator_id: 'op1', note: '我是超能力者' },
    })
    const state = usePlayerPersonaNote()

    await state.load('c1')

    expect(state.note.value).toBe('我是超能力者')
    expect(state.loaded.value).toBe(true)
    expect(state.loading.value).toBe(false)
  })

  it('leaves "loaded" false when the read fails, so nothing pops up on a guess', async () => {
    mockedAxios.get.mockRejectedValueOnce(new Error('offline'))
    const state = usePlayerPersonaNote()

    await state.load('c1')

    expect(state.loaded.value).toBe(false)
    expect(state.note.value).toBe('')
  })

  it('clears the previous character rather than showing its note under a new name', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: { character_id: 'c1', operator_id: 'op1', note: '我是超能力者' },
    })
    const state = usePlayerPersonaNote()
    await state.load('c1')

    await state.load(null)

    expect(state.note.value).toBe('')
    expect(state.loaded.value).toBe(false)
  })

  it('takes a saved value (including a clear) without re-reading the server', () => {
    const state = usePlayerPersonaNote()

    state.apply('我是超能力者')
    expect(state.note.value).toBe('我是超能力者')
    expect(state.loaded.value).toBe(true)

    state.apply('')
    expect(state.note.value).toBe('')
    expect(state.loaded.value).toBe(true)
  })

  it('re-reads the character it is currently showing when asked to retry', async () => {
    mockedAxios.get
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        data: { character_id: 'c1', operator_id: 'op1', note: '我是超能力者' },
      })
    const state = usePlayerPersonaNote()
    await state.load('c1')
    expect(state.loaded.value).toBe(false)

    // The retry needs no argument: the state already knows whose note it is.
    await state.reload()

    expect(state.note.value).toBe('我是超能力者')
    expect(state.loaded.value).toBe(true)
    expect(mockedAxios.get).toHaveBeenCalledTimes(2)
  })
})

// ----------------------------------------------------------------------
// Cross-surface sync — StagePage mounts the chat panel and the settings
// tab at the same time, each with its own copy of the state above.
// ----------------------------------------------------------------------

/** An `EventTarget`-backed stand-in for `window` (this suite runs in node). */
function installFakeWindow() {
  const bus = new EventTarget()
  vi.stubGlobal('window', {
    addEventListener: bus.addEventListener.bind(bus),
    removeEventListener: bus.removeEventListener.bind(bus),
    dispatchEvent: bus.dispatchEvent.bind(bus),
  })
}

describe('player persona note cross-surface sync', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('refreshes every surface showing that character when one of them saves', async () => {
    // The bug this pins: the settings tab saves, the chat header keeps the
    // pre-save value, and the next save from the chat header replaces the
    // whole note with that stale text.
    installFakeWindow()
    mockedAxios.get.mockResolvedValue({
      data: { character_id: 'c1', operator_id: 'op1', note: '' },
    })
    const chatPanel = usePlayerPersonaNote()
    const settingsTab = usePlayerPersonaNote()
    await chatPanel.load('c1')
    await settingsTab.load('c1')

    settingsTab.apply('我是超能力者')
    publishPlayerPersonaNoteUpdated('c1', '我是超能力者')

    expect(chatPanel.note.value).toBe('我是超能力者')
    expect(chatPanel.loaded.value).toBe(true)
  })

  it('keeps a broadcast value once a slower in-flight GET resolves with a stale one', async () => {
    // The bug this pins: load('c1') is in flight, another surface saves and
    // broadcasts the fresh note, `apply()` takes it — then the *old* GET
    // response lands and, without also invalidating in-flight reads,
    // overwrites the fresh value with the answer to a question nobody is
    // asking anymore.
    installFakeWindow()
    let settleGet: (note: string) => void = () => {}
    const pending = new Promise<{ data: { character_id: string, operator_id: string, note: string } }>((resolve) => {
      settleGet = (note) => resolve({
        data: { character_id: 'c1', operator_id: 'op1', note },
      })
    })
    mockedAxios.get.mockReturnValueOnce(pending)
    const state = usePlayerPersonaNote()

    const load = state.load('c1')
    publishPlayerPersonaNoteUpdated('c1', '新的內容')
    settleGet('過期的內容')
    await load

    expect(state.note.value).toBe('新的內容')
    expect(state.loaded.value).toBe(true)
  })

  it('propagates a clear as faithfully as a write', async () => {
    installFakeWindow()
    mockedAxios.get.mockResolvedValue({
      data: { character_id: 'c1', operator_id: 'op1', note: '我是超能力者' },
    })
    const chatPanel = usePlayerPersonaNote()
    await chatPanel.load('c1')

    publishPlayerPersonaNoteUpdated('c1', '')

    expect(chatPanel.note.value).toBe('')
    expect(chatPanel.loaded.value).toBe(true)
  })

  it('ignores a save for a different character', async () => {
    installFakeWindow()
    mockedAxios.get.mockResolvedValue({
      data: { character_id: 'c1', operator_id: 'op1', note: '我是超能力者' },
    })
    const chatPanel = usePlayerPersonaNote()
    await chatPanel.load('c1')

    publishPlayerPersonaNoteUpdated('c2', '別人的自述')

    expect(chatPanel.note.value).toBe('我是超能力者')
  })

  it('ignores a save while no character is selected', () => {
    installFakeWindow()
    const state = usePlayerPersonaNote()

    publishPlayerPersonaNoteUpdated('c1', '我是超能力者')

    expect(state.note.value).toBe('')
    expect(state.loaded.value).toBe(false)
  })

  it('rides the same window-CustomEvent convention as the operator profile', () => {
    expect(PLAYER_PERSONA_NOTE_UPDATED_EVENT).toBe('kokoro:player-persona-note-updated')

    installFakeWindow()
    const received: unknown[] = []
    window.addEventListener(
      PLAYER_PERSONA_NOTE_UPDATED_EVENT,
      (event) => received.push((event as CustomEvent).detail),
    )

    publishPlayerPersonaNoteUpdated('c1', '我是超能力者')

    expect(received).toEqual([{ characterId: 'c1', note: '我是超能力者' }])
  })

  it('is a no-op without a window rather than throwing', () => {
    // SSR / this very test suite's default: publishing must not explode.
    expect(() => publishPlayerPersonaNoteUpdated('c1', 'x')).not.toThrow()
  })
})

// ----------------------------------------------------------------------
// PlayerPersonaNoteChip — the always-there entry point in the chat header
// ----------------------------------------------------------------------

describe('PlayerPersonaNoteChip', () => {
  it('renders a hollow chip when nothing has been written yet', async () => {
    const html = await render(PlayerPersonaNoteChip, {
      filled: false,
      characterName: '芊璃',
    })

    expect(html).toContain('ui-btn--chip')
    expect(html).not.toContain('is-active')
    expect(html).toContain('○')
    expect(html).toContain(L.chip.emptyHint.replace('{name}', '芊璃'))
  })

  it('renders a filled chip once the player has written one', async () => {
    const html = await render(PlayerPersonaNoteChip, {
      filled: true,
      characterName: '芊璃',
    })

    expect(html).toContain('is-active')
    expect(html).toContain('●')
    expect(html).toContain(L.chip.filledHint.replace('{name}', '芊璃'))
  })

  it('separates the two states by more than colour alone', async () => {
    // `is-active` is a background swap on a dark chip; the glyph is what
    // survives low contrast and colour-vision differences.
    const empty = await render(PlayerPersonaNoteChip, { filled: false, characterName: 'Yun' })
    const full = await render(PlayerPersonaNoteChip, { filled: true, characterName: 'Yun' })

    expect(empty).toContain('○')
    expect(empty).not.toContain('●')
    expect(full).toContain('●')
    expect(full).not.toContain('○')
  })

  it('speaks all three languages', async () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const html = await render(
        PlayerPersonaNoteChip,
        { filled: false, characterName: 'Yun' },
        locale as Locale,
      )

      expect(html, locale).toContain(catalog.playerPersonaNote.chip.label)
      expect(html, locale).toContain(
        catalog.playerPersonaNote.chip.emptyHint.replace('{name}', 'Yun'),
      )
    }
  })

  it('discloses no operator jargon, in any language', async () => {
    for (const locale of Object.keys(CATALOGS) as Locale[]) {
      const html = await render(
        PlayerPersonaNoteChip,
        { filled: true, characterName: '芊璃' },
        locale,
      )

      expect(findJargon(visibleText(html)), locale).toEqual([])
    }
  })
})

// ----------------------------------------------------------------------
// PlayerPersonaNoteModal — the one editor all three surfaces share
// ----------------------------------------------------------------------

const MODAL_PROPS = {
  visible: true,
  characterId: 'c1',
  characterName: '芊璃',
  initialNote: '',
  noteLoaded: true,
}

describe('PlayerPersonaNoteModal', () => {
  it('renders nothing while closed', async () => {
    const html = await render(PlayerPersonaNoteModal, { ...MODAL_PROPS, visible: false })

    expect(html).not.toContain('persona-note-modal')
  })

  it('explains what the box is for, naming the character', async () => {
    const html = await render(PlayerPersonaNoteModal, MODAL_PROPS)

    expect(html).toContain(withName(L.description, '芊璃'))
    expect(html).toContain(L.title.replace('{name}', '芊璃'))
  })

  it('uses the shared dark-theme textarea, capped at the backend ceiling', async () => {
    const html = await render(PlayerPersonaNoteModal, MODAL_PROPS)

    expect(html).toContain('field-textarea')
    expect(html).toContain(`maxlength="${PLAYER_PERSONA_NOTE_MAX_CHARS}"`)
  })

  it('shows a character counter against that same ceiling', async () => {
    const html = await render(PlayerPersonaNoteModal, {
      ...MODAL_PROPS,
      initialNote: '我是超能力者',
    })

    expect(html).toContain(
      L.counter
        .replace('{used}', '6')
        .replace('{max}', String(PLAYER_PERSONA_NOTE_MAX_CHARS)),
    )
  })

  it('offers exactly the two ways out the plan names', async () => {
    const html = await render(PlayerPersonaNoteModal, MODAL_PROPS)

    expect(html).toContain(L.save)
    expect(html).toContain(L.later)
  })

  it('lets the caller relabel the dismiss button for a non-first-open context', async () => {
    // The settings tab opens this same modal to *edit*, not to be asked for
    // the first time — "之後再說" ("maybe later") there implies a choice
    // being remembered that never happens; it just closes the window.
    const html = await render(PlayerPersonaNoteModal, {
      ...MODAL_PROPS,
      dismissLabel: zhTW.common.actions.cancel,
    })

    expect(html).toContain(zhTW.common.actions.cancel)
    expect(html).not.toContain(L.later)
  })

  it('still defaults to "maybe later" when the caller does not relabel it', async () => {
    // ChatPanel's first-open popup must keep its original wording untouched.
    const html = await render(PlayerPersonaNoteModal, MODAL_PROPS)

    expect(html).toContain(L.later)
  })

  it('pre-fills what is already stored instead of starting blank', async () => {
    const html = await render(PlayerPersonaNoteModal, {
      ...MODAL_PROPS,
      initialNote: '我是超能力者',
    })

    expect(html).toContain('我是超能力者')
  })

  it('keeps the modal open on a failed save, so the typing is not lost', () => {
    // No click simulation in this repo (see the file header) — the contract
    // is pinned on the source: the catch sets the error and never emits close.
    const src = readFileSync(
      new URL('../src/components/PlayerPersonaNoteModal.vue', import.meta.url),
      'utf8',
    )
    const catchBlock = src.slice(src.indexOf('} catch (err) {'), src.indexOf('} finally {'))

    expect(catchBlock).toContain('errorMessage.value')
    expect(catchBlock).not.toContain("emit('close')")
  })

  it('closes only after the save succeeded, and hands the stored value back', () => {
    const src = readFileSync(
      new URL('../src/components/PlayerPersonaNoteModal.vue', import.meta.url),
      'utf8',
    )
    const tryBlock = src.slice(src.indexOf('  try {'), src.indexOf('} catch (err) {'))

    expect(tryBlock).toContain('updatePlayerPersonaNote(props.characterId')
    expect(tryBlock).toContain("emit('saved'")
    expect(tryBlock).toContain("emit('close')")
  })

  it('refuses to show an editing box before the stored content is known', async () => {
    // The whole point: an empty box plus "save replaces everything" is how a
    // note the player never saw gets wiped. Unknown content ⇒ no box, and no
    // "maybe later" either (that would record a permanent dismissal for a
    // question the player was never really asked).
    const html = await render(PlayerPersonaNoteModal, {
      ...MODAL_PROPS,
      noteLoaded: false,
      loading: false,
      initialNote: '',
    })

    expect(html).toContain('persona-note-modal')
    expect(html).not.toContain('field-textarea')
    // Source comments survive SSR, so the buttons are checked on the text.
    expect(visibleText(html)).not.toContain(L.save)
    expect(visibleText(html)).not.toContain(L.later)
    expect(html).toContain(L.loadFailed)
    expect(html).toContain(zhTW.common.actions.retry)
  })

  it('says it is still reading rather than reporting a failure', async () => {
    const html = await render(PlayerPersonaNoteModal, {
      ...MODAL_PROPS,
      noteLoaded: false,
      loading: true,
    })

    expect(html).toContain(L.loading)
    expect(html).not.toContain(L.loadFailed)
    expect(html).not.toContain('field-textarea')
  })

  it('will not write while the stored content is unknown', () => {
    // Source-scanned for the same reason as the failed-save case above.
    const src = readFileSync(
      new URL('../src/components/PlayerPersonaNoteModal.vue', import.meta.url),
      'utf8',
    )
    const saveFn = src.slice(src.indexOf('async function save()'), src.indexOf('  try {'))

    expect(saveFn).toContain('if (!props.noteLoaded) return')
  })

  it('tells the other surfaces about a successful save', () => {
    // The write path is the only place that knows a save happened, so it is
    // the only place that broadcasts. Both `usePlayerPersonaNote` instances
    // (chat header, settings tab) pick it up from there.
    const src = readFileSync(
      new URL('../src/components/PlayerPersonaNoteModal.vue', import.meta.url),
      'utf8',
    )
    const tryBlock = src.slice(src.indexOf('  try {'), src.indexOf('} catch (err) {'))

    expect(tryBlock).toContain('publishPlayerPersonaNoteUpdated(props.characterId, stored)')
  })

  it('speaks all three languages', async () => {
    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const html = await render(PlayerPersonaNoteModal, MODAL_PROPS, locale as Locale)

      expect(html, locale).toContain(
        withName(catalog.playerPersonaNote.description, '芊璃'),
      )
      expect(html, locale).toContain(catalog.playerPersonaNote.later)
    }
  })

  it('discloses no operator jargon, in any language', async () => {
    for (const locale of Object.keys(CATALOGS) as Locale[]) {
      const html = await render(PlayerPersonaNoteModal, MODAL_PROPS, locale)

      expect(findJargon(visibleText(html)), locale).toEqual([])
    }
  })
})

// ----------------------------------------------------------------------
// Wiring — source-scan (ChatPanel is too large / too dependency-heavy to
// SSR-render, same reasoning as stageNudgeSurface.test.ts)
// ----------------------------------------------------------------------

function sourceOf(relative: string): string {
  return readFileSync(new URL(`../src/${relative}`, import.meta.url), 'utf8')
}

describe('ChatPanel wiring', () => {
  const src = sourceOf('components/ChatPanel.vue')

  it('puts the chip in the header, next to the character name and status', () => {
    const header = src.slice(
      src.indexOf('<div class="chat-header">'),
      src.indexOf('<div class="chat-mode-bar"'),
    )
    expect(header).toContain('<PlayerPersonaNoteChip')
    expect(header).toContain(':filled="playerPersonaNoteFilled"')
  })

  it('decides whether to prompt through the shared pure function', () => {
    expect(src).toContain('shouldPromptPlayerPersonaNote({')
    expect(src).toContain('loadingHistory: props.loadingHistory ?? false')
    expect(src).toContain('historyFailed: props.historyFailed ?? false')
    expect(src).toContain('messageCount: localMessages.value.length')
  })

  it('hands the popup rule the difference between "no messages" and "no answer"', () => {
    expect(src).toContain('historyFailed?: boolean')
  })

  it('never opens the editor on a value it has not read yet', () => {
    // The chip is dead while the GET is in flight, and pressing it after a
    // failure retries the read instead of opening a blank box.
    expect(src).toContain(':disabled="playerPersonaNoteLoading"')
    const opener = src.slice(
      src.indexOf('function openPlayerPersonaNote()'),
      src.indexOf('function dismissPlayerPersonaNote()'),
    )
    expect(opener).toContain('reloadPlayerPersonaNote()')
    expect(src).toContain(':note-loaded="playerPersonaNoteLoaded"')
    expect(src).toContain('@reload="reloadPlayerPersonaNote"')
  })

  it('re-reads the per-character dismissal when the character changes', () => {
    expect(src).toContain('isPlayerPersonaNoteDismissed(')
    expect(src).toContain('void loadPlayerPersonaNote(characterId)')
  })

  it('records "maybe later" instead of only closing the popup', () => {
    const handler = src.slice(
      src.indexOf('function dismissPlayerPersonaNote()'),
      src.indexOf('function handlePlayerPersonaNoteSaved'),
    )
    expect(handler).toContain('rememberPlayerPersonaNoteDismissed(')
    expect(handler).toContain('playerPersonaNoteOpen.value = false')
  })

  it('reaches localStorage only through the throw-safe getter', () => {
    // Private mode throws on the *property access*, so the raw global must
    // not appear at the persona-note call sites at all.
    expect(src).toContain('rememberPlayerPersonaNoteDismissed(getSafeLocalStorage()')
    expect(src).not.toContain('rememberPlayerPersonaNoteDismissed(window.localStorage')
    expect(src).not.toContain('isPlayerPersonaNoteDismissed(window.localStorage')
  })

  it('mounts the same modal the settings tab uses', () => {
    expect(src).toContain('<PlayerPersonaNoteModal')
    expect(src).toContain(':initial-note="playerPersonaNoteText"')
  })
})

describe('CharacterSettingsSection wiring', () => {
  const src = sourceOf('components/CharacterSettingsSection.vue')

  it('adds the field to the per-character settings tab', () => {
    expect(src).toContain('<PlayerPersonaNoteSetting')
    expect(src).toContain("t('playerPersonaNote.sectionTitle')")
  })

  it('remounts the field when the selected character changes', () => {
    expect(src).toContain(':key="`${character.id}:player-persona-note`"')
  })
})

describe('PlayerPersonaNoteSetting wiring', () => {
  const src = sourceOf('components/PlayerPersonaNoteSetting.vue')

  it('edits through the shared modal rather than growing a second textarea', () => {
    expect(src).toContain('<PlayerPersonaNoteModal')
    expect(src).not.toContain('field-textarea')
    expect(src).not.toContain('UiTextarea')
  })

  it('confirms a successful save in place', () => {
    expect(src).toContain("t('playerPersonaNote.saved')")
  })

  it('does not pass a failed read off as "nothing written yet"', () => {
    expect(src).toContain("t('playerPersonaNote.loadFailed')")
    expect(src).toContain('!loading.value && !loaded.value')
    expect(src).toContain(':note-loaded="loaded"')
  })

  it('relabels the dismiss button instead of reusing the first-open wording', () => {
    // Here the button only closes the window — it must not claim to record
    // a "maybe later" the settings tab never actually remembers.
    expect(src).toContain(":dismiss-label=\"t('common.actions.cancel')\"")
  })
})

describe('StagePage wiring', () => {
  const src = readFileSync(
    new URL('../src/pages/StagePage.vue', import.meta.url),
    'utf8',
  )

  it('keeps a failed history load distinguishable from an empty thread', () => {
    const loader = src.slice(
      src.indexOf('async function loadHistoryFor'),
      src.indexOf('async function handleSelectCharacter'),
    )
    // Reset on entry, raised in the catch — the flag describes *this* load,
    // so a successful retry clears it.
    expect(loader).toContain('historyFailed.value = false')
    expect(loader.slice(loader.indexOf('} catch {'))).toContain('historyFailed.value = true')
  })

  it('hands that flag to the chat panel', () => {
    expect(src).toContain(':history-failed="historyFailed"')
  })
})
