export interface ParticipantRef {
  actor_kind: 'operator' | 'character' | 'npc' | string
  actor_id: string | null
  display_name: string
  role: string | null
}

export interface ScheduleActivity {
  id: string
  start_at: string
  end_at: string
  description: string
  category: string
  location: string | null
  busy_score: number
  memorialized: boolean
  has_memory: boolean
  companion_names: string[]
  participant_refs: ParticipantRef[]
  scene_privacy?: 'public' | 'semi_public' | 'private' | 'intimate' | null
  meeting_affordance?: 'open_to_encounter' | 'invite_only' | 'not_available' | null
}

export interface DailySchedule {
  id: string
  character_id: string
  date: string
  generated_at: string
  activities: ScheduleActivity[]
}

export interface CurrentActivitySnapshot {
  now: string
  current: ScheduleActivity | null
  upcoming: ScheduleActivity[]
}
