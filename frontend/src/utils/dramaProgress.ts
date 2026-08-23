import type { DramaSession, DramaTone } from '@/types/branchingDrama'
import { MIN_DRAMA_TOTAL_SEGMENTS } from '@/types/branchingDrama'

/**
 * 分歧劇場完成度 (BD 計畫 D8) — how much of one drama's *tree* a player has
 * actually walked, and where in that tree each playthrough went.
 *
 * Pure arithmetic over data the client already holds (`listSessions`), so
 * nothing here talks to the server and nothing here needs a new field on
 * the wire.
 *
 * ## The anti-spoiler boundary this file lives inside
 *
 * What comes out of here is *shape*: depths and layer indices. Never a
 * title, a summary, an image path, or an un-walked node's id. The gallery
 * payload (BD9) still refuses to name a node the player has not reached, and
 * the positions computed below are derived purely from the player's *own*
 * tone history — they say 「你往左、往左、往右」, not 「那邊有一場叫 X 的戲」.
 * Drawing the outline of a tree whose branch factor the create form already
 * states is not a leak; naming what sits on an un-walked branch is, and that
 * data is not in this process.
 */

/** Every non-root node offers this many tonal children — mirrors `BRANCH_FACTOR`. */
export const DRAMA_BRANCH_FACTOR = 3

/**
 * The order the three tones are laid out in, left to right, at every layer.
 *
 * Its own constant rather than a reuse of `DRAMA_TONES` because this order is
 * *load-bearing geometry*: a node's position is `parent * 3 + toneBranchIndex`,
 * so reordering this array silently redraws every branching graph and moves
 * every walked node. `DRAMA_TONES` exists to enumerate a vocabulary and is
 * free to be reordered for display; this one is not. A test pins the two to
 * the same membership so a fourth tone cannot appear on one side only.
 */
export const DRAMA_TONE_BRANCH_ORDER: readonly DramaTone[] = [
  'dark',
  'sunny',
  'neutral',
]

/**
 * Deepest tree this module will do arithmetic against.
 *
 * Deliberately *not* `MAX_DRAMA_TOTAL_SEGMENTS` (12): that is a create-path
 * policy number, and dramas written under an older, higher cap must still
 * render — clamping to it would under-report their denominator by orders of
 * magnitude. This is an arithmetic ceiling instead. `3 ** n` is a JS double:
 * past n = 33 the node count stops being an exact integer, and long before
 * that it has stopped meaning anything (a 20-segment tree is already
 * 1,743,392,200 nodes — no player moves that percentage off 0.0). So a
 * deeper-than-20 drama is measured against a 20-deep tree, which can only
 * make the percentage kinder and can never produce `NaN`, `Infinity`, or a
 * layer loop nobody can draw.
 */
export const DRAMA_TREE_SEGMENT_CEILING = 20

/**
 * Where a tone sits among its siblings, or `-1` for a tone this client
 * cannot place.
 *
 * `-1` is a real answer, not an error: a turn carrying a tone outside the
 * closed vocabulary means *every* position below it is unknowable (each
 * index is built by multiplying the parent's), so callers must stop
 * descending rather than guess.
 */
export function toneBranchIndex(tone: string | null | undefined): number {
  if (typeof tone !== 'string') return -1
  return DRAMA_TONE_BRANCH_ORDER.indexOf(tone as DramaTone)
}

/**
 * The tree depth to measure against: a whole number of layers inside
 * `[MIN_DRAMA_TOTAL_SEGMENTS, DRAMA_TREE_SEGMENT_CEILING]`, or `0` for
 * "no readable answer".
 *
 * Three behaviours, all deliberate:
 *
 * - **not a finite number** (`NaN`, `Infinity`, a missing field that typed
 *   as `number` anyway) → `0`. There is no honest denominator, and callers
 *   render nothing rather than a made-up percentage.
 * - **finite but below the floor** → raised to `MIN_DRAMA_TOTAL_SEGMENTS`.
 *   The backend enforces that floor on *reads* as well as writes
 *   (`BranchingDrama.__post_init__`), so a 0 or 1 reaching here is corrupt
 *   rather than historical; the shallowest tree that can exist is the
 *   honest reading of it.
 * - **above the ceiling** → clamped (see `DRAMA_TREE_SEGMENT_CEILING`).
 */
export function resolveTreeSegments(totalSegments: number): number {
  if (!Number.isFinite(totalSegments)) return 0
  const whole = Math.trunc(totalSegments)
  if (whole < MIN_DRAMA_TOTAL_SEGMENTS) return MIN_DRAMA_TOTAL_SEGMENTS
  return Math.min(whole, DRAMA_TREE_SEGMENT_CEILING)
}

