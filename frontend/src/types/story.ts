export interface StoryEvent {
  id: string
  character_id: string
  date: string
  seed_id: string
  narrative: string
  emotional_tone: string | null
  memorialized: boolean
  created_at: string
}

export interface StorySeed {
  id: string
  seed_text: string
  tags: string[]
  world_frames: string[]
  weight: number
  cooldown_days: number
  enabled: boolean
  character_id: string | null
  external_id: string | null
  pack_id: string | null
  created_at: string
  updated_at: string
}

export interface CreateStorySeedRequest {
  seed_text: string
  tags?: string[]
  world_frames?: string[]
  weight?: number
  cooldown_days?: number
}

export interface UpdateStorySeedRequest {
  seed_text?: string
  tags?: string[]
  world_frames?: string[]
  weight?: number
  cooldown_days?: number
  enabled?: boolean
}
