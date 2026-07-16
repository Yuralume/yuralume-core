export interface ArcSeriesBinding {
  world_frames: string[]
  required_traits: string[]
}

export interface ArcSeriesMember {
  template_id: string
  position: number
}

export interface ArcSeries {
  id: string
  title: string
  premise: string
  theme: string
  tone: string
  binding: ArcSeriesBinding
  members: ArcSeriesMember[]
  member_count: number
  is_pack: boolean
}

export interface ArcSeriesPayload {
  id?: string | null
  title: string
  premise: string
  theme: string
  tone: string
  world_frames: string[]
  required_traits: string[]
  template_ids: string[]
}

export interface ReorderArcSeriesPayload {
  template_ids: string[]
}

export interface BindArcSeriesPayload {
  character_id: string
}

export interface DraftNextSeasonPayload {
  character_id: string
  instruction?: string
  selected_memory_ids?: string[]
}

export interface ArcSeriesProgress {
  character_id: string
  series_id: string
  current_index: number
  status: string
  last_arc_id: string | null
}