/**
 * Nodes in the *whole* tree: `(3^N - 1) / 2`, the same closed form the
 * entity uses (`BranchingDrama.expected_node_count`).
 *
 * This — not "pictures that exist" — is D8.1's denominator. `total_with_images`
 * counts the nodes of the *whole* tree that lazy generation has already
 * painted, walked or not (`build_scene_gallery` sums every node carrying an
 * `image_path`), so it runs *ahead* of the player: the prefetch paints
 * branches nobody ever enters. The old 「已收集 X/Y」 ratio was therefore both
 * low and flat — pinned near 1/3 only at
 * `KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH=1`, and down in the 1/9–15% band at the
 * default depth of 2, where every step paints *two* layers of the subtree
 * ahead (six beats into a six-act line: walked 6, painted ≈40). Under no
 * setting does painted equal walked — at depth 0, or with no scene-image port
 * wired, it stays 0 while the walk keeps growing.
 *
 * Returns `0` when the segment count is unreadable; callers must treat that
 * as "unknown" and never divide by it.
 */
export function fullTreeNodeCount(totalSegments: number): number {
  const segments = resolveTreeSegments(totalSegments)
  if (segments <= 0) return 0
  return (DRAMA_BRANCH_FACTOR ** segments - 1) / (DRAMA_BRANCH_FACTOR - 1)
}

/** One node's coordinates in the tree: which layer, and where across it. */
export interface DramaTreeNodePosition {
  /** 0 = root. Equals the index of the turn that reached it. */
  depth: number
  /** Position across that layer, `0 .. 3^depth - 1`. */
  index: number
}

/** One playthrough, as a root-first run of coordinates. */
export interface DramaWalkedPath {
  sessionId: string
  nodes: DramaTreeNodePosition[]
  /**
   * The path stopped short of the turns actually stored — an unplaceable
   * tone, or turns past the bottom of the tree. The nodes present are still
   * correct; there is simply no honest way to draw the rest.
   */
  truncated: boolean
}

/** Everything a branching graph needs to draw itself. */
export interface DramaWalkedTree {
  /** Number of layers; `0` when the segment count was unreadable. */
  depth: number
  /** `levels[d]` = the layer-`d` indices this player has stood on. */
  levels: Set<number>[]
  /** One entry per session that walked at least one placeable node. */
  paths: DramaWalkedPath[]
}

/**
 * Turn the player's tone history into positions in the tree.
 *
 * ## The mapping
 *
 * A node is identified by the tone sequence that reaches it from the root,
 * so with a stable sibling order (`DRAMA_TONE_BRANCH_ORDER`) the tree is a
 * base-3 numbering:
 *
 * - root — depth 0, index 0
 * - child — `index = parentIndex * 3 + toneBranchIndex(tone)`
 * - depth d — `index = Σ toneBranchIndex(tone_k) * 3^(d-k)` for k = 1..d
 *
 * ## Why turn index is depth
 *
 * `start_session` opens with exactly one turn — the root, `chosen_tone` null
 * — and `advance_session` appends exactly one turn per layer descended,
 * carrying the tone of the child it moved to. `interact_session` appends
 * *exchanges* to the last turn and never a turn. So `turns[k]` is the node at
 * depth `k`, and `turns[k].chosen_tone` is the branch taken out of depth
 * `k-1`. `turns[0].chosen_tone` is ignored: the root is index 0 whatever a
 * stored row claims.
 *
 * Nothing but coordinates comes out — no node ids, no titles (see the file
 * header).
 */
export function buildWalkedTree(
  sessions: readonly DramaSession[] | null | undefined,
  totalSegments: number,
): DramaWalkedTree {
  const depth = resolveTreeSegments(totalSegments)
  const levels: Set<number>[] = Array.from(
    { length: depth },
    () => new Set<number>(),
  )
  const paths: DramaWalkedPath[] = []

  for (const session of sessions ?? []) {
    const turns = session?.turns ?? []
    const nodes: DramaTreeNodePosition[] = []
    let truncated = false
    let index = 0

    for (let d = 0; d < turns.length; d += 1) {
      // A session deeper than the tree it belongs to is corrupt data, not a
      // reason to invent a layer nobody can draw.
      if (d >= depth) {
        truncated = true
        break
      }
      if (d > 0) {
        const branch = toneBranchIndex(turns[d]?.chosen_tone)
        if (branch < 0) {
          truncated = true
          break
        }
        index = index * DRAMA_BRANCH_FACTOR + branch
      }
      nodes.push({ depth: d, index })
      levels[d].add(index)
    }

    // A session with nothing placeable contributes no line to the graph.
    if (nodes.length > 0) {
      paths.push({ sessionId: session?.id ?? '', nodes, truncated })
    }
  }

  return { depth, levels, paths }
}

/** D8.1's three numbers. `percent` is already rounded to one decimal. */
export interface DramaCompletion {
  /** Distinct nodes walked, across every playthrough. */
  walked: number
  /** Nodes in the whole tree; `0` when the segment count is unreadable. */
  total: number
  /** `walked / total` as a percentage, one decimal, capped at 100. */
  percent: number
}

