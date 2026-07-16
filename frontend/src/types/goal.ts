export type GoalStatus = 'active' | 'paused' | 'done' | 'abandoned'

export interface Goal {
  id: string
  character_id: string
  content: string
  status: GoalStatus
  priority: number
  origin: string
  tags: string[]
  created_at: string
  last_progressed_at?: string | null
  review_notes?: string | null
}

export interface CreateGoalRequest {
  content: string
  priority?: number
  tags?: string[]
}

export interface UpdateGoalRequest {
  content?: string
  status?: GoalStatus
  priority?: number
  notes?: string
}
