/**
 * "This plan does not include that" — said *before* the press.
 *
 * Two hosted feature switches reach the player as a refusal today:
 * `tts_enabled` answers a pressed play button with "TTS is not enabled", and
 * `album_generation_enabled` answers a pressed Generate with a 403. Both are
 * facts the SPA can read up front, so this pins the surfaces that now do.
 *
 * The three rules every assertion here is really about:
 *   1. **Off means gone, not broken.** A control that can only fail is not
 *      shown at all (voice) or is shown disabled *with the reason beside it*
 *      (generation) — never left pressable to produce an error.
 *   2. **Unknown is never "off".** The switches read `true` on self-host and
 *      whenever the limits could not be read, so a failed request cannot take
 *      a feature away from a player who has it.
 *   3. **Only the generated path is gated.** Uploading your own picture is
 *      untouched, because the server only ever gated generation.
 *
 * SSR (`createSSRApp` + `renderToString`) per the repo convention.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { computed, createSSRApp, ref, type Ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

const holder = vi.hoisted(() => ({
  ttsEnabled: null as Ref<boolean> | null,
  albumGenerationEnabled: null as Ref<boolean> | null,
  ensureLoaded: vi.fn(),
}))

const ttsEnabled = ref(true)
const albumGenerationEnabled = ref(true)
holder.ttsEnabled = ttsEnabled
holder.albumGenerationEnabled = albumGenerationEnabled

vi.mock('@/composables/useRuntimeLimits', () => ({
  useRuntimeLimits: () => ({
    supported: computed(() => true),
    ttsEnabled: computed(() => holder.ttsEnabled!.value),
    albumGenerationEnabled: computed(
      () => holder.albumGenerationEnabled!.value,
    ),
    videoGenerationEnabled: computed(() => true),
    characterSlots: computed(() => null),
    characterDailyCreates: computed(() => null),
    storyScenesDaily: computed(() => null),
    sessionMessageLimit: computed(() => null),
    characterCreationBlocked: computed(() => null),
    ensureLoaded: holder.ensureLoaded,
    refresh: vi.fn(),
    reset: vi.fn(),
  }),
}))

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    isAdmin: ref(false),
    cloudMode: ref(true),
    portalUrl: ref<string | null>(null),
    currentUser: ref<Record<string, unknown> | null>({ id: 'user-1' }),
  }),
}))

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

// Not because anything here fetches: importing it pulls in the real router
// module, which builds a browser history at import time.
vi.mock('@/utils/authedFetch', () => ({ authedFetch: vi.fn() }))

vi.mock('@/composables/useCloudCredits', () => ({
  refreshCloudCreditsAfterAction: vi.fn(),
  useCloudCredits: () => ({
    total: computed(() => 0),
    hasBalance: computed(() => false),
    stale: computed(() => false),
  }),
}))

vi.mock('@/utils/api/observability', () => ({
  updateTurnOperatorFeedback: vi.fn(),
}))

vi.mock('@/utils/api/tts', () => ({
  synthesizeCharacterTTS: vi.fn(),
  TTSDisabledError: class TTSDisabledError extends Error {},
}))

vi.mock('@/utils/api/characters', () => ({
  commitPortraitCandidates: vi.fn(),
  deleteCharacterImage: vi.fn(),
  generatePortraitCandidates: vi.fn(),
  reorderCharacterImages: vi.fn(),
  uploadCharacterImage: vi.fn(),
}))

vi.mock('@/utils/api/album', () => ({ transferStageToAlbum: vi.fn() }))

const ChatBubble = (await import('@/components/ChatBubble.vue')).default
const CharacterImagesPanel
  = (await import('@/components/CharacterImagesPanel.vue')).default

const CATALOGS = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP } as const
type Locale = keyof typeof CATALOGS

async function render(
  component: unknown,
  props: Record<string, unknown>,
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

const ASSISTANT_MESSAGE = {
  role: 'assistant',
  content: '晚安。今天過得還好嗎？',
  attachments: [],
  turn_record_id: null,
  kind: 'chat',
}

const CHARACTER = {
  id: 'char-1',
  name: '芊璃',
  image_urls: [] as string[],
}

// Both switches start where the composable starts them: enabled. A test that
// inherited "off" from its predecessor would be asserting the wrong world.
beforeEach(() => {
  ttsEnabled.value = true
  albumGenerationEnabled.value = true
})

describe('voice playback when the plan has no voice', () => {
  it('offers to read a reply aloud when voice is usable', async () => {
    const html = await render(ChatBubble, {
      message: ASSISTANT_MESSAGE,
      characterId: CHARACTER.id,
      ttsAvailable: true,
    })

    expect(html).toContain('bubble-tts')
    expect(html).toContain(zhTW.chat.bubble.ttsPlay)
  })

  it('renders no voice control at all when voice is not usable', async () => {
    // Not a disabled button, not a "voice is off" line: nothing. The panel
    // hands down `ttsAvailable && ttsEnabled`, so a hosted plan without
    // voice looks like a deployment without a voice channel — which is what
    // it is, from where the player sits.
    const html = await render(ChatBubble, {
      message: ASSISTANT_MESSAGE,
      characterId: CHARACTER.id,
      ttsAvailable: false,
    })

    expect(html).not.toContain('bubble-tts')
    expect(html).not.toContain(zhTW.chat.bubble.ttsUnavailable)
    // ...and the reply itself is untouched.
    expect(html).toContain('晚安。')
  })

  it('feeds the bubbles the plan switch, not just the channel probe', async () => {
    // A source scan rather than a render: `ChatPanel` is too large to mount
    // here, and the failure this guards is precisely someone adding a third
    // `<ChatBubble>` bound to the raw channel probe — which would put a play
    // button back on a plan that has no voice.
    const source = readFileSync(
      fileURLToPath(new URL('../src/components/ChatPanel.vue', import.meta.url)),
      'utf8',
    )

    expect(source).toContain(':tts-available="ttsUsable"')
    expect(source).not.toContain(':tts-available="ttsAvailable"')
    expect(source).toMatch(/ttsUsable[\s\S]{0,200}runtimeLimits\.ttsEnabled/)
  })
})

describe('picture generation when the plan does not include it', () => {
  it('says so before the press', async () => {
    albumGenerationEnabled.value = false

    const html = await render(CharacterImagesPanel, { character: CHARACTER })

    expect(html).toContain(
      zhTW.characterImagesPanel.generate.disabledNotice,
    )
    expect(html).toContain('generate-disabled-note')
  })

  it('withholds the Generate button itself', async () => {
    // Not observable in the initial markup — the button is already disabled
    // by the empty description box, so both states render the same
    // attribute. The binding is what is worth pinning: the notice without
    // the disable would be a sentence next to a button that still fails.
    const source = readFileSync(
      fileURLToPath(new URL(
        '../src/components/CharacterImagesPanel.vue', import.meta.url,
      )),
      'utf8',
    )

    const generateButton = source.match(
      /<UiButton[\s\S]*?@click="handleGenerate"[\s\S]*?<\/UiButton>/,
    )?.[0]

    expect(generateButton).toBeTruthy()
    expect(generateButton).toContain('albumGenerationBlocked')
  })

  it('leaves uploading your own picture alone', async () => {
    // The server gates generation only. Taking the upload away would
    // remove something the player still has.
    albumGenerationEnabled.value = false

    const html = await render(CharacterImagesPanel, { character: CHARACTER })

    expect(html).toContain(zhTW.characterImagesPanel.actions.upload)
    expect(html).toContain('type="file"')
  })

  it('renders no notice when generation is allowed', async () => {
    // Self-host, an unread snapshot and an enabled plan all land here, and
    // all three must be byte-identical to before this gate existed.
    albumGenerationEnabled.value = true

    const html = await render(CharacterImagesPanel, { character: CHARACTER })

    expect(html).not.toContain('generate-disabled-note')
    expect(html).not.toContain(
      zhTW.characterImagesPanel.generate.disabledNotice,
    )
  })

  it('speaks all three languages', async () => {
    albumGenerationEnabled.value = false

    for (const [locale, catalog] of Object.entries(CATALOGS)) {
      const html = await render(
        CharacterImagesPanel, { character: CHARACTER }, locale as Locale,
      )

      expect(html, locale)
        .toContain(catalog.characterImagesPanel.generate.disabledNotice)
    }
  })
})
