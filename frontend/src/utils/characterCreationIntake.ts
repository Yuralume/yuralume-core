/**
 * Highest ``round_index`` the creation-intake API accepts (backend clamps to
 * the same ceiling). The modals bump the round every time an analysis does not
 * pass, so without this cap the value grows unbounded and the request starts
 * failing validation — which used to wipe the already-shown suggestions. Keep
 * this in sync with ``_MAX_INTAKE_ROUND`` in ``api/routes/characters.py``.
 */
export const MAX_CREATION_INTAKE_ROUND = 2

/** Bump the intake round without exceeding {@link MAX_CREATION_INTAKE_ROUND}. */
export function nextIntakeRound(current: number): number {
  return Math.min(current + 1, MAX_CREATION_INTAKE_ROUND)
}

export interface InitialRelationshipSuggestionState {
  relationship_label: string
  known_context: string
  living_arrangement: string
  user_address_name: string
  character_address_name: string
  tone_distance: string
  familiarity_boundary: string
  schedule_involvement_policy: string
  proactive_permission: boolean
  proactive_cadence_hint: string
  user_profile_notes: string
  profile_interests: string
  profile_routine: string
  profile_life_goals: string
}

export function emptyInitialRelationshipSuggestionState(): InitialRelationshipSuggestionState {
  return {
    relationship_label: '',
    known_context: '',
    living_arrangement: '',
    user_address_name: '',
    character_address_name: '',
    tone_distance: '',
    familiarity_boundary: '',
    schedule_involvement_policy: 'none',
    proactive_permission: false,
    proactive_cadence_hint: '',
    user_profile_notes: '',
    profile_interests: '',
    profile_routine: '',
    profile_life_goals: '',
  }
}

export function applyInitialRelationshipSuggestion(
  state: InitialRelationshipSuggestionState,
  field: string,
  suggestion: string,
  sentenceJoiner = '；',
): boolean {
  const value = suggestion.trim()
  if (!value) return false

  const normalizedField = normalizeIntakeField(field)
  if (BACKGROUND_FIELDS.has(normalizedField)) {
    state.known_context = appendSentence(state.known_context, value, sentenceJoiner)
    return true
  }
  if (RELATIONSHIP_LABEL_FIELDS.has(normalizedField)) {
    state.relationship_label = value
    return true
  }
  if (LIVING_ARRANGEMENT_FIELDS.has(normalizedField)) {
    state.living_arrangement = value
    return true
  }
  if (USER_ADDRESS_FIELDS.has(normalizedField)) {
    state.user_address_name = value
    return true
  }
  if (CHARACTER_ADDRESS_FIELDS.has(normalizedField)) {
    state.character_address_name = value
    return true
  }
  if (TONE_DISTANCE_FIELDS.has(normalizedField)) {
    state.tone_distance = value
    return true
  }
  if (BOUNDARY_FIELDS.has(normalizedField)) {
    state.familiarity_boundary = appendSentence(state.familiarity_boundary, value, sentenceJoiner)
    return true
  }
  if (PROACTIVE_CADENCE_FIELDS.has(normalizedField)) {
    state.proactive_cadence_hint = value
    state.proactive_permission = true
    return true
  }
  if (USER_PROFILE_NOTES_FIELDS.has(normalizedField)) {
    state.user_profile_notes = appendSentence(state.user_profile_notes, value, sentenceJoiner)
    return true
  }
  if (PROFILE_INTEREST_FIELDS.has(normalizedField)) {
    state.profile_interests = appendListItem(state.profile_interests, value)
    return true
  }
  if (PROFILE_ROUTINE_FIELDS.has(normalizedField)) {
    state.profile_routine = appendSentence(state.profile_routine, value, sentenceJoiner)
    return true
  }
  if (PROFILE_GOAL_FIELDS.has(normalizedField)) {
    state.profile_life_goals = appendListItem(state.profile_life_goals, value)
    return true
  }

  state.user_profile_notes = appendSentence(state.user_profile_notes, value, sentenceJoiner)
  return true
}

