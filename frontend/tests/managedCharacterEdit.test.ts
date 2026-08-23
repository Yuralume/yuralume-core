/**
 * EC2-B — front-end half of the managed (IP-partner) character write
 * boundary. `character.managed` only exists (and is only ever `true`) on
 * a hosted character with `origin_official_card_id` set server-side
 * (EC2-A); this file pins that the edit surfaces react to it and that an
 * ordinary character — `managed` absent, the only shape self-host ever
 * produces — renders and saves byte-for-byte as before.
 *
 * No DOM test infra exists in this repo (@vue/test-utils / jsdom are not
 * installed), so component coverage is via SSR (`createSSRApp` +
 * `renderToString`), the same pattern as cloudPlayerJargon.test.ts /
 * imageStage.test.ts. `buildManagedAwareUpdateRequest` is a plain
 * function export (no SSR needed) covering the `handleSave` payload
 * shape directly.
 */
import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'

// `usePlayerCopy` -> `useAuth` reads `localStorage` at module scope,
// which does not exist in this repo's jsdom-less SSR test environment.
// Mocked the same way cloudPlayerJargon.test.ts does it.
vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    cloudMode: ref(false),
    portalUrl: ref<string | null>(null),
    currentUser: ref<Record<string, unknown> | null>(null),
  }),
}))

// `authedFetch` pulls in the router (and therefore `window`) at import
// time; nothing here issues a request, so stub the transport rather than
// the api modules that sit on top of it. Same fix as cloudPlayerJargon.test.ts.
vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}))
import type {
  Character,
  CharacterCompanion,
  CharacterPersonalityType,
  CharacterState,
} from '@/types/character'
import {
  buildManagedAwareUpdateRequest,
  type CharacterEditPersonaFormFields,
} from '@/utils/characterEditRequest'

// CharacterEditPanel mounts CharacterRelationshipsPanel unconditionally
// (managed or not — "known characters" is out of EC2-B's scope, see the
// plan), which fires a `watch(..., { immediate: true })` API call the
// moment it is set up. Stub the whole surface so SSR does not attempt a
// real network call for behaviour this file is not about.
vi.mock('@/utils/api/characters', () => ({
  generateCompanions: vi.fn(async () => []),
  resetCharacterData: vi.fn(),
  updateCharacter: vi.fn(),
  updateCharacterProactiveRhythm: vi.fn(),
  listCharacterRelationships: vi.fn(async () => []),
  listCharacterEncounters: vi.fn(async () => []),
  createCharacterRelationship: vi.fn(),
  updateCharacterRelationship: vi.fn(),
  tickCharacterEncounters: vi.fn(),
  downloadCharacterCard: vi.fn(),
  importCharacterCard: vi.fn(),
  installCharacterCard: vi.fn(),
  listCharacterCards: vi.fn(async () => ({ cards: [], official_cards_unavailable: false })),
  previewCharacterCard: vi.fn(),
  previewCharacterCardPack: vi.fn(),
}))

vi.mock('@/utils/api/tools', () => ({
  listTools: vi.fn(async () => []),
}))

vi.mock('@/utils/api/ttsAssets', () => ({
  listTTSAssets: vi.fn(async () => ({ voice_presets: [] })),
}))

// InitialRelationshipSettingsEditor (mounted unconditionally by
// CharacterSettingsSection since IR2) fires the same kind of immediate
// watch-driven GET the moment it is set up.
vi.mock('@/utils/api/initialRelationship', () => ({
  getInitialRelationship: vi.fn(async () => ({ has_seed: false })),
  updateInitialRelationship: vi.fn(),
}))

// StoryArcPanel (mounted unconditionally by admin/CharacterCardEditor —
// arc binding is out of EC2-B's scope) fires a `watch(..., { immediate:
// true })` active-arc fetch at setup.
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

