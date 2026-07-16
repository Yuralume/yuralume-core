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
  progress: FusionStoryProgress
  total_chars: number
  created_at: string
  updated_at: string
}

export interface CreateFusionStoryPayload {
  character_ids: string[]
  prompt: string
}

export interface IterateOutlinePayload {
  hint?: string | null
}

export interface IterateBeatPayload {
  beat_index: number
  hint?: string | null
}

export interface FusionToArcDraftPayload {
  instruction?: string | null
}
