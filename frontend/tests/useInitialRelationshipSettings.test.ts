/**
 * 設定頁「關係與相處設定」編輯器 (IR2).
 *
 * 這個 repo 沒有 jsdom / @vue/test-utils（見 playerPersonaNoteSurface.test.ts
 * 檔頭），互動元件走「composable 邏輯直接測、渲染面用來源掃描釘住」的兩層：
 *   1. `useInitialRelationshipSettings` — 預填、tri-state 存檔差集、存檔後
 *      不會把同一批值誤判成又改過一次。axios 直接 mock，不經真正網路。
 *   2. `InitialRelationshipSettingsEditor.vue` / `CharacterSettingsSection.vue`
 *      的來源掃描 — 勾選展開頻率欄、四選一下拉、稱呼欄位併入同一份表單、
 *      未改欄位不送出等在 composable 測不到的模板細節。
 */

import { readFileSync } from 'node:fs'

import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'

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
const { useInitialRelationshipSettings } = await import(
  '@/composables/useInitialRelationshipSettings'
)

beforeEach(() => {
  vi.clearAllMocks()
})

function seedFixture(overrides: Record<string, unknown> = {}) {
  return {
    character_id: 'c1',
    operator_id: 'op1',
    has_seed: true,
    relationship_label: '朋友',
    known_context: '在同一間咖啡店認識',
    living_arrangement: '',
    user_address_name: '阿丹',
    character_address_name: '澪',
    tone_distance: '',
    familiarity_boundary: '',
    schedule_involvement_policy: 'invite_required',
    proactive_permission: true,
    proactive_cadence_hint: '一週一兩次',
    user_profile_notes: '',
    confirmed_by_user: true,
    ...overrides,
  }
}

// ----------------------------------------------------------------------
// useInitialRelationshipSettings — load, dirty-tracking, tri-state save
// ----------------------------------------------------------------------

describe('useInitialRelationshipSettings', () => {
  it('pre-fills the form from the GET response', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: seedFixture() })
    const settings = useInitialRelationshipSettings()

    await settings.load('c1')

    expect(settings.loaded.value).toBe(true)
    expect(settings.loading.value).toBe(false)
    expect(settings.form.value).toMatchObject({
      relationship_label: '朋友',
      known_context: '在同一間咖啡店認識',
      user_address_name: '阿丹',
      character_address_name: '澪',
      schedule_involvement_policy: 'invite_required',
      proactive_permission: true,
      proactive_cadence_hint: '一週一兩次',
    })
    expect(settings.hasChanges.value).toBe(false)
  })

  it('pre-fills a blank form for a pair with no seed yet, instead of erroring', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: seedFixture({
        has_seed: false,
        relationship_label: '',
        known_context: '',
        user_address_name: '',
        character_address_name: '',
        schedule_involvement_policy: 'none',
        proactive_permission: false,
        proactive_cadence_hint: '',
      }),
    })
    const settings = useInitialRelationshipSettings()

    await settings.load('c1')

    expect(settings.loaded.value).toBe(true)
    expect(settings.form.value.relationship_label).toBe('')
    expect(settings.hasChanges.value).toBe(false)
  })

  it('fails soft on a read error: "loaded" stays false instead of throwing out of the watcher', async () => {
    // The component calls `load` as `void load(characterId)` from an
    // immediate watcher and never awaits it — a rejection here would be an
    // unhandled promise rejection, not a caught error.
    mockedAxios.get.mockRejectedValueOnce(new Error('offline'))
    const settings = useInitialRelationshipSettings()

    await expect(settings.load('c1')).resolves.toBeUndefined()
    expect(settings.loaded.value).toBe(false)
    expect(settings.loading.value).toBe(false)
  })

  it('flags hasChanges as soon as a field diverges from the loaded value', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: seedFixture() })
    const settings = useInitialRelationshipSettings()
    await settings.load('c1')

    settings.form.value.tone_distance = '更親近一點'

    expect(settings.hasChanges.value).toBe(true)
  })

  it('does not call the API and reports "nothing changed" when the form is untouched', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: seedFixture() })
    const settings = useInitialRelationshipSettings()
    await settings.load('c1')

    const result = await settings.save()

    expect(result).toEqual({ changed: false })
    expect(mockedAxios.patch).not.toHaveBeenCalled()
  })

  it('PATCHes only the field that changed, never the untouched ones as empty strings', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: seedFixture() })
    const settings = useInitialRelationshipSettings()
    await settings.load('c1')
    settings.form.value.tone_distance = '更親近一點'
    mockedAxios.patch.mockResolvedValueOnce({
      data: seedFixture({ tone_distance: '更親近一點' }),
    })

    const result = await settings.save()

    expect(result).toEqual({ changed: true })
    expect(mockedAxios.patch).toHaveBeenCalledWith(
      '/api/v1/characters/c1/initial-relationship',
      { tone_distance: '更親近一點' },
    )
  })

  it('re-baselines against the saved values so a second no-op save sends nothing', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: seedFixture() })
    const settings = useInitialRelationshipSettings()
    await settings.load('c1')
    settings.form.value.tone_distance = '更親近一點'
    mockedAxios.patch.mockResolvedValueOnce({
      data: seedFixture({ tone_distance: '更親近一點' }),
    })
    await settings.save()

    expect(settings.hasChanges.value).toBe(false)
    const second = await settings.save()

    expect(second).toEqual({ changed: false })
    expect(mockedAxios.patch).toHaveBeenCalledTimes(1)
  })

  it('sends a clear as an explicit empty string, not as an omission', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: seedFixture() })
    const settings = useInitialRelationshipSettings()
    await settings.load('c1')
    settings.form.value.known_context = ''
    mockedAxios.patch.mockResolvedValueOnce({
      data: seedFixture({ known_context: '' }),
    })

    await settings.save()

    expect(mockedAxios.patch).toHaveBeenCalledWith(
      '/api/v1/characters/c1/initial-relationship',
      { known_context: '' },
    )
  })

  it('does nothing when asked to save before any character has been loaded', async () => {
    const settings = useInitialRelationshipSettings()

    const result = await settings.save()

    expect(result).toEqual({ changed: false })
    expect(mockedAxios.patch).not.toHaveBeenCalled()
  })
})

