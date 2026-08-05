/**
 * Arc template types — mirror of backend
 * `kokoro_link.api.routes.arc_templates.ArcTemplateResponse` (Phase 2 of
 * SCENE_BEAT_PLAN). Templates are read-only at runtime; the frontend
 * only consumes them via `GET /api/v1/arc-templates`.
 */

export type ArcTemplateSceneType =
  | 'encounter'
  | 'revelation'
  | 'conflict'
  | 'resolution'
  | 'interlude'

export type ArcTemplateTension =
  | 'setup'
  | 'rising'
  | 'climax'
  | 'falling'
  | 'resolution'

/**
 * The operator's (player's) dramatic place in a beat (OP0). Closed
 * three-value enum; `null` = unjudged — never coerce a missing value
 * to `'absent'`, that's a judgement call only the LLM or the operator
 * gets to make (see docs/plans/ARC_PLAYER_POSITION_PLAN.md §3.1).
 */
export type OperatorPosition = 'absent' | 'present' | 'central'

export interface ArcTemplateBeat {
  sequence: number
  day_offset: number
  title: string
  summary: string
  tension: ArcTemplateTension
  scene_type: string
  location: string | null
  scene_characters: string[]
  dramatic_question: string | null
  required: boolean
  operator_position: OperatorPosition | null
  operator_note: string | null
}

export interface ArcTemplateBinding {
  world_frames: string[]
  required_traits: string[]
}

export interface ArcTemplate {
  id: string
  title: string
  premise: string
  theme: string
  tone: string
  /**
   * Authored-prose language tag (metadata only). Surfaced as a
   * source-language badge in the picker; drives the "翻成我的語言"
   * preview toggle. Never used to filter the catalogue.
   */
  language: string
  duration_days: number
  beat_count: number
  applicability_scope: 'generic' | 'character_bound'
  target_character_ids: string[]
  binding: ArcTemplateBinding
  beats: ArcTemplateBeat[]
}