// ChannelBindingsPanel (mounted unconditionally by CharacterSettingsSection,
// managed or not — channel bindings are out of EC2-B's scope) fires a
// `watch(..., { immediate: true })` messaging-settings fetch at setup.
vi.mock('@/utils/api/messaging', () => ({
  createAccount: vi.fn(),
  createBinding: vi.fn(),
  deleteAccount: vi.fn(),
  deleteBinding: vi.fn(),
  getMessagingSettings: vi.fn(async () => ({
    public_base_url: '',
    effective_public_base_url: '',
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

// PlayerCharacterCardPanel always mounts CharacterCardGalleryModal (twice)
// regardless of `visible` — its own immediate watcher touches `window`,
// which does not exist in this repo's jsdom-less SSR test environment.
// That is a pre-existing gap unrelated to EC2-B; stub the modal (and its
// siblings, none of which this ticket touches) so the export-button
// gating this ticket *does* own can be asserted without tripping it.
vi.mock('@/components/CharacterCardGalleryModal.vue', () => ({
  default: { template: '<div />' },
}))
vi.mock('@/components/CharacterBackupRestorePanel.vue', () => ({
  default: { template: '<div />' },
}))
vi.mock('@/components/InitialRelationshipWizardModal.vue', () => ({
  default: { template: '<div />' },
}))
vi.mock('@/components/CharacterLimitAdvisory.vue', () => ({
  default: { template: '<div />' },
}))
// CharacterBackupPanel -> CharacterBackupPasswordModal has the same
// unconditional `window`-touching immediate watcher, unrelated to EC2-B
// (that panel's managed-character gating is EC3's ticket, not this one).
vi.mock('@/components/CharacterBackupPanel.vue', () => ({
  default: { template: '<div />' },
}))

const CharacterEditPanel = (await import('@/components/CharacterEditPanel.vue')).default
const PlayerCharacterCardPanel = (await import('@/components/PlayerCharacterCardPanel.vue')).default
const CharacterSettingsSection = (await import('@/components/CharacterSettingsSection.vue')).default
const CharacterCardEditor = (await import('@/components/admin/CharacterCardEditor.vue')).default

const L = zhTW.characterEdit
const CARD_L = zhTW.playerSidebar.characterCards

async function render(
  component: unknown,
  props: Record<string, unknown>,
): Promise<string> {
  const app = createSSRApp(component as Parameters<typeof createSSRApp>[0], props)
  app.use(createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  }))
  return renderToString(app)
}

/** Enough of a `Character` for the edit panel / card panel to render. */
function character(overrides: Partial<Character> = {}): Character {
  return {
    id: 'char-1',
    name: '芊璃',
    summary: '摘要',
    personality: ['溫柔'],
    interests: ['閱讀'],
    speaking_style: '輕聲細語',
    boundaries: [],
    aspirations: [],
    appearance: '長髮',
    gender_identity: '',
    third_person_pronoun: '',
    visual_gender_presentation: '',
    visual_subject_type: 'auto',
    visual_generation_style: '',
    date_of_birth: null,
    image_urls: [],
    allowed_tools: [],
    loras: [],
    state: {
      emotion: '平靜',
      affection: 50,
      fatigue: 0,
      trust: 50,
      energy: 100,
      current_intent: null,
    },
    proactive_enabled: true,
    proactive_daily_limit: 3,
    proactive_cooldown_minutes: 60,
    proactive_rhythm: 'balanced',
    feed_daily_limit: 3,
    world_awareness_enabled: false,
    world_topics: [],
    subscribed_categories: [],
    excluded_topics: [],
    world_frame: '',
    accepts_web_proactive: true,
    unread_proactive_count: 0,
    unread_feed_reply_count: 0,
    arc_template_id: null,
    arc_series_id: null,
    voice_profile: null,
    companions: [],
    disposition: {
      self_centeredness: 'medium',
      candor: 'medium',
      sharing_drive: 'medium',
      associativeness: 'medium',
    },
    personality_type: {
      system: 'mbti_16',
      code: '',
      source: 'unset',
      confidence: 0,
      rationale: '',
      consistency_notes: [],
    },
    operator_pace_preference: '',
    ...overrides,
  }
}

// ----------------------------------------------------------------------
// buildManagedAwareUpdateRequest — the handleSave payload contract
// ----------------------------------------------------------------------

describe('buildManagedAwareUpdateRequest', () => {
  const form: CharacterEditPersonaFormFields = {
    name: '芊璃',
    summary: '摘要',
    personality: '溫柔, 直率',
    interests: '閱讀, 散步',
    speaking_style: '輕聲細語',
    boundaries: '不談政治',
    aspirations: '環遊世界',
    appearance: '長髮及腰',
    gender_identity: 'female',
    third_person_pronoun: 'she',
    visual_gender_presentation: 'feminine',
    visual_subject_type: 'human',
    visual_generation_style: 'anime',
    date_of_birth: '2000-01-01',
  }
  const state: CharacterState = {
    emotion: '平靜',
    affection: 60,
    fatigue: 10,
    trust: 70,
    energy: 90,
    current_intent: null,
  }
  const companions: CharacterCompanion[] = [
    {
      id: null,
      name: '小明',
      role: '同事',
      brief_profile: '',
      personality_sketch: [],
      relationship_snippet: '',
    },
    // A blank draft row — dropped either way, managed or not.
    { id: null, name: '  ', role: '', brief_profile: '', personality_sketch: [], relationship_snippet: '' },
  ]
  const personalityType: CharacterPersonalityType = {
    system: 'mbti_16',
    code: 'INFP',
    source: 'user_explicit',
    confidence: 1,
    rationale: 'r',
    consistency_notes: [],
  }

  it('sends every persona field for an ordinary character — zero behaviour change', () => {
    const req = buildManagedAwareUpdateRequest({
      form,
      state,
      companions,
      personalityType,
      isManaged: false,
      allowedTools: null,
    })

    expect(req.name).toBe('芊璃')
    expect(req.summary).toBe('摘要')
    expect(req.personality).toEqual(['溫柔', '直率'])
    expect(req.interests).toEqual(['閱讀', '散步'])
    expect(req.speaking_style).toBe('輕聲細語')
    expect(req.boundaries).toEqual(['不談政治'])
    expect(req.aspirations).toEqual(['環遊世界'])
    expect(req.appearance).toBe('長髮及腰')
    expect(req.gender_identity).toBe('female')
    expect(req.visual_subject_type).toBe('human')
    expect(req.date_of_birth).toBe('2000-01-01')
    expect(req.personality_type).toEqual(personalityType)
    // Writable fields travel unconditionally.
    expect(req.state).toEqual(state)
    expect(req.companions).toHaveLength(1)
    expect(req.companions?.[0].name).toBe('小明')
  })

  it('omits every protected field for a managed character — only the writable half goes out', () => {
    const req = buildManagedAwareUpdateRequest({
      form,
      state,
      companions,
      personalityType,
      isManaged: true,
      allowedTools: null,
    })

    // The exact set MANAGED_WRITABLE_UPDATE_FIELDS allows through unrelated
    // to persona: state and companions. Everything the licensor owns must
    // be `undefined` on the wire — not blanked, not present at all — so a
    // masked-but-present key can never be mistaken for "player cleared it".
    expect(req.name).toBeUndefined()
    expect(req.summary).toBeUndefined()
    expect(req.personality).toBeUndefined()
    expect(req.interests).toBeUndefined()
    expect(req.speaking_style).toBeUndefined()
    expect(req.boundaries).toBeUndefined()
    expect(req.aspirations).toBeUndefined()
    expect(req.appearance).toBeUndefined()
    expect(req.gender_identity).toBeUndefined()
    expect(req.third_person_pronoun).toBeUndefined()
    expect(req.visual_gender_presentation).toBeUndefined()
    expect(req.visual_subject_type).toBeUndefined()
    expect(req.visual_generation_style).toBeUndefined()
    expect(req.date_of_birth).toBeUndefined()
    expect(req.personality_type).toBeUndefined()

    expect(req.state).toEqual(state)
    expect(req.companions).toHaveLength(1)
    expect(Object.keys(req)).toEqual(['state', 'companions'])
  })

  it('carries allowed_tools when the caller supplies it, managed or not', () => {
    const managedReq = buildManagedAwareUpdateRequest({
      form, state, companions, personalityType,
      isManaged: true,
      allowedTools: ['generate_image'],
    })
    expect(managedReq.allowed_tools).toEqual(['generate_image'])

    const ordinaryReq = buildManagedAwareUpdateRequest({
      form, state, companions, personalityType,
      isManaged: false,
      allowedTools: null,
    })
    expect(ordinaryReq.allowed_tools).toBeUndefined()
  })
})

// ----------------------------------------------------------------------
// CharacterEditPanel — section gating
// ----------------------------------------------------------------------

describe('CharacterEditPanel managed gating', () => {
  it('hides the whole persona section and shows the licensed-character notice', async () => {
    const html = await render(CharacterEditPanel, {
      character: character({ managed: true }),
      showAdminLinks: false,
    })

    expect(html).toContain(L.managed.badge)
    expect(html).toContain(L.managed.notice)
    expect(html).not.toContain(L.sections.basic)
    expect(html).not.toContain(L.basic.birthdayLabel)
    // The persona fields themselves — not just the section title — must be
    // gone, since these are exactly the fields the licensor owns. Asserting
    // on the field placeholders (not labels) avoids a false pass from the
    // unconditional identity-drift banner, which also happens to say "名稱".
    expect(html).not.toContain(zhTW.characterCreate.fields.name.placeholder)
    expect(html).not.toContain(zhTW.characterCreate.fields.appearance.placeholder)
    expect(html).not.toContain(zhTW.characterCreate.fields.personality.placeholder)
  })

  it('keeps companions and state editable — the player-owned half', async () => {
    const html = await render(CharacterEditPanel, {
      character: character({ managed: true, companions: [{
        id: 'npc-1', name: '小明', role: '同事', brief_profile: '', personality_sketch: [], relationship_snippet: '',
      }] }),
      showAdminLinks: false,
      showStateSettings: true,
    })

    expect(html).toContain(L.sections.state)
    expect(html).toContain('小明')
  })

  it('renders the full persona section for an ordinary character — self-host / non-managed parity', async () => {
    const html = await render(CharacterEditPanel, {
      character: character(),
      showAdminLinks: false,
    })

    expect(html).not.toContain(L.managed.badge)
    expect(html).toContain(L.sections.basic)
    expect(html).toContain(L.basic.birthdayLabel)
  })

  it('treats managed: false the same as an absent key', async () => {
    const html = await render(CharacterEditPanel, {
      character: character({ managed: false }),
      showAdminLinks: false,
    })

    expect(html).not.toContain(L.managed.badge)
    expect(html).toContain(L.sections.basic)
  })

  it('hides the appearance-trigger explainer for managed characters', async () => {
    const html = await render(CharacterEditPanel, {
      character: character({ managed: true }),
      showAdminLinks: false,
      showImageTriggerInfo: true,
    })

    expect(html).not.toContain(L.sections.imageTrigger)
  })
})

// ----------------------------------------------------------------------
// PlayerCharacterCardPanel — `.lumecard` export entrance
// ----------------------------------------------------------------------

describe('PlayerCharacterCardPanel managed gating', () => {
  it('hides the export button for a managed character', async () => {
    const html = await render(PlayerCharacterCardPanel, {
      selectedCharacter: character({ managed: true }),
    })

    expect(html).not.toContain(CARD_L.exportAction)
  })

  it('keeps the export button for an ordinary selected character', async () => {
    const html = await render(PlayerCharacterCardPanel, {
      selectedCharacter: character(),
    })

    expect(html).toContain(CARD_L.exportAction)
  })

  it('keeps import/browse actions regardless of the selected character', async () => {
    const html = await render(PlayerCharacterCardPanel, {
      selectedCharacter: character({ managed: true }),
    })

    expect(html).toContain(CARD_L.importAction)
    expect(html).toContain(CARD_L.browseAction)
  })
})

// ----------------------------------------------------------------------
// CharacterSettingsSection — SimpleVoicePicker (locked licensed voice)
// ----------------------------------------------------------------------

describe('CharacterSettingsSection managed gating', () => {
  const sectionProps = {
    characters: [],
    channelSetupPlatform: null,
    channelSetupSignal: 0,
  }

  it('hides the voice picker and shows the locked-voice notice for a managed character', async () => {
    const html = await render(CharacterSettingsSection, {
      character: character({ managed: true }),
      ...sectionProps,
    })

    expect(html).toContain(L.managed.voiceBadge)
    expect(html).toContain(L.managed.voiceNotice)
    // SimpleVoicePicker's own enable toggle / voice dropdown must be gone.
    expect(html).not.toContain(zhTW.simpleVoicePicker.enabledLabel)
  })

  it('keeps the voice picker for an ordinary character', async () => {
    const html = await render(CharacterSettingsSection, {
      character: character(),
      ...sectionProps,
    })

    expect(html).not.toContain(L.managed.voiceBadge)
    expect(html).toContain(zhTW.simpleVoicePicker.enabledLabel)
  })
})

// ----------------------------------------------------------------------
// admin/CharacterCardEditor — CharacterImagesPanel (locked official art)
// ----------------------------------------------------------------------
//
// Same `character.managed` flag, same gate shape, on the admin surface
// that shares CharacterEditPanel with the player settings tab (plan
// §3.2: "CharacterEditPanel 依 managed 只渲染可調區塊" — one component,
// both mount sites inherit the behaviour without a surface-specific branch).

describe('admin CharacterCardEditor managed gating', () => {
  it('hides CharacterImagesPanel and shows the locked-art notice for a managed character', async () => {
    const html = await render(CharacterCardEditor, {
      character: character({ managed: true }),
    })

    expect(html).toContain(L.managed.imagesBadge)
    expect(html).toContain(L.managed.imagesNotice)
    expect(html).not.toContain(zhTW.characterImagesPanel.actions.upload)
  })

  it('keeps CharacterImagesPanel for an ordinary character', async () => {
    const html = await render(CharacterCardEditor, {
      character: character(),
    })

    expect(html).not.toContain(L.managed.imagesBadge)
    expect(html).toContain(zhTW.characterImagesPanel.actions.upload)
  })

  it('also hides the persona section inside the shared CharacterEditPanel', async () => {
    // admin/CharacterCardEditor reuses CharacterEditPanel verbatim — this
    // is the same masking the CharacterEditPanel suite above pins,
    // re-asserted from the admin mount site so a future surface-specific
    // branch there cannot quietly reintroduce full-payload PATCH.
    const html = await render(CharacterCardEditor, {
      character: character({ managed: true }),
    })

    expect(html).toContain(L.managed.badge)
    expect(html).not.toContain(zhTW.characterCreate.fields.name.placeholder)
  })
})
