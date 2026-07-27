import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, ref, type Ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import {
  COMPONENT_JARGON_ALLOWLIST,
  allowlistForKey,
  findJargon,
} from './fixtures/cloud-jargon-denylist'

/**
 * Dual-mode rendering guard for plan U1.
 *
 * Two promises are asserted for every surface that grew a hosted wording:
 *   1. hosted output contains none of the operator jargon the plan removes;
 *   2. self-host output still contains exactly the original strings — the
 *      "byte-level zero change" red line, checked against the catalogue so
 *      it cannot drift with a future copy edit.
 *
 * The named assertions below say *what each screen should say*; the
 * sweep at the bottom says *what no screen may say*, so new copy is
 * covered without anyone remembering to extend this file.
 *
 * No DOM test infra exists in this repo (@vue/test-utils / jsdom are not
 * installed), so coverage is via SSR — the same pattern as
 * cloudCreditsSurface.test.ts.
 */
// A real ref, not a `{ value }` stand-in: templates only auto-unwrap actual
// refs, and `v-if="!cloudMode"` would read a plain object as always truthy.
const holder = vi.hoisted(() => ({
  cloudMode: null as Ref<boolean> | null,
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

const cloudMode = ref(true)
holder.cloudMode = cloudMode
holder.portalUrl = ref<string | null>(null)
holder.currentUser = ref<Record<string, unknown> | null>(null)

// `authedFetch` pulls in the router (and therefore `window`) at import time;
// nothing here issues a request, so stub the transport rather than the many
// api modules that sit on top of it.
vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}))

vi.mock('@/utils/api/album', () => ({
  listAlbum: vi.fn(async () => ({ items: [] })),
  deleteAlbumItem: vi.fn(),
  promoteAlbumToStage: vi.fn(),
  transferStageToAlbum: vi.fn(),
}))

vi.mock('@/utils/api/story', () => ({
  createStorySeed: vi.fn(),
  deleteStorySeed: vi.fn(),
  listStoryEvents: vi.fn(async () => []),
  listStorySeeds: vi.fn(async () => []),
  rollStoryEvent: vi.fn(),
  updateStorySeed: vi.fn(),
}))

vi.mock('@/utils/api/storyArc', () => ({
  abandonStoryArc: vi.fn(),
  addStoryArcBeat: vi.fn(),
  deleteStoryArcBeat: vi.fn(),
  getActiveStoryArc: vi.fn(async () => null),
  listStoryArcs: vi.fn(async () => []),
  regenerateStoryArc: vi.fn(),
  startStoryArc: vi.fn(),
  updateStoryArcBeat: vi.fn(),
  updateStoryArcMeta: vi.fn(),
}))

vi.mock('@/utils/api/characters', () => ({
  commitPortraitCandidates: vi.fn(),
  deleteCharacterImage: vi.fn(),
  generatePortraitCandidates: vi.fn(),
  reorderCharacterImages: vi.fn(),
  uploadCharacterImage: vi.fn(),
}))

vi.mock('@/utils/api/messaging', () => ({
  createAccount: vi.fn(),
  createBinding: vi.fn(),
  deleteAccount: vi.fn(),
  deleteBinding: vi.fn(),
  getMessagingSettings: vi.fn(async () => ({
    public_base_url: '',
    effective_public_base_url: 'https://play.yuralume.example',
    source: 'env',
    telegram_delivery_mode: 'polling',
  })),
  getWebhookStatus: vi.fn(),
  listAccounts: vi.fn(async () => []),
  listBindings: vi.fn(async () => []),
  registerWebhook: vi.fn(),
  setBindingAcceptsProactive: vi.fn(),
  setBindingEnabled: vi.fn(),
  updateAccount: vi.fn(),
  whatsappQrSvgUrl: vi.fn(() => '/api/v1/messaging/whatsapp/qr'),
}))

vi.mock('@/utils/api/proactive', () => ({
  evaluateProactiveNow: vi.fn(),
  listProactiveAttempts: vi.fn(async () => []),
}))

const CharacterImagesPanel
  = (await import('@/components/CharacterImagesPanel.vue')).default
const AlbumPanel = (await import('@/components/AlbumPanel.vue')).default
const StoryPanel = (await import('@/components/StoryPanel.vue')).default
const ChannelBindingsPanel
  = (await import('@/components/ChannelBindingsPanel.vue')).default
const ChannelSetupGuide
  = (await import('@/components/ChannelSetupGuide.vue')).default
