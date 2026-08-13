/**
 * EC2-C — three more bypass surfaces onto a managed (IP-partner)
 * character's protected fields, all discovered because they write
 * `Character` fields outside `CharacterService.update_character`'s own
 * form (`CharacterEditPanel`, gated by EC2-B) or write `image_urls`
 * through a second endpoint entirely:
 *
 * - `AlbumPanel`'s "promote to stage" button calls the album API
 *   directly, bypassing the PATCH allowlist that protects `image_urls`.
 * - `WorldAdminEditor` / `StoryPanel` PATCH `world_frame` /
 *   `world_topics` / `excluded_topics`, none of which are in
 *   `MANAGED_WRITABLE_UPDATE_FIELDS` — `world_frame` in particular
 *   always carries a real value (the control's default), so an
 *   ungated save would 400 on every attempt for a managed character.
 * - `StoryArcPanel`'s template-bar binds `arc_template_id`, also not
 *   in the allowlist.
 *
 * Same SSR-render pattern as `managedCharacterEdit.test.ts` (no DOM test
 * infra in this repo — see that file's header for the full rationale).
 */
import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import { messages as zhTW } from '@/i18n/locales/zh-TW'

vi.mock('@/composables/useAuth', () => ({
  useAuth: () => ({
    cloudMode: ref(false),
    portalUrl: ref<string | null>(null),
    currentUser: ref<Record<string, unknown> | null>(null),
  }),
  getStoredToken: () => null,
}))

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}))

vi.mock('@/utils/api/album', () => ({
  ALBUM_PAGE_SIZE: 40,
  listAlbum: vi.fn(async () => ({ items: [], total: 0, has_more: false, next_before: null })),
  transferStageToAlbum: vi.fn(),
  promoteAlbumToStage: vi.fn(),
  deleteAlbumItem: vi.fn(),
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
  updateCharacter: vi.fn(),
}))

const AlbumPanel = (await import('@/components/AlbumPanel.vue')).default
const WorldAdminEditor = (await import('@/components/admin/WorldAdminEditor.vue')).default
const StoryArcPanel = (await import('@/components/StoryArcPanel.vue')).default
const StoryPanel = (await import('@/components/StoryPanel.vue')).default

const L = zhTW.characterEdit.managed
const ARC_L = zhTW.story.arcPanel
const WORLD_L = zhTW.admin.worldEditor
// Admin surface (default `surface` prop) uses the root-level namespace —
// the `playerAuthoring.*` sibling is only wired for `surface="player"`.
const INTEREST_L = zhTW.interestSubscriptionPanel

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

// ----------------------------------------------------------------------
// AlbumPanel — "promote to stage" write path onto image_urls
// ----------------------------------------------------------------------

describe('AlbumPanel managed gating', () => {
  it('hides the promote button and shows the licensed-character notice for a managed character', async () => {
    const html = await render(AlbumPanel, {
      characterId: 'char-1',
      managed: true,
    })

    expect(html).toContain(L.albumBadge)
    expect(html).toContain(L.albumNotice)
    expect(html).not.toContain(zhTW.albumPanel.actions.promote)
  })

  it('keeps the promote button for an ordinary character', async () => {
    const html = await render(AlbumPanel, {
      characterId: 'char-1',
      managed: false,
    })

    expect(html).not.toContain(L.albumBadge)
  })

  it('treats an absent managed prop the same as false — self-host parity', async () => {
    const html = await render(AlbumPanel, {
      characterId: 'char-1',
    })

    expect(html).not.toContain(L.albumBadge)
  })
})

// ----------------------------------------------------------------------
// WorldAdminEditor — world_frame / world_topics / excluded_topics
// ----------------------------------------------------------------------

function worldCharacter(overrides: Record<string, unknown> = {}) {
  return {
    id: 'char-1',
    name: '美緒',
    world_frame: 'modern',
    world_awareness_enabled: true,
    subscribed_categories: ['news'],
    excluded_topics: [],
    world_topics: [],
    ...overrides,
  }
}

