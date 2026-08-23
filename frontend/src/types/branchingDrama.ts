import type { FusionToArcOperatorMode } from '@/types/fusionStory'

export type BranchingDramaStatus =
  | 'generating_outlines'
  | 'generating_images'
  | 'ready'
  | 'failed'

/**
 * Where the player stands inside the drama (BD2). Closed vocabulary —
 * the backend plans and narrates the whole tree against this value:
 * `central` (主演), `present` (在場配角), `absent` (旁觀者).
 */
export type DramaOperatorPosition = 'central' | 'present' | 'absent'

export const DRAMA_OPERATOR_POSITIONS: DramaOperatorPosition[] = [
  'central',
  'present',
  'absent',
]

/** What the create form starts on, matching the backend default. */
export const DEFAULT_DRAMA_OPERATOR_POSITION: DramaOperatorPosition = 'central'

/** Mirrors the entity ceiling (`OPERATOR_NOTE_MAX_CHARS`). */
export const DRAMA_OPERATOR_NOTE_MAX_CHARS = 120

/**
 * The look every scene image of one drama is drawn in (BD10). Closed
 * vocabulary, shared with the per-character `visual_generation_style` —
 * except that the character-level slot has a fourth `''` ("inherit") state
 * and this one does not: a drama is one production and states its own art
 * direction outright.
 */
export type DramaVisualStyle = 'anime' | 'realistic'

export const DRAMA_VISUAL_STYLES: DramaVisualStyle[] = ['anime', 'realistic']

/** Matches `VISUAL_GENERATION_STYLE_DEFAULT` on the backend. */
export const DEFAULT_DRAMA_VISUAL_STYLE: DramaVisualStyle = 'anime'

/**
 * Mirrors the *create-path* ceiling (`MAX_TOTAL_SEGMENTS`, BD4). Node count
 * grows as `3^N`, so this bounds worst-case generation fan-out — keep in
 * sync with `BranchingDrama.create_pending` and the DTO's `le=` in
 * `application/dto/branching_drama.py`.
 *
 * It bounds the create form only. Dramas made under an older, higher cap
 * still list and play, so nothing that *renders* a drama may clamp to this
 * number — the entity constructor deliberately does not either (FX3).
 */
export const MAX_DRAMA_TOTAL_SEGMENTS = 12

/** The other half of the same DTO bound (`ge=2`): a drama needs a branch. */
export const MIN_DRAMA_TOTAL_SEGMENTS = 2

/**
 * The three-way branch every non-root node offers (mirrors `VALID_TONES` /
 * `TONE_DARK` / `TONE_SUNNY` / `TONE_NEUTRAL` in `branching_drama.py`).
 * Closed vocabulary — the wire value is this English enum, and it must
 * never reach the player un-translated (BD11): route it through
 * `dramaToneLabel` in `utils/dramaTone.ts` instead of rendering it raw.
 */
export type DramaTone = 'dark' | 'sunny' | 'neutral'

export const DRAMA_TONES: DramaTone[] = ['dark', 'sunny', 'neutral']

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

/**
 * One picture the player walked past (BD9). Safe to show whole — it is a
 * scene they already read.
 */
export interface CollectedScene {
  node_id: string
  title: string
  depth: number
  tone: string | null
  image_path: string
}

/**
 * 劇場圖集 — what this player collected out of one drama's painted tree.
 *
 * The un-walked pictures arrive as `locked_count` and nothing else: the
 * backend never sends their titles, summaries or paths, so a silhouette tile
 * has nothing to spoil even if a future view tried to render more. Do not
 * add fields here expecting the server to fill them — the omission is the
 * feature.
 *
 * `total_with_images` counts the nodes of the *whole* tree that lazy
 * generation has already painted — walked or not — so it runs *ahead* of the
 * player: the prefetch paints branches nobody ever enters. That is exactly
 * why it is no longer a progress denominator (D8.2): the 「已收集 X/Y」 ratio
 * it produced was both low and flat — pinned near 1/3 only at
 * `KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH=1`, down in the 1/9–15% band at the
 * default depth of 2 — and never equal to what the player walked. Completion is
 * measured against the whole tree instead (`utils/dramaProgress`). The field
 * stays on the wire untouched; nothing on this page divides by it any more —
 * and it is not a count of the pictures this player collected either.
 */
