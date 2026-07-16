/**
 * Arc-template wizard types (Phase 2.7 frontend of SCENE_BEAT_PLAN).
 *
 * Mirror of `src/kokoro_link/api/routes/arc_template_intake.py`. The
 * wizard is stateless on the server — every step posts the partial
 * draft back; the client owns the canonical draft.
 */

import type { ArcTemplate, ArcTemplateBeat } from './arcTemplate'

export interface SuggestMetaResponse {
  titles: string[]
  themes: string[]
  tones: string[]
  world_frames: string[]
}

export interface CondensePremisePayload {
  logline: string
  start_state?: string
  end_state?: string
  tone?: string
}

export interface BeatContextPayload {
  template_title: string
  premise: string
  theme: string
  tone: string
  duration_days: number
  world_frames: string[]
  beat_position: number
  total_beats: number
  day_offset: number
  tension: string
  prior_titles: string[]
}

export interface SuggestBeatOptionsResponse {
  titles: string[]
  locations: string[]
  scene_characters: string[]
  dramatic_questions: string[]
  scene_types: string[]
}

export interface BeatDraftPayload {
  sequence: number
  day_offset: number
  title: string
  summary: string
  tension: string
  scene_type: string
  location: string | null
  scene_characters: string[]
  dramatic_question: string | null
  required: boolean
}

export interface TemplateDraftPayload {
  id: string
  title: string
  premise: string
  theme: string
  tone: string
  duration_days: number
  world_frames: string[]
  required_traits: string[]
  applicability_scope: 'generic' | 'character_bound'
  target_character_ids: string[]
  beats: BeatDraftPayload[]
}

export interface SaveTemplateResponse {
  template_id: string
  template: ArcTemplate
}

export interface RhythmPattern {
  id: string
  /**
   * Backend no longer ships display strings (plan #4 / D6). Kept
   * optional so the wizard can translate `id` via its i18n bundle while
   * tolerating older backends that still send `label`/`description`.
   */
  label?: string
  description?: string
  recommended_duration: [number, number]
  recommended_beat_count: [number, number]
  default_distribution_14d: Array<{
    day_offset: number
    tension: string
    scene_type: string
  }>
}

export interface ScaffoldsResponse {
  rhythm_patterns: RhythmPattern[]
  // `id` is the stable enum; labels/descriptions are translated on the
  // frontend (plan #4 / D6). `label`/`description` are optional legacy.
  tones: Array<{ id: string; label?: string; description?: string }>
  themes: Array<{ id: string; label?: string }>
  scene_types: Array<{ id: string; label?: string }>
  world_frames: string[]
}

/** Convenience: an empty beat draft factory for the wizard. */
export function blankBeatDraft(sequence: number, day_offset: number): BeatDraftPayload {
  return {
    sequence,
    day_offset,
    title: '',
    summary: '',
    tension: 'rising',
    scene_type: 'encounter',
    location: null,
    scene_characters: [],
    dramatic_question: null,
    required: true,
  }
}

/** Build a `BeatDraftPayload` from an existing `ArcTemplateBeat` (for editing). */
export function beatDraftFromTemplate(beat: ArcTemplateBeat): BeatDraftPayload {
  return {
    sequence: beat.sequence,
    day_offset: beat.day_offset,
    title: beat.title,
    summary: beat.summary,
    tension: beat.tension,
    scene_type: beat.scene_type,
    location: beat.location,
    scene_characters: [...beat.scene_characters],
    dramatic_question: beat.dramatic_question,
    required: beat.required,
  }
}