// ----------------------------------------------------------------------
// Wiring — template details a composable-only test cannot see, pinned on
// the source the same way playerPersonaNoteSurface.test.ts pins ChatPanel.
// ----------------------------------------------------------------------

function sourceOf(relative: string): string {
  return readFileSync(new URL(`../src/${relative}`, import.meta.url), 'utf8')
}

describe('InitialRelationshipSettingsEditor wiring', () => {
  const src = sourceOf('components/InitialRelationshipSettingsEditor.vue')

  it('only reveals the cadence hint once proactive permission is checked', () => {
    expect(src).toContain('v-if="form.proactive_permission"')
    expect(src).toContain('v-model="form.proactive_permission"')
    expect(src).toContain('type="checkbox"')
  })

  it('offers the same four schedule-involvement choices as character creation', () => {
    expect(src).toContain('characterCreate.initialRelationship.scheduleOptions.none')
    expect(src).toContain('characterCreate.initialRelationship.scheduleOptions.mentionOnly')
    expect(src).toContain('characterCreate.initialRelationship.scheduleOptions.inviteRequired')
    expect(src).toContain('characterCreate.initialRelationship.scheduleOptions.sharedAllowed')
  })

  it('folds the two address-name fields into this same form', () => {
    expect(src).toContain('form.user_address_name')
    expect(src).toContain('form.character_address_name')
    expect(src).toContain("t('playerSidebar.relationshipSeed.namesHint')")
    expect(src).toContain("t('playerSidebar.relationshipSeed.namesReconcileHint'")
  })

  it('disables the save button while there is nothing to save', () => {
    expect(src).toContain(':disabled="!hasChanges"')
  })

  it('does not roll its own field styling, using the shared UI primitives instead', () => {
    expect(src).not.toContain('class="field-input"')
    expect(src).not.toContain('class="field-textarea"')
    expect(src).not.toContain('class="field-select"')
  })

  it('confirms a successful save without claiming one happened on a no-op', () => {
    expect(src).toContain('result.changed')
    expect(src).toContain("t('playerSidebar.relationshipSeed.saved')")
  })

  it('does not report a failed read as an empty seed', () => {
    expect(src).toContain("t('playerSidebar.relationshipSeed.loadFailed')")
    expect(src).toContain('!loading.value && !loaded.value')
  })
})

describe('CharacterSettingsSection wiring', () => {
  const src = sourceOf('components/CharacterSettingsSection.vue')

  it('mounts the merged seed editor instead of the retired names-only editor', () => {
    expect(src).toContain('<InitialRelationshipSettingsEditor')
    expect(src).not.toContain('RelationshipNamesEditor')
    expect(src).toContain("t('playerSidebar.relationshipSeed.title')")
  })

  it('remounts the editor when the selected character changes', () => {
    expect(src).toContain(':key="`${character.id}:rel-seed`"')
  })
})
