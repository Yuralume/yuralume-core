export type BranchingDramaStatus =
  | 'generating_outlines'
  | 'generating_images'
  | 'ready'
  | 'failed'

export interface DramaNode {
  id: string
  drama_id: string
  parent_node_id: string | null
  depth: number
  tone: string | null
  title: string
  summary: string
  appearing_character_ids: string[]
  image_path: string | null
}

export interface Exchange {
  player_input: string
  response: string
}

export interface DramaSessionTurn {
  node_id: string
  narration: string
  player_input: string
  chosen_tone: string | null
  exchanges: Exchange[]
}

export interface DramaSession {
  id: string
  drama_id: string
  current_node_id: string
  status: 'playing' | 'ended'
  turns: DramaSessionTurn[]
  created_at: string
  updated_at: string
}

export interface BranchingDrama {
  id: string
  character_ids: string[]
  prompt: string
  title: string
  total_segments: number
  status: BranchingDramaStatus
  error_message: string | null
  /**
   * Machine-readable failure reason for a `failed` job (plan U4b), from
   * an open set the backend forwards verbatim. `insufficient_credits`
   * is the only value the SPA acts on; anything else (or `null`, for an
   * ordinary crash) falls through to the generic failure copy.
   */
  error_code?: string | null
  expected_node_count: number
  generated_node_count: number
  first_scene_image_path: string | null
  warning: string | null
  created_at: string
  updated_at: string
}

export interface BranchingDramaSummary {
  id: string
  character_ids: string[]
  title: string
  total_segments: number
  status: BranchingDramaStatus
  error_message: string | null
  error_code?: string | null
  created_at: string
  updated_at: string
}

export interface CreateBranchingDramaPayload {
  character_ids: string[]
  prompt: string
  total_segments: number
}

export interface InteractSessionResponse {
  session: DramaSession
  response: string
  advance_hint: string | null
}

export interface AdvanceSessionResponse {
  session: DramaSession
  current_node: DramaNode
  is_ending: boolean
}
