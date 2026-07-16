import { describe, expect, it } from 'vitest'

import {
  WIZARD_DRAFT_STORAGE_KEY,
  clearWizardDraft,
  draftHasWizardContent,
  loadWizardDraft,
  saveWizardDraft,
} from '@/utils/arcWizardDraft'
import type { TemplateDraftPayload } from '@/types/arcTemplateIntake'

function fakeStorage() {
  const map = new Map<string, string>()
  return {
    map,
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => {
      map.set(k, v)
    },
    removeItem: (k: string) => {
      map.delete(k)
    },
  }
}

function draft(overrides: Partial<TemplateDraftPayload> = {}): TemplateDraftPayload {
  return {
    id: '',
    title: '',
    premise: '',
    theme: 'custom',
    tone: 'daily',
    duration_days: 14,
    world_frames: [],
    required_traits: [],
    applicability_scope: 'generic',
    target_character_ids: [],
    beats: [],
    ...overrides,
  }
}

describe('arcWizardDraft', () => {
  it('treats a blank draft as no content and a filled one as content', () => {
    expect(draftHasWizardContent(draft(), '')).toBe(false)
    expect(draftHasWizardContent(draft(), '   ')).toBe(false)
    expect(draftHasWizardContent(draft({ title: 'x' }), '')).toBe(true)
    expect(draftHasWizardContent(draft({ premise: 'y' }), '')).toBe(true)
    expect(draftHasWizardContent(draft(), 'a pitch')).toBe(true)
    expect(
      draftHasWizardContent(
        draft({ beats: [{ sequence: 0, day_offset: 0 } as never] }),
        '',
      ),
    ).toBe(true)
  })

  it('autosaves a draft with content and round-trips it back', () => {
    const storage = fakeStorage()
    const state = { draft: draft({ title: 'My Arc' }), pitch: 'a week at the cafe', step: 5 }

    saveWizardDraft(storage, state)
    expect(storage.map.has(WIZARD_DRAFT_STORAGE_KEY)).toBe(true)

    const loaded = loadWizardDraft(storage)
    expect(loaded).not.toBeNull()
    expect(loaded?.draft.title).toBe('My Arc')
    expect(loaded?.pitch).toBe('a week at the cafe')
    expect(loaded?.step).toBe(5)
  })

  it('clears the slot instead of storing a blank draft', () => {
    const storage = fakeStorage()
    storage.setItem(WIZARD_DRAFT_STORAGE_KEY, 'stale')

    saveWizardDraft(storage, { draft: draft(), pitch: '', step: 1 })

    expect(storage.map.has(WIZARD_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('returns null for absent, blank, or corrupt stored drafts', () => {
    const storage = fakeStorage()
    expect(loadWizardDraft(storage)).toBeNull()

    storage.setItem(WIZARD_DRAFT_STORAGE_KEY, JSON.stringify({ draft: draft(), pitch: '', step: 1 }))
    expect(loadWizardDraft(storage)).toBeNull() // blank → not worth restoring

    storage.setItem(WIZARD_DRAFT_STORAGE_KEY, '{ not json')
    expect(loadWizardDraft(storage)).toBeNull()
  })

  it('clears an autosaved draft on demand', () => {
    const storage = fakeStorage()
    saveWizardDraft(storage, { draft: draft({ title: 'x' }), pitch: '', step: 2 })
    expect(storage.map.has(WIZARD_DRAFT_STORAGE_KEY)).toBe(true)

    clearWizardDraft(storage)
    expect(storage.map.has(WIZARD_DRAFT_STORAGE_KEY)).toBe(false)
  })

  it('is a safe no-op when storage is unavailable', () => {
    expect(loadWizardDraft(null)).toBeNull()
    expect(() => saveWizardDraft(null, { draft: draft({ title: 'x' }), pitch: '', step: 1 })).not.toThrow()
    expect(() => clearWizardDraft(null)).not.toThrow()
  })
})
