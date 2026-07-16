export type ProactiveOutcome =
  | 'disabled'
  | 'gate_blocked'
  | 'no_binding'
  | 'decider_skipped'
  | 'sent'
  | 'errored'

export type ProactiveTrigger =
  | 'tick'
  | 'post_turn'
  | 'activity_transition'

export interface ProactiveAttempt {
  id: string
  character_id: string
  trigger: ProactiveTrigger | string
  outcome: ProactiveOutcome | string
  reason: string
  binding_id: string | null
  message: string | null
  metadata: Record<string, unknown>
  decided_at: string
}

export interface ProactiveEvaluateResponse {
  ok: boolean
  attempt: ProactiveAttempt | null
  message?: string | null
}