/**
 * How much of the tree this player has seen.
 *
 * De-duplicated **by `node_id`**, not by position: replays share their early
 * layers, and counting the root once per save slot would let a player inflate
 * the number by pressing 開場 repeatedly. Node id is the server's own identity
 * for a node, so it de-dupes correctly even for a turn whose tone this client
 * could not place.
 *
 * `walked` is reported raw (it is a fact about the save data) while `percent`
 * is capped at 100, so a corrupt row claiming more nodes than the tree holds
 * prints a strange fraction rather than 4,300%.
 */
export function dramaCompletion(
  sessions: readonly DramaSession[] | null | undefined,
  totalSegments: number,
): DramaCompletion {
  const total = fullTreeNodeCount(totalSegments)
  const seen = new Set<string>()

  for (const session of sessions ?? []) {
    for (const turn of session?.turns ?? []) {
      const nodeId = typeof turn?.node_id === 'string' ? turn.node_id.trim() : ''
      if (nodeId) seen.add(nodeId)
    }
  }

  const walked = seen.size
  // `total === 0` is "unknown tree", never a division: 0/0 would print NaN%
  // and walked/0 would print Infinity%.
  const percent =
    total > 0 ? Math.round(Math.min(walked / total, 1) * 1000) / 10 : 0

  return { walked, total, percent }
}

/**
 * Both halves of the panel's progress read, computed once.
 *
 * One object rather than two props so the branching graph (BD14) can hang in
 * the same panel off the same input — the graph needs `tree`, the header line
 * needs `completion`, and they must never disagree about which drama they
 * describe.
 */
export interface DramaProgress {
  completion: DramaCompletion
  tree: DramaWalkedTree
}

export function buildDramaProgress(
  sessions: readonly DramaSession[] | null | undefined,
  totalSegments: number,
): DramaProgress {
  return {
    completion: dramaCompletion(sessions, totalSegments),
    tree: buildWalkedTree(sessions, totalSegments),
  }
}

/**
 * The completion line's display number (FX3).
 *
 * `DramaCompletion.percent` is rounded to one decimal for reading, which is
 * fine for the six-act default (4/364 reads as a legible `1.1`) and a lie for
 * a deep one: a 12-act tree is `(3^12-1)/2 = 265,720` nodes, so a full
 * costume-change loop — 12 distinct nodes, a real playthrough — is
 * `12/265720 = 0.0045%`, which rounds to exactly `0.0`. Printed next to
 * 「完成度」that reads as *nothing walked*, indistinguishable from a drama
 * nobody has opened, and it stays that way for another ten loops. This
 * returns the same rounded digits `percent.toFixed(1)` always did, except
 * when rounding erased a real, nonzero walk — then it returns `'<0.1'`, a
 * token every locale's `{percent}%` template already renders correctly
 * without a new i18n key (a bare `<` in an interpolated slot is text, not
 * markup — vue-i18n does not parse it as HTML).
 *
 * `walked === 0` is excluded on purpose: a genuinely untouched drama must
 * still say `0.0`, not `<0.1` — the two mean different things to a player
 * deciding whether to start.
 */
export function formatDramaCompletionPercent(
  completion: DramaCompletion,
): string {
  if (completion.walked > 0 && completion.percent === 0) return '<0.1'
  return completion.percent.toFixed(1)
}

/**
 * Floor under the fraction of its circumference `UiProgressRing` is allowed
 * to leave unfilled when at least one node has been walked (FX3, mirrors
 * `BAR_MIN_VISIBLE_FILL` in `dramaProgressTree.ts` — the same "a nonzero
 * fact must survive being drawn" rule, applied to a ring instead of a bar).
 *
 * Sized against the ring the gallery-entry button actually draws
 * (`size=30, thickness=3` → circumference ≈ 84.8 view units): below roughly
 * 0.024 the filled arc is under 2px and disappears into the track at typical
 * screen density. 0.03 clears that with room to spare without the ring
 * reading as meaningfully further along than a fresh 1/364 walk already is.
 */
export const DRAMA_RING_MIN_VISIBLE_RATIO = 0.03

/**
 * The progress ring's input (D8.4) — **not** `completion.percent / 100`.
 *
 * `percent` is rounded to one decimal for a human to read; feeding that
 * rounding into a ring means every playthrough of a deep tree draws the same
 * empty ring a drama nobody has opened does — see `formatDramaCompletionPercent`
 * for the arithmetic. This divides the raw counts instead, so the ring moves
 * on precision `percent` cannot carry, then floors a nonzero result so a
 * genuinely tiny walk still reads as a ring, not a rounding error.
 *
 * `0` stays exactly `0` — an untouched drama draws a genuinely empty ring;
 * the floor never fires for `walked === 0`.
 */
export function dramaCompletionRingRatio(completion: DramaCompletion): number {
  if (completion.total <= 0 || completion.walked <= 0) return 0
  const raw = Math.min(completion.walked / completion.total, 1)
  return Math.max(raw, DRAMA_RING_MIN_VISIBLE_RATIO)
}