export function shouldBlockCreateForIntake(
  warnings: Array<{ blocking?: boolean }>,
  options: { stale: boolean },
): boolean {
  return !options.stale && warnings.some(warning => warning.blocking === true)
}

export function normalizeIntakeField(field: string): string {
  return field.trim().toLowerCase().replace(/[-.\s]+/g, '_')
}

export function canonicalInitialRelationshipField(field: string): string {
  const normalizedField = normalizeIntakeField(field)
  if (BACKGROUND_FIELDS.has(normalizedField)) return 'known_context'
  if (RELATIONSHIP_LABEL_FIELDS.has(normalizedField)) return 'relationship_label'
  if (LIVING_ARRANGEMENT_FIELDS.has(normalizedField)) return 'living_arrangement'
  if (USER_ADDRESS_FIELDS.has(normalizedField)) return 'user_address_name'
  if (CHARACTER_ADDRESS_FIELDS.has(normalizedField)) return 'character_address_name'
  if (TONE_DISTANCE_FIELDS.has(normalizedField)) return 'tone_distance'
  if (BOUNDARY_FIELDS.has(normalizedField)) return 'familiarity_boundary'
  if (PROACTIVE_CADENCE_FIELDS.has(normalizedField)) return 'proactive_cadence_hint'
  if (USER_PROFILE_NOTES_FIELDS.has(normalizedField)) return 'user_profile_notes'
  if (PROFILE_INTEREST_FIELDS.has(normalizedField)) return 'profile_interests'
  if (PROFILE_ROUTINE_FIELDS.has(normalizedField)) return 'profile_routine'
  if (PROFILE_GOAL_FIELDS.has(normalizedField)) return 'profile_life_goals'
  return normalizedField
}

function appendSentence(current: string, value: string, joiner = '；'): string {
  const existing = current.trim()
  if (!existing) return value
  const existingItems = existing
    .split(/[；;]/)
    .map(item => item.trim())
  if (existing === value || existingItems.includes(value)) {
    return existing
  }
  return `${existing}${joiner}${value}`
}

function appendListItem(current: string, value: string): string {
  const existingItems = current
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
  if (existingItems.includes(value)) {
    return existingItems.join(', ')
  }
  return [...existingItems, value].join(', ')
}

const BACKGROUND_FIELDS = new Set([
  'known_context',
  'context',
  'background',
  'relationship_context',
  'relationship_background',
  'shared_context',
])

const RELATIONSHIP_LABEL_FIELDS = new Set([
  'relationship',
  'relationship_label',
  'starting_relationship',
])

const LIVING_ARRANGEMENT_FIELDS = new Set([
  'living_arrangement',
  'living',
  'living_context',
  'home_context',
  'housing',
  'residence',
  'cohabitation',
])

const USER_ADDRESS_FIELDS = new Set([
  'user_address_name',
  'user_address',
  'operator_address_name',
  'nickname',
])

const CHARACTER_ADDRESS_FIELDS = new Set([
  'character_address_name',
  'character_address',
])

const TONE_DISTANCE_FIELDS = new Set([
  'tone_distance',
  'distance',
  'tone',
])

const BOUNDARY_FIELDS = new Set([
  'boundary',
  'boundaries',
  'familiarity_boundary',
  'relationship_boundary',
])

const PROACTIVE_CADENCE_FIELDS = new Set([
  'proactive_cadence_hint',
  'proactive_cadence',
  'proactive_permission',
  'proactive',
])

const USER_PROFILE_NOTES_FIELDS = new Set([
  'user_profile_notes',
  'profile_notes',
  'safe_user_profile',
  'user_profile',
])

const PROFILE_INTEREST_FIELDS = new Set([
  'profile_interests',
  'interests',
  'safe_user_profile_interests',
])

const PROFILE_ROUTINE_FIELDS = new Set([
  'profile_routine',
  'routine',
  'safe_user_profile_routine',
])

const PROFILE_GOAL_FIELDS = new Set([
  'profile_life_goals',
  'life_goals',
  'goals',
  'safe_user_profile_life_goals',
])