describe('WorldAdminEditor managed gating (admin surface)', () => {
  it('shows world_frame read-only and hides the select for a managed character', async () => {
    const html = await render(WorldAdminEditor, {
      character: worldCharacter({ managed: true }),
      patch: () => {},
    })

    expect(html).toContain(L.worldBadge)
    expect(html).toContain(L.worldFrameNotice)
    expect(html).not.toContain('<select')
  })

  it('hides world_topics / excluded_topics entirely for a managed character', async () => {
    const html = await render(WorldAdminEditor, {
      character: worldCharacter({ managed: true }),
      patch: () => {},
    })

    expect(html).toContain(L.worldTopicsNotice)
    expect(html).not.toContain(WORLD_L.eventPoolTitle)
    expect(html).not.toContain(INTEREST_L.excludedLegend)
  })

  it('keeps subscribed_categories editable for a managed character — the writable half', async () => {
    const html = await render(WorldAdminEditor, {
      character: worldCharacter({ managed: true }),
      patch: () => {},
    })

    expect(html).toContain(WORLD_L.subscriptionTitle.replace('{name}', '美緒'))
    expect(html).toContain(INTEREST_L.categoryLegend)
  })

  it('renders the full editor for an ordinary character — zero behaviour change', async () => {
    const html = await render(WorldAdminEditor, {
      character: worldCharacter(),
      patch: () => {},
    })

    expect(html).not.toContain(L.worldBadge)
    expect(html).toContain(WORLD_L.worldFrameTitle)
    expect(html).toContain(WORLD_L.eventPoolTitle)
    expect(html).toContain(INTEREST_L.excludedLegend)
    expect(html).toContain('<select')
  })
})

// ----------------------------------------------------------------------
// StoryArcPanel — arc_template_id binding control
// ----------------------------------------------------------------------

describe('StoryArcPanel managed gating', () => {
  it('hides the template choose/change button and shows the notice for a managed character', async () => {
    const html = await render(StoryArcPanel, {
      characterId: 'char-1',
      arcTemplateId: null,
      managed: true,
    })

    expect(html).toContain(L.arcBindingBadge)
    expect(html).toContain(L.arcBindingNotice)
    expect(html).not.toContain(ARC_L.template.choose)
    expect(html).not.toContain(ARC_L.template.changeOrClear)
  })

  it('still shows the read-only bound/unbound state for a managed character', async () => {
    const bound = await render(StoryArcPanel, {
      characterId: 'char-1',
      arcTemplateId: 'tpl-1',
      managed: true,
    })
    expect(bound).toContain('tpl-1')

    const unbound = await render(StoryArcPanel, {
      characterId: 'char-1',
      arcTemplateId: null,
      managed: true,
    })
    expect(unbound).toContain(ARC_L.template.unbound)
  })

  it('keeps the template button for an ordinary character', async () => {
    const html = await render(StoryArcPanel, {
      characterId: 'char-1',
      arcTemplateId: null,
      managed: false,
    })

    expect(html).not.toContain(L.arcBindingBadge)
    expect(html).toContain(ARC_L.template.choose)
  })
})

// ----------------------------------------------------------------------
// StoryPanel — its own world_frame select (PlayerSidebar's "life" tab)
// ----------------------------------------------------------------------

describe('StoryPanel managed gating', () => {
  it('shows world_frame read-only and forwards managed to StoryArcPanel', async () => {
    const html = await render(StoryPanel, {
      characterId: 'char-1',
      worldFrame: 'fantasy',
      arcTemplateId: null,
      managed: true,
    })

    expect(html).toContain(L.worldBadge)
    expect(html).toContain(L.worldFrameNotice)
    expect(html).toContain(L.arcBindingNotice)
    expect(html).not.toContain('<select')
  })

  it('keeps the editable world_frame select for an ordinary character', async () => {
    const html = await render(StoryPanel, {
      characterId: 'char-1',
      worldFrame: 'modern',
      arcTemplateId: null,
      managed: false,
    })

    expect(html).not.toContain(L.worldBadge)
    expect(html).toContain('<select')
  })
})
