export type FusionStoryStatus =
  | 'planning'
  | 'writing'
  | 'polishing'
  | 'ready'
  | 'failed'

export interface FusionStoryBeat {
  id: string
  sequence: number
  act: string
  title: string
  hook: string
  dramatic_question: string
  target_chars: number
  actual_chars: number
  content: string
  focus_character_ids: string[]
}

export interface FusionStoryVersion {
  id: string
  story_id: string
  version_number: number
  title: string
  premise: string
  theme: string
  full_text: string
  iteration_label: string
  created_at: string
}

export interface FusionStoryProgress {
  stage: FusionStoryStatus
  beats_total: number
  beats_done: number
  percent: number | null
}

export interface FusionStory {
  id: string
  character_ids: string[]
  prompt: string
  title: string
  premise: string
  theme: string
  status: FusionStoryStatus
  head_version: number
  full_text: string
  error_message: string | null
  /**
   * Machine-readable failure reason for a `failed` job (plan U4b), from
   * an open set the backend forwards verbatim. `insufficient_credits`
   * is the only value the SPA acts on; anything else (or `null`, for an
   * ordinary crash) falls through to the generic failure copy.
   */
  error_code?: string | null
  progress: FusionStoryProgress
  beats: FusionStoryBeat[]
  versions: FusionStoryVersion[]
  created_at: string
  updated_at: string
}

export interface FusionStorySummary {
  id: string
  character_ids: string[]
  title: string
  premise: string
  status: FusionStoryStatus
  head_version: number
  error_message: string | null
  error_code?: string | null
  progress: FusionStoryProgress
  total_chars: number
  created_at: string
  updated_at: string
}

/**
 * The fixed action price this client had on screen (plan R9 quote binding).
 *
 * Attached by the transport, never by a caller: the server binds the charge to
 * it and answers `409 price_changed` when the published number moved, so the
 * player is never billed an amount they were not shown. Absent on self-host and
 * on token-billed tiers, where there is no fixed price to quote.
 */
export interface QuotedActionPrice {
  quoted_price_cr?: number
}

export interface CreateFusionStoryPayload extends QuotedActionPrice {
  character_ids: string[]
  prompt: string
}

export interface IterateOutlinePayload extends QuotedActionPrice {
  hint?: string | null
}

export interface IterateBeatPayload extends QuotedActionPrice {
  beat_index: number
  hint?: string | null
}

export type PolishFusionStoryPayload = QuotedActionPrice

/**
 * How the creator enters the story they just wrote (OP1-C / plan 拍板 #2).
 *
 * - `write_in`  — rewrite the beats so the player has a real place in them
 * - `observer`  — story untouched, the player is there watching it happen
 * - `unchanged` — a pure character story; the player is in none of it
 *
 * Omitting the field is the same as `unchanged` server-side: a caller who
 * never saw the choice must not have their prose rewritten. The UI always
 * sends an explicit value.
 */
export type FusionToArcOperatorMode = 'write_in' | 'observer' | 'unchanged'

export const FUSION_TO_ARC_OPERATOR_MODES: FusionToArcOperatorMode[] = [
  'write_in',
  'observer',
  'unchanged',
]

export interface FusionToArcDraftPayload {
  instruction?: string | null
  operator_mode?: FusionToArcOperatorMode
}
