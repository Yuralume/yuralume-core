import axios from 'axios'

const BASE = '/api/v1/admin/observability'
const USAGE_BASE = '/api/v1/admin/usage'

export type OperatorFeedbackKind = 'out_of_character' | 'felt_human'

export interface OperatorFeedback {
  kind?: OperatorFeedbackKind | string
  note?: string
  tags?: string[]
  source?: string
  updated_at?: string
}

export interface TurnRecordSummary {
  id: string
  character_id: string
  conversation_id: string | null
  kind: string
  model_id: string
  prompt_pack_hash: string
  latency_ms: number | null
  prompt_tokens: number | null
  completion_tokens: number | null
  error: string | null
  operator_feedback: OperatorFeedback
  response_excerpt: string
  created_at: string
}

export interface TurnRecordDetail {
  id: string
  character_id: string
  conversation_id: string | null
  kind: string
  model_id: string
  prompt_pack_hash: string
  prompt_assembled: string
  response_text: string
  response_json: Record<string, unknown> | null
  latency_ms: number | null
  prompt_tokens: number | null
  completion_tokens: number | null
  error: string | null
  post_turn_refs: Record<string, unknown>
  operator_feedback: OperatorFeedback
  created_at: string
}

export interface LatencyBucket {
  lower_ms: number
  upper_ms: number | null
  count: number
}

export interface EmotionEventRow {
  id: string
  character_id: string
  operator_id: string
  cause_ref_kind: string
  cause_ref_id: string | null
  valence: number
  arousal: number
  intensity: number
  affection_delta: number
  fatigue_delta: number
  trust_delta: number
  energy_delta: number
  emotion_label: string
  evidence_quote: string
  decay_half_life_minutes: number
  created_at: string
}

export interface ProactiveFunnel {
  sent: number
  decider_skipped: number
  intention_skipped: number
  gate_blocked: number
  errored: number
  disabled: number
  no_binding: number
  total: number
}

export type UsageCapability = '' | 'llm' | 'image' | 'video' | 'tts'

export interface UsageQueryParams {
  from?: string | null
  to?: string | null
  capability?: UsageCapability | string | null
  characterId?: string | null
  limit?: number
}

export interface UsageSummary {
  request_count: number
  succeeded_count: number
  failed_count: number
  cached_count: number
  estimated_usage_count: number
  estimated_cost_count: number
  total_input_quantity: number
  total_output_quantity: number
  total_billable_quantity: number
  cost_currency: string
  total_cost_amount: string
}

export interface UsageFeatureBucket {
  feature_key: string
  capability: string
  request_count: number
  total_input_quantity: number
  total_output_quantity: number
  total_billable_quantity: number
  total_cost_amount: string
}

export interface UsageCharacterFeatureBucket {
  character_id: string | null
  feature_key: string
  capability: string
  request_count: number
  total_input_quantity: number
  total_output_quantity: number
  total_billable_quantity: number
  total_cost_amount: string
}

export interface UsageModelBucket {
  provider_id: string
  model_id: string
  capability: string
  request_count: number
  total_input_quantity: number
  total_output_quantity: number
  total_billable_quantity: number
  total_cost_amount: string
}

export interface UsageCharacterBucket {
  character_id: string | null
  request_count: number
  total_input_quantity: number
  total_output_quantity: number
  total_billable_quantity: number
  total_cost_amount: string
  /** Distinct UTC calendar dates the character produced any event on —
   * the denominator for the cost-modeling background-noise statistic. */
  active_days: number
}