const ChannelAccountNextStep
  = (await import('@/components/ChannelAccountNextStep.vue')).default

const L = zhTW

const CHARACTER = {
  id: 'char-1',
  name: 'Yura',
  image_urls: [],
  allowed_tools: [],
} as unknown as Record<string, unknown>

const PLATFORMS = ['telegram', 'line', 'discord', 'whatsapp'] as const

function account(platform: (typeof PLATFORMS)[number]) {
  return {
    id: `acc-${platform}`,
    character_id: 'char-1',
    platform,
    display_name: 'Yura',
    enabled: true,
    allowed_sender_refs: [],
    webhook_slug: 'slug',
  } as unknown as Record<string, unknown>
}

async function render(
  component: unknown,
  props: Record<string, unknown> = {},
): Promise<string> {
  const app = createSSRApp(
    component as Parameters<typeof createSSRApp>[0],
    props,
  )
  app.use(createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  }))
  return renderToString(app)
}

/**
 * Rendered text only — markup and dev comments are stripped so a jargon
 * assertion cannot be satisfied (or defeated) by a CSS class name like
 * `arc-panel`, which no player ever reads.
 */
function visibleText(html: string): string {
  return html
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<[^>]*>/g, ' ')
}

beforeEach(() => {
  cloudMode.value = true
})

describe('CharacterImagesPanel generation hint', () => {
  it('drops the image-channel plumbing for hosted players', async () => {
    const html = await render(CharacterImagesPanel, {
      character: CHARACTER,
      showTechnicalHints: false,
    })

    expect(html).toContain(L.characterImagesPanel.generate.hintCloud)
    expect(html).not.toContain('ComfyUI')
    expect(html).not.toContain('LoRA')
    expect(html).not.toContain('OpenAI')
    expect(html).not.toContain('profile')
  })

  it('keeps the operator wording by default (self-host parity)', async () => {
    const html = await render(CharacterImagesPanel, { character: CHARACTER })

    expect(html).toContain(L.characterImagesPanel.generate.hint)
    expect(html).toContain('ComfyUI')
  })
})

describe('AlbumPanel hint', () => {
  it('says what the album is, not how images got there, when hosted', async () => {
    const html = await render(AlbumPanel, { characterId: 'char-1' })

    expect(html).toContain(L.albumPanel.hintCloud)
    expect(html).not.toContain(L.albumPanel.hint)
  })

  it('is unchanged on self-host', async () => {
    cloudMode.value = false

    const html = await render(AlbumPanel, { characterId: 'char-1' })

    expect(html).toContain(L.albumPanel.hint)
    expect(html).not.toContain(L.albumPanel.hintCloud)
  })
})

describe('StoryPanel seed library', () => {
  const props = { characterId: 'char-1', worldFrame: 'modern' }

  it('hides the authoring-only seed library from hosted players', async () => {
    const html = await render(StoryPanel, props)

    expect(html).not.toContain(L.story.panel.seedsTitle)
    expect(html).not.toContain('import_story_seeds')
    expect(html).not.toContain('world_frames')
    // ...and the surviving daily-story blurb is the plain-language one.
    expect(html).toContain(L.story.panel.hintCloud)
    expect(html).not.toContain('episodic')
    expect(html).not.toContain('gacha')
    // The nested arc panel renders here too, so its vocabulary is covered
    // by the same pass.
    expect(html).toContain(L.story.arcPanel.hintCloud)
    expect(html).toContain(L.story.arcPanel.template.unboundCloud)
    const text = visibleText(html)
    expect(text).not.toContain('beat')
    expect(text).not.toContain('LLM')
    expect(text).not.toContain('arc')
  })

  it('keeps the seed library and its wording on self-host', async () => {
    cloudMode.value = false

    const html = await render(StoryPanel, props)

    expect(html).toContain(L.story.panel.seedsTitle)
    expect(html).toContain('import_story_seeds')
    expect(html).toContain(L.story.panel.hint)
    expect(html).toContain(L.story.arcPanel.hint)
    expect(html).toContain(L.story.arcPanel.template.unbound)
  })
})

// ----------------------------------------------------------------------
// U1-C — external message channels: kept, but lowered
// ----------------------------------------------------------------------
//
// Owner decision 2026-07-26: hosted keeps the self-bind surface, because
// hiding it would strand every player outside LINE's cultural reach.
// What changes is the *height of the step*, not its existence.

