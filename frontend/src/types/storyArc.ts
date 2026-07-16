export type StoryArcStatus = 'active' | 'completed' | 'abandoned'
export type StoryArcBeatStatus =
  | 'pending'
  | 'active'
  | 'realized'
  | 'skipped'
export type StoryArcTension =
  | 'setup'
  | 'rising'
  | 'climax'
  | 'falling'
  | 'resolution'

export interface StoryArcBeat {
  id: string
  arc_id: string
  sequence: number
  scheduled_date: string // YYYY-MM-DD
  title: string
  summary: string
  tension: StoryArcTension
  status: StoryArcBeatStatus
  realized_event_id: string | null
  /**
   * Phase 1 scene-structure fields. Older beats persisted before
   * Phase 1 read back with safe defaults (``encounter`` / null / [] /
   * required=true) so the UI can render uniformly without null guards
   * everywhere.
   */
  scene_characters: string[]
  location: string | null
  dramatic_question: string | null
  scene_type: string
  required: boolean
}

export interface StoryArc {
  id: string
  character_id: string
  title: string
  premise: string
  theme: string
  start_date: string // YYYY-MM-DD
  end_date: string // YYYY-MM-DD
  status: StoryArcStatus
  beats: StoryArcBeat[]
  created_at: string
  updated_at: string
}

export interface StartStoryArcPayload {
  hint?: string
  duration_days?: number
  beat_count?: number
}

export interface UpdateStoryArcMetaPayload {
  title?: string
  premise?: string
  theme?: string
}

export interface AddStoryArcBeatPayload {
  scheduled_date: string
  title: string
  summary: string
  tension?: StoryArcTension
}

export interface UpdateStoryArcBeatPayload {
  scheduled_date?: string
  title?: string
  summary?: string
  tension?: StoryArcTension
}
