import axios from 'axios'

export interface PersonaEvidence {
  turn_id: string
  conversation_id: string
  quote: string
  extracted_at: string
}

export interface PersonaField {
  field_id: string | null
  layer: number
  field_key: string
  value: string
  confidence: number
  source: string
  update_count: number
  last_updated: string
  evidence: PersonaEvidence[]
}

export interface PersonaCandidate {
  candidate_id: string | null
  layer: number
  field_key: string
  proposed_value: string
  raw_extractor_confidence: number
  state: string
  source: string
  explicit: boolean
  extracted_at: string
  evidence: PersonaEvidence
}

export interface PersonaInteractionStrength {
  first_message_at: string | null
  total_user_messages: number
  days_since_first_contact: number
  messages_last_7_days: number
  messages_last_30_days: number
  longest_session_minutes: number
  shared_arc_realized_count: number
  shared_drama_count: number
  familiarity_band: 'stranger' | 'acquaintance' | 'familiar' | 'close'
  computed_at: string
}

export interface PersonaInitialRelationship {
  relationship_label: string
  summary_lines: string[]
}

export interface PersonaSnapshot {
  character_id: string
  operator_id: string
  layer1_identity: PersonaField[]
  layer2_life: PersonaField[]
  layer3_emotional: PersonaField[]
  layer5_trust: PersonaField[]
  interaction_strength: PersonaInteractionStrength | null
  initial_relationship: PersonaInitialRelationship | null
  pending_candidates: PersonaCandidate[]
  prompt_preview_lines: string[]
}

export interface PersonaDreamTickResult {
  applied: boolean
  promotions: number
  merges: number
  supersedes: number
  rejections: number
  decays: number
  inferences: number
}

export interface PersonaProjectionFact {
  field_id: string
  /**
   * Stable enum key (e.g. `name` / `interests`) the UI translates via
   * its trilingual bundle (plan D6). `label` stays as the zh-TW default
   * for older backends that don't send `field_key`.
   */
  field_key?: string
  label: string
  value: string
}

export interface PersonaProjection {
  character_id: string
  narrative: string
  facts: PersonaProjectionFact[]
  empty: boolean
}

const BASE = '/api/v1'

export async function getOperatorPersona(
  characterId: string,
): Promise<PersonaSnapshot> {
  const { data } = await axios.get<PersonaSnapshot>(
    `${BASE}/operator/persona`,
    { params: { character_id: characterId } },
  )
  return data
}

export async function getOperatorPersonaProjection(
  characterId: string,
): Promise<PersonaProjection> {
  const { data } = await axios.get<PersonaProjection>(
    `${BASE}/operator/persona/projection`,
    { params: { character_id: characterId } },
  )
  return data
}

export async function rejectPersonaCandidate(candidateId: string): Promise<void> {
  await axios.post(
    `${BASE}/operator/persona/candidates/${encodeURIComponent(candidateId)}/reject`,
  )
}

export async function transitionPersonaFieldState(
  fieldId: string,
  state: 'stale' | 'superseded' | 'rejected',
): Promise<void> {
  await axios.post(
    `${BASE}/operator/persona/fields/${encodeURIComponent(fieldId)}/state`,
    { state },
  )
}

/** Player correction of a learned identity field (name / nickname). The
 * backend supersedes any learned value and writes the explicit one. */
export async function setPersonaField(payload: {
  character_id: string
  field_key: 'name' | 'nickname'
  value: string
}): Promise<PersonaField> {
  const { data } = await axios.put<PersonaField>(
    `${BASE}/operator/persona/fields`,
    payload,
  )
  return data
}

export async function triggerPersonaDreamTick(
  characterId: string,
): Promise<PersonaDreamTickResult> {
  const { data } = await axios.post<PersonaDreamTickResult>(
    `${BASE}/admin/operator/persona/dream-tick`,
    null,
    { params: { character_id: characterId } },
  )
  return data
}