describe('ChannelSetupGuide (hosted)', () => {
  it('replaces the four site-mode lines with one managed-connection line', async () => {
    for (const platform of PLATFORMS) {
      const html = await render(ChannelSetupGuide, {
        platform,
        telegramDeliveryMode: 'polling',
        effectivePublicBaseUrl: 'https://play.yuralume.example',
      })

      expect(html).toContain(L.channelSetupGuide.telegramModeCloud)
      expect(visibleText(html)).not.toContain('Gateway WebSocket')
      expect(visibleText(html)).not.toContain('Baileys')
    }
  })

  it('links each platform to its own official console', async () => {
    const expected: Record<string, string> = {
      telegram: 't.me/BotFather',
      line: 'developers.line.biz',
      discord: 'discord.com/developers',
      whatsapp: 'faq.whatsapp.com',
    }

    for (const platform of PLATFORMS) {
      const html = await render(ChannelSetupGuide, {
        platform,
        telegramDeliveryMode: 'polling',
      })

      expect(html).toContain(expected[platform])
      expect(html).toContain(L.channelSetupGuide.consoleLinks[platform])
    }
  })

  it('drops the informational site notes but keeps the actionable warning', async () => {
    const ready = await render(ChannelSetupGuide, {
      platform: 'discord',
      telegramDeliveryMode: 'polling',
      effectivePublicBaseUrl: 'https://play.yuralume.example',
    })
    expect(ready).not.toContain(L.channelSetupGuide.notes.discordGateway)

    const missing = await render(ChannelSetupGuide, {
      platform: 'line',
      telegramDeliveryMode: 'polling',
      effectivePublicBaseUrl: '',
    })
    expect(missing).toContain(L.channelSetupGuide.notes.webhookMissingCloud)
    expect(missing).not.toContain(L.channelSetupGuide.notes.webhookMissing)
  })

  it('keeps the platform-native credential names', async () => {
    // Translating "Bot token" leaves the player hunting for a field that
    // Telegram labels in English. Native names survive by design.
    const html = await render(ChannelSetupGuide, {
      platform: 'telegram',
      telegramDeliveryMode: 'polling',
    })

    expect(html).toContain('Bot token')
  })
})

describe('ChannelSetupGuide (self-host parity)', () => {
  it('renders the operator wording, the site mode, and no console links', async () => {
    cloudMode.value = false

    const telegram = await render(ChannelSetupGuide, {
      platform: 'telegram',
      telegramDeliveryMode: 'polling',
    })
    expect(telegram).toContain(L.channelSetupGuide.steps.telegramPollingFirstMessage)
    expect(telegram).toContain(L.channelSetupGuide.notes.telegramPolling)
    expect(telegram).not.toContain('t.me/BotFather')

    const discord = await render(ChannelSetupGuide, {
      platform: 'discord',
      telegramDeliveryMode: 'polling',
    })
    expect(discord).toContain(L.channelSetupGuide.discordMode)
    expect(discord).toContain(L.channelSetupGuide.notes.discordGateway)

    const whatsapp = await render(ChannelSetupGuide, {
      platform: 'whatsapp',
      telegramDeliveryMode: 'polling',
    })
    expect(whatsapp).toContain(L.channelSetupGuide.whatsappMode)
    expect(whatsapp).toContain(L.channelSetupGuide.steps.whatsappStartSidecar)

    const lineReady = await render(ChannelSetupGuide, {
      platform: 'line',
      telegramDeliveryMode: 'polling',
      effectivePublicBaseUrl: 'https://tunnel.example',
    })
    expect(lineReady).toContain(L.channelSetupGuide.lineMode)
    expect(lineReady).toContain('https://tunnel.example')
  })
})

describe('ChannelAccountNextStep', () => {
  it('tells the hosted player what to do without naming the transport', async () => {
    const html = await render(ChannelAccountNextStep, {
      account: account('discord'),
      telegramDeliveryMode: 'polling',
    })

    expect(html).toContain(L.channelAccountNextStep.pending.discordGatewayCloud)
    expect(visibleText(html)).not.toContain('Gateway')
  })

  it('hides the "ask an admin" dead end from hosted players', async () => {
    const html = await render(ChannelAccountNextStep, {
      account: account('line'),
      telegramDeliveryMode: 'polling',
      effectivePublicBaseUrl: '',
    })

    expect(html).toContain(L.channelAccountNextStep.pending.lineWebhookMissingCloud)
    expect(html).not.toContain(L.channelAccountNextStep.pending.lineWebhookMissing)
  })

  it('is unchanged on self-host', async () => {
    cloudMode.value = false

    const html = await render(ChannelAccountNextStep, {
      account: account('whatsapp'),
      telegramDeliveryMode: 'polling',
    })

    expect(html).toContain(L.channelAccountNextStep.pending.whatsappGateway)
  })
})