export interface UsageEventRow {
  id: string
  request_id: string
  upstream_request_id: string
  turn_record_id: string | null
  conversation_id: string | null
  character_id: string | null
  operator_id: string
  capability: string
  feature_key: string
  source_surface: string
  routing_mode: string
  provider_id: string
  model_id: string
  profile_id: string
  voice_id: string
  usage_unit: string
  input_quantity: number
  output_quantity: number
  total_quantity: number
  billable_quantity: number
  cached: boolean
  usage_is_estimated: boolean
  cost_currency: string
  cost_amount: string
  cost_is_estimated: boolean
  pricing_source: string
  pricing_version: string
  latency_ms: number | null
  status: string
  error_code: string | null
  error_message: string | null
  artifact_count: number
  output_bytes: number | null
  duration_seconds: string | null
  created_at: string
  completed_at: string | null
}

export interface PersonaCuriosityAttempt {
  id: string
  character_id: string
  operator_id: string
  conversation_id: string | null
  surface: string
  target_layer: number
  target_topic: string
  question_intent: string
  status: string
  created_at: string
  cooldown_until: string | null
  response_turn_id: string | null
  metadata: Record<string, unknown>
}

export interface PersonaCuriosityMetrics {
  window_hours: number
  plan_count: number
  ask_plan_count: number
  no_ask_plan_count: number
  asked_count: number
  answered_count: number
  deflected_count: number
  ignored_count: number
  answered_ratio: number
  deflected_ratio: number
  ignored_ratio: number
  persona_candidate_facts_after_curiosity: number
  repeated_question_guard_incidents: number
}

export interface PersonaCuriosityFlags {
  enabled: boolean
  proactive_enabled: boolean
  env_names: Record<string, string>
}

export interface ListTurnsParams {
  characterId?: string | null
  kind?: string | null
  feedbackKind?: OperatorFeedbackKind | string | null
  sinceIso?: string | null
  limit?: number
}

export async function listTurns(
  params: ListTurnsParams = {},
): Promise<TurnRecordSummary[]> {
  const { data } = await axios.get<TurnRecordSummary[]>(`${BASE}/turns`, {
    params: {
      character_id: params.characterId ?? undefined,
      kind: params.kind ?? undefined,
      feedback_kind: params.feedbackKind ?? undefined,
      since: params.sinceIso ?? undefined,
      limit: params.limit ?? 50,
    },
  })
  return data
}

export async function getTurn(turnId: string): Promise<TurnRecordDetail> {
  const { data } = await axios.get<TurnRecordDetail>(`${BASE}/turns/${turnId}`)
  return data
}

export async function updateTurnOperatorFeedback(
  turnId: string,
  payload: {
    kind: OperatorFeedbackKind
    note?: string
    tags?: string[]
  },
): Promise<TurnRecordDetail> {
  const { data } = await axios.put<TurnRecordDetail>(
    `${BASE}/turns/${turnId}/operator-feedback`,
    payload,
  )
  return data
}

export async function latencyHistogram(
  params: { characterId?: string | null; kind?: string | null } = {},
): Promise<LatencyBucket[]> {
  const { data } = await axios.get<LatencyBucket[]>(
    `${BASE}/turns/latency-histogram`,
    {
      params: {
        character_id: params.characterId ?? undefined,
        kind: params.kind ?? undefined,
      },
    },
  )
  return data
}

export async function proactiveFunnel(
  params: { characterId?: string | null; sinceHours?: number } = {},
): Promise<ProactiveFunnel> {
  const { data } = await axios.get<ProactiveFunnel>(`${BASE}/proactive/funnel`, {
    params: {
      character_id: params.characterId ?? undefined,
      since_hours: params.sinceHours ?? 24,
    },
  })
  return data
}

function usageParams(params: UsageQueryParams = {}) {
  return {
    from: params.from ?? undefined,
    to: params.to ?? undefined,
    capability: params.capability || undefined,
    character_id: params.characterId ?? undefined,
    limit: params.limit ?? undefined,
  }
}

export async function usageSummary(
  params: UsageQueryParams = {},
): Promise<UsageSummary> {
  const { data } = await axios.get<UsageSummary>(`${USAGE_BASE}/summary`, {
    params: usageParams(params),
  })
  return data
}

