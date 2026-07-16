import { describe, expect, it } from 'vitest'
import {
  applyInitialRelationshipSuggestion,
  canonicalInitialRelationshipField,
  emptyInitialRelationshipSuggestionState,
  MAX_CREATION_INTAKE_ROUND,
  nextIntakeRound,
  shouldBlockCreateForIntake,
} from '@/utils/characterCreationIntake'

describe('applyInitialRelationshipSuggestion', () => {
  it('appends background suggestions instead of replacing earlier answers', () => {
    const state = emptyInitialRelationshipSuggestionState()

    applyInitialRelationshipSuggestion(state, 'known_context', '第一次見面，還沒有共同背景')
    applyInitialRelationshipSuggestion(state, 'known_context', '已經認識，但不要補共同回憶')

    expect(state.known_context).toBe('第一次見面，還沒有共同背景；已經認識，但不要補共同回憶')
  })

  it('routes suggestions to their own relationship fields', () => {
    const state = emptyInitialRelationshipSuggestionState()

    applyInitialRelationshipSuggestion(state, 'proactive_cadence_hint', '一天最多一次')
    applyInitialRelationshipSuggestion(state, 'familiarity_boundary', '只準備話題，不安排見面')
    applyInitialRelationshipSuggestion(state, 'living_arrangement', '住在一起')
    applyInitialRelationshipSuggestion(state, 'profile_interests', '音樂')
    applyInitialRelationshipSuggestion(state, 'profile_interests', '咖啡')

    expect(state.proactive_permission).toBe(true)
    expect(state.proactive_cadence_hint).toBe('一天最多一次')
    expect(state.familiarity_boundary).toBe('只準備話題，不安排見面')
    expect(state.living_arrangement).toBe('住在一起')
    expect(state.profile_interests).toBe('音樂, 咖啡')
    expect(state.known_context).toBe('')
  })

  it('keeps unknown suggestions as additional notes without overwriting background', () => {
    const state = emptyInitialRelationshipSuggestionState()
    state.known_context = '你們在同一間咖啡店認識'

    applyInitialRelationshipSuggestion(state, 'custom_expectation', '不要一開始就裝熟')
    applyInitialRelationshipSuggestion(state, 'custom_expectation', '先保持禮貌')

    expect(state.known_context).toBe('你們在同一間咖啡店認識')
    expect(state.user_profile_notes).toBe('不要一開始就裝熟；先保持禮貌')
  })

  it('uses the caller-supplied joiner instead of a hardcoded fullwidth semicolon', () => {
    const state = emptyInitialRelationshipSuggestionState()

    applyInitialRelationshipSuggestion(state, 'known_context', 'First time meeting, no shared history', '; ')
    applyInitialRelationshipSuggestion(state, 'known_context', 'Already acquainted, skip shared memories', '; ')

    expect(state.known_context).toBe('First time meeting, no shared history; Already acquainted, skip shared memories')
  })

  it('dedupes against existing entries regardless of which joiner was used to write them', () => {
    const state = emptyInitialRelationshipSuggestionState()

    applyInitialRelationshipSuggestion(state, 'known_context', 'sentence one', '; ')
    applyInitialRelationshipSuggestion(state, 'known_context', 'sentence one', '; ')

    expect(state.known_context).toBe('sentence one')
  })
})

describe('canonicalInitialRelationshipField', () => {
  it('maps LLM field aliases to visible relationship inputs', () => {
    expect(canonicalInitialRelationshipField('boundary')).toBe('familiarity_boundary')
    expect(canonicalInitialRelationshipField('relationship')).toBe('relationship_label')
    expect(canonicalInitialRelationshipField('cohabitation')).toBe('living_arrangement')
    expect(canonicalInitialRelationshipField('interests')).toBe('profile_interests')
    expect(canonicalInitialRelationshipField('proactive_permission')).toBe('proactive_cadence_hint')
  })
})

describe('nextIntakeRound', () => {
  it('bumps the round but never past the backend ceiling', () => {
    // Regression: the modals call this on every failed analysis. Without the
    // clamp the value grew unbounded, the request eventually 422'd, and the
    // catch block wiped the already-shown suggestion chips.
    expect(nextIntakeRound(0)).toBe(1)
    expect(nextIntakeRound(1)).toBe(2)
    expect(nextIntakeRound(2)).toBe(MAX_CREATION_INTAKE_ROUND)
    expect(nextIntakeRound(2)).toBe(2)
    expect(nextIntakeRound(9)).toBe(2)
  })
})

describe('shouldBlockCreateForIntake', () => {
  it('does not block creation for ordinary follow-up questions', () => {
    expect(shouldBlockCreateForIntake([], { stale: false })).toBe(false)
  })

  it('blocks only current blocking warnings', () => {
    expect(shouldBlockCreateForIntake([{ blocking: true }], { stale: false })).toBe(true)
    expect(shouldBlockCreateForIntake([{ blocking: true }], { stale: true })).toBe(false)
    expect(shouldBlockCreateForIntake([{ blocking: false }], { stale: false })).toBe(false)
  })
})