describe('ChannelBindingsPanel', () => {
  const props = { characterId: 'char-1' }

  it('leads with the plain-language framing when hosted', async () => {
    const html = await render(ChannelBindingsPanel, props)

    expect(html).toContain(L.channelBindingsPanel.accounts.hintCloud)
    expect(html).not.toContain(L.channelBindingsPanel.accounts.hint)
  })

  it('is unchanged on self-host', async () => {
    cloudMode.value = false

    const html = await render(ChannelBindingsPanel, props)

    expect(html).toContain(L.channelBindingsPanel.accounts.hint)
    expect(html).not.toContain(L.channelBindingsPanel.accounts.hintCloud)
  })
})

// ----------------------------------------------------------------------
// U1-F — the sweep
// ----------------------------------------------------------------------

interface Surface {
  name: string
  render: () => Promise<string>
}

const SURFACES: Surface[] = [
  {
    name: 'CharacterImagesPanel',
    render: () => render(CharacterImagesPanel, {
      character: CHARACTER,
      showTechnicalHints: false,
    }),
  },
  { name: 'AlbumPanel', render: () => render(AlbumPanel, { characterId: 'char-1' }) },
  {
    name: 'StoryPanel',
    render: () => render(StoryPanel, {
      characterId: 'char-1',
      worldFrame: 'modern',
    }),
  },
  {
    name: 'ChannelBindingsPanel',
    render: () => render(ChannelBindingsPanel, { characterId: 'char-1' }),
  },
  ...PLATFORMS.map(platform => ({
    name: 'ChannelSetupGuide',
    render: () => render(ChannelSetupGuide, {
      platform,
      telegramDeliveryMode: 'polling' as const,
      effectivePublicBaseUrl: 'https://play.yuralume.example',
    }),
  })),
  ...PLATFORMS.map(platform => ({
    name: 'ChannelAccountNextStep',
    render: () => render(ChannelAccountNextStep, {
      account: account(platform),
      telegramDeliveryMode: 'polling' as const,
      effectivePublicBaseUrl: 'https://play.yuralume.example',
    }),
  })),
]

describe('hosted jargon sweep', () => {
  it.each(SURFACES)('$name renders no denied term', async ({ name, render: run }) => {
    const text = visibleText(await run())

    expect(findJargon(text, COMPONENT_JARGON_ALLOWLIST[name])).toEqual([])
  })

  it('has teeth: the same scanner still flags the self-host wording', async () => {
    // A denylist that matches nothing passes every screen forever. The
    // self-host story panel is the control sample — its operator copy is
    // pinned above, so this stays meaningful.
    cloudMode.value = false

    const text = visibleText(await render(StoryPanel, {
      characterId: 'char-1',
      worldFrame: 'modern',
    }))

    expect(findJargon(text)).toContain('LLM')
  })

  it('has teeth: the channel allowlist is load-bearing, not decorative', async () => {
    // If the panel ever stopped showing the platform's own credential
    // names, the exemption would be masking nothing and should go.
    const text = visibleText(await render(ChannelSetupGuide, {
      platform: 'telegram',
      telegramDeliveryMode: 'polling',
    }))

    expect(findJargon(text)).toContain('token')
  })
})

function flattenCatalog(
  value: unknown,
  prefix = '',
): Array<[string, string]> {
  if (typeof value === 'string') return [[prefix, value]]
  if (!value || typeof value !== 'object') return []
  return Object.entries(value as Record<string, unknown>).flatMap(
    ([key, child]) => flattenCatalog(child, prefix ? `${prefix}.${key}` : key),
  )
}

describe('hosted copy catalogue', () => {
  it('has no denied term in any *Cloud variant', () => {
    // Catches hosted copy no test renders yet — a create form behind a
    // click, an error branch behind a failure — so the guard does not
    // silently shrink to whatever happens to be mounted today.
    const offenders = flattenCatalog(zhTW)
      .filter(([key]) => key.endsWith('Cloud'))
      .flatMap(([key, value]) => {
        const found = findJargon(value, allowlistForKey(key))
        return found.length > 0 ? [`${key}: ${found.join(', ')}`] : []
      })

    expect(offenders).toEqual([])
  })
})