export async function usageByFeature(
  params: UsageQueryParams = {},
): Promise<UsageFeatureBucket[]> {
  const { data } = await axios.get<UsageFeatureBucket[]>(
    `${USAGE_BASE}/by-feature`,
    { params: usageParams(params) },
  )
  return data
}

export async function usageByModel(
  params: UsageQueryParams = {},
): Promise<UsageModelBucket[]> {
  const { data } = await axios.get<UsageModelBucket[]>(
    `${USAGE_BASE}/by-model`,
    { params: usageParams(params) },
  )
  return data
}

export async function usageByCharacter(
  params: UsageQueryParams = {},
): Promise<UsageCharacterBucket[]> {
  const { data } = await axios.get<UsageCharacterBucket[]>(
    `${USAGE_BASE}/by-character`,
    { params: usageParams(params) },
  )
  return data
}

export async function usageByCharacterFeature(
  params: UsageQueryParams = {},
): Promise<UsageCharacterFeatureBucket[]> {
  const { data } = await axios.get<UsageCharacterFeatureBucket[]>(
    `${USAGE_BASE}/by-character-feature`,
    { params: usageParams(params) },
  )
  return data
}

export async function listUsageEvents(
  params: UsageQueryParams = {},
): Promise<UsageEventRow[]> {
  const { data } = await axios.get<UsageEventRow[]>(`${USAGE_BASE}/events`, {
    params: usageParams({ ...params, limit: params.limit ?? 50 }),
  })
  return data
}

export async function exportUsageEventsCsv(
  params: UsageQueryParams = {},
): Promise<Blob> {
  const { data } = await axios.get<Blob>(`${USAGE_BASE}/events.csv`, {
    params: usageParams({ ...params, limit: params.limit ?? 500 }),
    responseType: 'blob',
  })
  return data
}

export async function listPersonaCuriosityAttempts(
  params: {
    characterId: string
    operatorId?: string
    limit?: number
  },
): Promise<PersonaCuriosityAttempt[]> {
  const { data } = await axios.get<PersonaCuriosityAttempt[]>(
    `${BASE}/persona-curiosity/attempts`,
    {
      params: {
        character_id: params.characterId,
        operator_id: params.operatorId ?? 'default',
        limit: params.limit ?? 50,
      },
    },
  )
  return data
}

export async function personaCuriosityMetrics(
  params: {
    characterId: string
    operatorId?: string
    sinceHours?: number
  },
): Promise<PersonaCuriosityMetrics> {
  const { data } = await axios.get<PersonaCuriosityMetrics>(
    `${BASE}/metrics/persona-curiosity`,
    {
      params: {
        character_id: params.characterId,
        operator_id: params.operatorId ?? 'default',
        since_hours: params.sinceHours ?? 72,
      },
    },
  )
  return data
}

export async function listEmotionEvents(
  params: {
    characterId: string
    operatorId?: string
    sinceHours?: number
    limit?: number
  },
): Promise<EmotionEventRow[]> {
  const { data } = await axios.get<EmotionEventRow[]>(`${BASE}/emotion-events`, {
    params: {
      character_id: params.characterId,
      operator_id: params.operatorId ?? 'default-operator',
      since_hours: params.sinceHours ?? 24,
      limit: params.limit ?? 100,
    },
  })
  return data
}

/**
 * Subsystem health trend dashboard payload.
 */
export interface SubsystemHealthMetrics {
  window_hours: number
  emotion_causality_ratio: number
  emotion_causality_by_kind: Record<string, number>
  proactive_send_ratio: number
  proactive_intention_skipped_ratio: number
  proactive_gate_blocked_ratio: number
  emotion_followup_window_hours: number
  emotion_followup_count: number
  emotion_high_intensity_total: number
  emotion_followup_ratio: number
}

