import { describe, expect, it } from 'vitest'

import {
  WIZARD_DRAFT_STORAGE_KEY,
  applyBeatSummaryResult,
  clearWizardDraft,
  draftHasWizardContent,
  loadWizardDraft,
  saveWizardDraft,
} from '@/utils/arcWizardDraft'
import type {
  BeatDraftPayload,
  GenerateSummaryResponse,
  TemplateDraftPayload,
} from '@/types/arcTemplateIntake'
import { blankBeatDraft } from '@/types/arcTemplateIntake'

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

// ---------- applyBeatSummaryResult (Codex review fix, M1) -----------
//
// "regenerate summary" used to overwrite operator_position/operator_note
// unconditionally, so an LLM crash, an off-vocabulary hallucination, or a
// model simply judging differently on rerun could silently wipe a
// manually-picked `central` back to unjudged. The merge is gated per field,
// independently, so a manually-typed note survives even if the position is
// still unjudged (and vice versa).

function summaryResult(
  overrides: Partial<GenerateSummaryResponse> = {},
): GenerateSummaryResponse {
  return {
    summary: '模型生成的新摘要文字',
    operator_position: null,
    operator_note: null,
    ...overrides,
  }
}

describe('applyBeatSummaryResult', () => {
  it('always overwrites the summary text', () => {
    const beat: BeatDraftPayload = blankBeatDraft(0, 0)
    beat.summary = '舊摘要'
    applyBeatSummaryResult(beat, summaryResult({ summary: '新摘要' }))
    expect(beat.summary).toBe('新摘要')
  })

  it('applies the proposal when the beat is still unjudged', () => {
    const beat: BeatDraftPayload = blankBeatDraft(0, 0)
    applyBeatSummaryResult(
      beat,
      summaryResult({ operator_position: 'present', operator_note: '你在旁邊看著' }),
    )
    expect(beat.operator_position).toBe('present')
    expect(beat.operator_note).toBe('你在旁邊看著')
  })

  it('never reopens a position the operator already decided', () => {
    const beat: BeatDraftPayload = blankBeatDraft(0, 0)
    beat.operator_position = 'central'
    beat.operator_note = '她要向你坦白'
    applyBeatSummaryResult(
      beat,
      summaryResult({ operator_position: 'absent', operator_note: '他完全不在場' }),
    )
    expect(beat.operator_position).toBe('central')
    expect(beat.operator_note).toBe('她要向你坦白')
  })

  it('keeps a manually-typed note even while the position is still unjudged', () => {
    const beat: BeatDraftPayload = blankBeatDraft(0, 0)
    beat.operator_note = '操作者手寫的 note，不該被蓋掉'
    applyBeatSummaryResult(
      beat,
      summaryResult({ operator_position: 'present', operator_note: '模型自己想的 note' }),
    )
    expect(beat.operator_position).toBe('present')
    expect(beat.operator_note).toBe('操作者手寫的 note，不該被蓋掉')
  })

  it('clears back to unjudged only through an explicit null proposal, not silently', () => {
    // Sanity: a still-unjudged beat with the model also proposing
    // nothing (e.g. a crash fallback) stays unjudged rather than
    // fabricating a position — this is the pre-existing degrade path,
    // just confirming the merge doesn't disturb it.
    const beat: BeatDraftPayload = blankBeatDraft(0, 0)
    applyBeatSummaryResult(beat, summaryResult())
    expect(beat.operator_position).toBeNull()
    expect(beat.operator_note).toBeNull()
  })
})