export interface DramaSceneGallery {
  collected: CollectedScene[]
  locked_count: number
  total_with_images: number
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
  operator_position: DramaOperatorPosition
  operator_note: string | null
  /**
   * The look this drama's scene images are drawn in (BD10). `null` only for
   * a drama created before the slot existed — those still style themselves
   * off their first character, so the detail view must render nothing
   * rather than claim a look they do not have.
   */
  visual_style: DramaVisualStyle | null
  expected_node_count: number
  /**
   * Node count pre-generated synchronously at creation (root + prefetch
   * layers) — the correct denominator for the early `generating_outlines`
   * progress bar. `expected_node_count` above is the full-tree total
   * (only ever fully realized after lazy generation catches up while the
   * player advances), so dividing by it there pins the bar near 0%.
   */
  initial_node_target: number
  generated_node_count: number
  first_scene_image_path: string | null
  /**
   * Root node id, so the detail view's title image can name the node it
   * wants redrawn (BD6). Present even when `first_scene_image_path` is
   * `null` — a node with no picture is exactly what the redraw repairs.
   */
  first_scene_node_id: string | null
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

/**
 * The fixed price this client had on screen when the player pressed (BD3).
 * The server binds the charge to it and answers `409 price_changed` if the
 * published price moved, rather than silently charging a number nobody saw.
 * Absent whenever nothing is quotable — self-host, a token-billed tier, a
 * price list that never loaded — in which case the server falls back to its
 * own cache exactly as before.
 */
export interface QuotedActionPrice {
  quoted_price_cr?: number
}

export interface CreateBranchingDramaPayload extends QuotedActionPrice {
  character_ids: string[]
  prompt: string
  total_segments: number
  /**
   * Optional on the wire: the backend defaults it to `central`, so an
   * entry point that has no opinion (the fusion 「換個玩法」 handoff) can
   * simply leave it out and get the framing dramas always had.
   */
  operator_position?: DramaOperatorPosition
  operator_note?: string | null
  /**
   * Optional on the wire for the same reason `operator_position` is: an
   * entry point with no opinion (the fusion 「換個玩法」 handoff) leaves it
   * out, and the backend resolves the first character's style *and stores
   * it*. Omitting it never means "anime" — it means "you decide, once".
   */
  visual_style?: DramaVisualStyle
}

/**
 * What `interact` posts. Named rather than inlined at the call site because
 * an object literal handed straight to the generic `withQuotedPrice` is
 * excess-property-checked against its `QuotedActionPrice` constraint, which
 * rejects `player_input` outright.
 */
export interface InteractSessionPayload extends QuotedActionPrice {
  player_input: string
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

/**
 * BD7 — 把走過的這條路寫成劇本. Both fields optional in both directions:
 * an omitted `operator_mode` is a player who did not touch the chips, and
 * the backend answers that with the framing the drama was actually played
 * under (see `DRAMA_POSITION_TO_ARC_MODE`) rather than a blanket default.
 */
export interface DramaToArcDraftPayload {
  instruction?: string
  operator_mode?: FusionToArcOperatorMode
}

/**
 * The conversion mode a drama's own player position implies (BD7).
 *
 * A pre-fill, never a lock — the exit hub shows all three chips, and a
 * player who watched a drama may still ask to be written into the arc it
 * becomes. Kept beside the position vocabulary it reads so the two closed
 * sets cannot drift apart silently; the backend holds the same table
 * (`contracts/drama_to_arc.operator_mode_for_drama_position`) because the
 * server must answer a client that sends nothing at all.
 */
export const DRAMA_POSITION_TO_ARC_MODE: Record<
  DramaOperatorPosition,
  FusionToArcOperatorMode
> = {
  central: 'write_in',
  present: 'observer',
  absent: 'unchanged',
}