export async function subsystemHealthMetrics(
  params: {
    characterId: string
    operatorId?: string
    sinceHours?: number
    followupHours?: number
  },
): Promise<SubsystemHealthMetrics> {
  const { data } = await axios.get<SubsystemHealthMetrics>(
    `${BASE}/metrics/subsystem-health`,
    {
      params: {
        character_id: params.characterId,
        operator_id: params.operatorId ?? 'default',
        since_hours: params.sinceHours ?? 72,
        followup_hours: params.followupHours ?? 24,
      },
    },
  )
  return data
}

/**
 * HUMANIZATION_ROADMAP §3.1 —— 人格演化軌跡 audit row.
 *
 * Source: ``GET /admin/observability/disposition-drift?character_id=...``
 */
export interface DispositionDriftRecord {
  id: string
  character_id: string
  dimension: 'self_centeredness' | 'candor' | 'sharing_drive' | 'associativeness'
  from_band: 'low' | 'medium' | 'high'
  to_band: 'low' | 'medium' | 'high'
  reason: string
  evidence_quote: string
  decided_at: string
}

export async function listDispositionDrift(
  params: { characterId: string; limit?: number },
): Promise<DispositionDriftRecord[]> {
  const { data } = await axios.get<DispositionDriftRecord[]>(
    `${BASE}/disposition-drift`,
    {
      params: {
        character_id: params.characterId,
        limit: params.limit ?? 20,
      },
    },
  )
  return data
}

/**
 * HUMANIZATION_ROADMAP §4.5 — quiet hours window.
 */
export interface QuietHours {
  start: number
  end: number
}

export async function getQuietHours(): Promise<QuietHours> {
  const { data } = await axios.get<QuietHours>(
    `/api/v1/admin/app-settings/quiet-hours`,
  )
  return data
}

export async function setQuietHours(window: QuietHours): Promise<QuietHours> {
  const { data } = await axios.put<QuietHours>(
    `/api/v1/admin/app-settings/quiet-hours`,
    window,
  )
  return data
}

/**
 * HUMANIZATION_ROADMAP §4.5 — per-kind latency percentile report. No SLO
 * numbers, owner decided to keep this descriptive (trend only).
 */
export interface LatencyKindStats {
  kind: string
  count: number
  p50_ms: number | null
  p90_ms: number | null
  p95_ms: number | null
  p99_ms: number | null
  max_ms: number | null
}

export interface LatencyReport {
  window_hours: number
  overall_count: number
  per_kind: LatencyKindStats[]
}

export async function latencyReport(
  params: { sinceHours?: number } = {},
): Promise<LatencyReport> {
  const { data } = await axios.get<LatencyReport>(`${BASE}/latency-report`, {
    params: { since_hours: params.sinceHours ?? 24 },
  })
  return data
}

/**
 * HUMANIZATION_ROADMAP §4.4 / §4.1 — read-only humanization flag snapshot.
 *
 * All flags are env-driven; the UI surfaces them so operators don't have
 * to grep ``.env``. To change a flag the operator still has to edit env
 * vars and restart — the response carries the env-prefix so the UI can
 * spell out the env var name next to each toggle.
 */
export interface HumanizationFlags {
  relationship_milestone_enabled: boolean
  disposition_drift_enabled: boolean
  self_reflection_enabled: boolean
  behavioral_pattern_enabled: boolean
  deferred_intent_enabled: boolean
  route_b_enabled: boolean
  body_state_enabled: boolean
  subjective_time_enabled: boolean
  address_preference_enabled: boolean
  env_prefix: string
}

export async function getHumanizationFlags(): Promise<HumanizationFlags> {
  const { data } = await axios.get<HumanizationFlags>(
    `/api/v1/admin/app-settings/humanization-flags`,
  )
  return data
}

export async function getPersonaCuriosityFlags(): Promise<PersonaCuriosityFlags> {
  const { data } = await axios.get<PersonaCuriosityFlags>(
    `/api/v1/admin/app-settings/persona-curiosity-flags`,
  )
  return data
}
