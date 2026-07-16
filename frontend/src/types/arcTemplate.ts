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
