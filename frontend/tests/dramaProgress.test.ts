import { describe, expect, it } from 'vitest'
import {
  DRAMA_BRANCH_FACTOR,
  DRAMA_RING_MIN_VISIBLE_RATIO,
  DRAMA_TONE_BRANCH_ORDER,
  DRAMA_TREE_SEGMENT_CEILING,
  buildDramaProgress,
  buildWalkedTree,
  dramaCompletion,
  dramaCompletionRingRatio,
  formatDramaCompletionPercent,
  fullTreeNodeCount,
  resolveTreeSegments,
  toneBranchIndex,
} from '../src/utils/dramaProgress'
import {
  DRAMA_TONES,
  MAX_DRAMA_TOTAL_SEGMENTS,
  MIN_DRAMA_TOTAL_SEGMENTS,
} from '../src/types/branchingDrama'
import type { DramaSession, DramaSessionTurn } from '../src/types/branchingDrama'

function turn(overrides: Partial<DramaSessionTurn> = {}): DramaSessionTurn {
  return {
    node_id: 'n-root',
    narration: '',
    player_input: '',
    chosen_tone: null,
    exchanges: [],
    ...overrides,
  }
}

/**
 * A playthrough written the way the service writes one: turn 0 is the root
 * with no tone, and every later turn is one layer down carrying the tone of
 * the child it moved to.
 */
function session(
  id: string,
  tones: (string | null)[],
  nodeIds?: string[],
): DramaSession {
  const turns = [turn({ node_id: nodeIds?.[0] ?? 'n-root' })]
  tones.forEach((tone, i) => {
    turns.push(
      turn({ node_id: nodeIds?.[i + 1] ?? `${id}-n${i + 1}`, chosen_tone: tone }),
    )
  })
  return {
    id,
    drama_id: 'd-1',
    current_node_id: turns[turns.length - 1].node_id,
    status: 'playing',
    turns,
    created_at: '2026-08-19T00:00:00Z',
    updated_at: '2026-08-19T00:00:00Z',
  }
}

describe('DRAMA_TONE_BRANCH_ORDER', () => {
  it('lays the branch out in the same closed vocabulary the wire uses', () => {
    // Load-bearing geometry: a fourth tone appearing in `DRAMA_TONES` alone
    // would leave every graph placing it nowhere, silently.
    expect([...DRAMA_TONE_BRANCH_ORDER].sort()).toEqual([...DRAMA_TONES].sort())
    expect(DRAMA_TONE_BRANCH_ORDER).toHaveLength(DRAMA_BRANCH_FACTOR)
  })

  it('pins the sibling order the index arithmetic is built on', () => {
    expect(toneBranchIndex('dark')).toBe(0)
    expect(toneBranchIndex('sunny')).toBe(1)
    expect(toneBranchIndex('neutral')).toBe(2)
  })

  it('refuses to place a tone it does not know', () => {
    expect(toneBranchIndex(null)).toBe(-1)
    expect(toneBranchIndex(undefined)).toBe(-1)
    expect(toneBranchIndex('')).toBe(-1)
    expect(toneBranchIndex('DARK')).toBe(-1)
    expect(toneBranchIndex('bittersweet')).toBe(-1)
  })
})

describe('resolveTreeSegments', () => {
  it('passes a normal drama through untouched', () => {
    expect(resolveTreeSegments(6)).toBe(6)
    expect(resolveTreeSegments(MAX_DRAMA_TOTAL_SEGMENTS)).toBe(
      MAX_DRAMA_TOTAL_SEGMENTS,
    )
  })

  it('raises a below-floor count to the shallowest tree that can exist', () => {
    // The backend enforces the floor on reads too, so 0/1 here is corrupt
    // data rather than an older drama.
    expect(resolveTreeSegments(0)).toBe(MIN_DRAMA_TOTAL_SEGMENTS)
    expect(resolveTreeSegments(1)).toBe(MIN_DRAMA_TOTAL_SEGMENTS)
    expect(resolveTreeSegments(-9)).toBe(MIN_DRAMA_TOTAL_SEGMENTS)
  })

  it('answers "unknown" rather than throwing on a non-number', () => {
    expect(resolveTreeSegments(Number.NaN)).toBe(0)
    expect(resolveTreeSegments(Number.POSITIVE_INFINITY)).toBe(0)
    expect(resolveTreeSegments(undefined as unknown as number)).toBe(0)
  })

  it('clamps a drama deeper than the arithmetic ceiling', () => {
    // Not clamped to the *create* cap: a drama written under an older,
    // higher cap must still be measured against its own tree.
    expect(resolveTreeSegments(MAX_DRAMA_TOTAL_SEGMENTS + 3)).toBe(
      MAX_DRAMA_TOTAL_SEGMENTS + 3,
    )
    expect(resolveTreeSegments(DRAMA_TREE_SEGMENT_CEILING + 40)).toBe(
      DRAMA_TREE_SEGMENT_CEILING,
    )
  })

  it('takes the whole part of a fractional count', () => {
    expect(resolveTreeSegments(6.9)).toBe(6)
  })
})

describe('fullTreeNodeCount', () => {
  it('matches the entity closed form (3^N - 1) / 2', () => {
    expect(fullTreeNodeCount(2)).toBe(4)
    expect(fullTreeNodeCount(3)).toBe(13)
    // The product default: 364 nodes, not the handful that have pictures.
    expect(fullTreeNodeCount(6)).toBe(364)
    expect(fullTreeNodeCount(MAX_DRAMA_TOTAL_SEGMENTS)).toBe(265720)
  })

  it('stays an exact integer at the ceiling', () => {
    const total = fullTreeNodeCount(DRAMA_TREE_SEGMENT_CEILING)

    expect(Number.isSafeInteger(total)).toBe(true)
    expect(total).toBe(1743392200)
  })

  it('reads an unknown segment count as no denominator at all', () => {
    expect(fullTreeNodeCount(Number.NaN)).toBe(0)
    expect(fullTreeNodeCount(undefined as unknown as number)).toBe(0)
  })

  it('never throws on the shapes stored rows can actually hold', () => {
    for (const value of [0, 1, -5, 2.5, 1e9, Number.NEGATIVE_INFINITY]) {
      const total = fullTreeNodeCount(value)
      expect(Number.isSafeInteger(total)).toBe(true)
      expect(total).toBeGreaterThanOrEqual(0)
    }
  })
})

describe('dramaCompletion', () => {
  it('reads an untouched drama as nothing walked', () => {
    expect(dramaCompletion([], 6)).toEqual({ walked: 0, total: 364, percent: 0 })
  })

  it('reads a freshly opened session as one node — the root', () => {
    // 開場 writes exactly one turn; 1/364 rounds to 0.3%.
    expect(dramaCompletion([session('s1', [])], 6)).toEqual({
      walked: 1,
      total: 364,
      percent: 0.3,
    })
  })

  it('counts a node once however many playthroughs pass through it', () => {
    // Two save slots that both opened and both went dark: root and the dark
    // child are the same two nodes, not four.
    const a = session('s1', ['dark'], ['n-root', 'n-dark'])
    const b = session('s2', ['dark'], ['n-root', 'n-dark'])

    expect(dramaCompletion([a, b], 6).walked).toBe(2)
  })

  it('adds the nodes a second run reached that the first did not', () => {
    const a = session('s1', ['dark'], ['n-root', 'n-dark'])
    const b = session('s2', ['sunny'], ['n-root', 'n-sunny'])

    expect(dramaCompletion([a, b], 6).walked).toBe(3)
  })

  it('keeps the percentage to one decimal', () => {
    // 4 of 13 is 30.769…%
    const walk = session('s1', ['dark', 'sunny', 'neutral'])

    expect(dramaCompletion([walk], 3).percent).toBe(30.8)
  })

  it('prints no NaN and no Infinity when the tree is unreadable', () => {
    const result = dramaCompletion([session('s1', ['dark'])], Number.NaN)

    expect(result).toEqual({ walked: 2, total: 0, percent: 0 })
    expect(Number.isFinite(result.percent)).toBe(true)
  })

  it('caps a corrupt over-count at 100% instead of printing 400%', () => {
    // A 2-segment tree holds 4 nodes; six distinct ids is data that
    // disagrees with itself.
    const walk = session('s1', ['dark', 'sunny', 'neutral', 'dark', 'sunny'])

    const result = dramaCompletion([walk], 2)

    expect(result.walked).toBe(6)
    expect(result.total).toBe(4)
    expect(result.percent).toBe(100)
  })

  it('ignores a turn with no usable node id', () => {
    const walk = session('s1', ['dark'])
    walk.turns[1] = { ...walk.turns[1], node_id: '   ' }

    expect(dramaCompletion([walk], 6).walked).toBe(1)
  })

  it('still counts a node whose tone this client cannot place', () => {
    // De-dupe is by node id, so an unreadable tone costs the graph a line
    // but never the completion number.
    const walk = session('s1', ['bittersweet'])

    expect(dramaCompletion([walk], 6).walked).toBe(2)
  })

  it('survives a missing session list', () => {
    expect(dramaCompletion(null, 6).walked).toBe(0)
    expect(dramaCompletion(undefined, 6).walked).toBe(0)
  })
})

describe('buildWalkedTree', () => {
  it('lays the root at depth 0, index 0 whatever its stored tone says', () => {
    const walk = session('s1', [])
    walk.turns[0] = { ...walk.turns[0], chosen_tone: 'sunny' }

    const tree = buildWalkedTree([walk], 6)

    expect(tree.paths[0].nodes).toEqual([{ depth: 0, index: 0 }])
    expect(tree.paths[0].truncated).toBe(false)
  })

  it('numbers a child as parentIndex * 3 + toneIndex', () => {
    const tree = buildWalkedTree([session('s1', ['neutral', 'dark'])], 6)

    expect(tree.paths[0].nodes).toEqual([
      { depth: 0, index: 0 },
      { depth: 1, index: 2 },
      { depth: 2, index: 6 },
    ])
  })

  it('agrees with the base-3 sum over the tone sequence', () => {
    // index at depth d = Σ toneIndex(tone_k) * 3^(d-k)
    const tones = ['sunny', 'neutral', 'dark']
    const tree = buildWalkedTree([session('s1', tones)], 6)

    const expected = tones.reduce(
      (acc, tone) => acc * DRAMA_BRANCH_FACTOR + toneBranchIndex(tone),
      0,
    )
    expect(tree.paths[0].nodes[3]).toEqual({ depth: 3, index: expected })
    expect(expected).toBe(1 * 9 + 2 * 3 + 0)
  })

  it('gives every layer a set of the indices actually stood on', () => {
    const a = session('s1', ['dark', 'dark'])
    const b = session('s2', ['sunny'])

    const tree = buildWalkedTree([a, b], 4)

    expect(tree.depth).toBe(4)
    expect(tree.levels.map((level) => [...level].sort((x, y) => x - y))).toEqual([
      [0],
      [0, 1],
      [0],
      [],
    ])
  })

  it('cuts a path at a tone it cannot place and says so', () => {
    // Everything below an unknown tone would be a guessed index, and a
    // guessed index is a line drawn to the wrong branch.
    const tree = buildWalkedTree([session('s1', ['dark', 'bittersweet', 'sunny'])], 6)

    expect(tree.paths[0].nodes).toEqual([
      { depth: 0, index: 0 },
      { depth: 1, index: 0 },
    ])
    expect(tree.paths[0].truncated).toBe(true)
    expect(tree.levels[2].size).toBe(0)
  })

  it('cuts a session that runs deeper than its own tree', () => {
    const tree = buildWalkedTree([session('s1', ['dark', 'sunny', 'neutral'])], 2)

    expect(tree.depth).toBe(2)
    expect(tree.paths[0].nodes).toHaveLength(2)
    expect(tree.paths[0].truncated).toBe(true)
  })

  it('drops a session with nothing placeable rather than drawing an empty line', () => {
    const empty: DramaSession = { ...session('s1', []), turns: [] }

    expect(buildWalkedTree([empty], 6).paths).toEqual([])
  })

  it('names the session each path belongs to', () => {
    const tree = buildWalkedTree([session('slot-a', ['dark'])], 6)

    expect(tree.paths[0].sessionId).toBe('slot-a')
  })

  it('carries coordinates only — never a node id or a title', () => {
    // The anti-spoiler boundary: the graph is shape, not content.
    const tree = buildWalkedTree([session('s1', ['dark'])], 6)

    for (const node of tree.paths[0].nodes) {
      expect(Object.keys(node).sort()).toEqual(['depth', 'index'])
    }
  })

  it('draws nothing when the segment count is unreadable', () => {
    const tree = buildWalkedTree([session('s1', ['dark'])], Number.NaN)

    expect(tree).toEqual({ depth: 0, levels: [], paths: [] })
  })

  it('survives a missing session list', () => {
    expect(buildWalkedTree(null, 6).paths).toEqual([])
  })
})

describe('buildDramaProgress', () => {
  it('computes both halves off one reading of the same drama', () => {
    const sessions = [session('s1', ['dark'])]

    const progress = buildDramaProgress(sessions, 6)

    expect(progress.completion).toEqual(dramaCompletion(sessions, 6))
    expect(progress.tree.depth).toBe(6)
    expect(progress.tree.paths).toHaveLength(1)
  })
})

describe('formatDramaCompletionPercent — FX3, the deep-tree 0.0% regression', () => {
  it('prints the untouched drama as a real 0.0%, not the negligible token', () => {
    // walked === 0 is honestly nothing, not "some but unreadable".
    expect(formatDramaCompletionPercent(dramaCompletion([], 12))).toBe('0.0')
  })

  it('says <0.1 rather than 0.0 for a walk too small for one decimal to show', () => {
    // A full costume-change loop of a 12-act tree: 12 of 265,720 nodes,
    // walked, and 0.0045% — `percent` still rounds to exactly 0.
    const walk = session('s1', Array(11).fill('dark'))
    const completion = dramaCompletion([walk], MAX_DRAMA_TOTAL_SEGMENTS)

    expect(completion).toEqual({ walked: 12, total: 265720, percent: 0 })
    expect(formatDramaCompletionPercent(completion)).toBe('<0.1')
  })

  it('leaves the six-act default exactly as it printed before FX3', () => {
    // 4/364 = 1.0989…% — nonzero once rounded, so no floor kicks in.
    const walk = session('s1', ['dark', 'sunny', 'neutral'])
    const completion = dramaCompletion([walk], 6)

    expect(completion.percent).toBe(1.1)
    expect(formatDramaCompletionPercent(completion)).toBe('1.1')
  })

  it('still prints a genuine 100 for a corrupt over-count', () => {
    const walk = session('s1', ['dark', 'sunny', 'neutral', 'dark', 'sunny'])
    const completion = dramaCompletion([walk], 2)

    expect(formatDramaCompletionPercent(completion)).toBe('100.0')
  })

  it('shows the boundary just above the floor as a normal decimal', () => {
    // Any percent that rounds to a nonzero tenth prints as that tenth, not
    // the negligible token — the token is only for what rounding erased.
    const walk = session('s1', ['dark', 'sunny'])
    const completion = dramaCompletion([walk], 6) // 3/364 = 0.8% (rounds to 0.8, not 0)

    expect(completion.percent).toBeGreaterThan(0)
    expect(formatDramaCompletionPercent(completion)).toBe(
      completion.percent.toFixed(1),
    )
  })
})

describe('dramaCompletionRingRatio — FX3, the ring must not read the rounded percent', () => {
  it('draws a genuinely empty ring for an untouched drama', () => {
    expect(dramaCompletionRingRatio(dramaCompletion([], 12))).toBe(0)
  })

  it('floors a walked-but-tiny deep tree to a visible arc instead of 0', () => {
    const walk = session('s1', Array(11).fill('dark'))
    const completion = dramaCompletion([walk], MAX_DRAMA_TOTAL_SEGMENTS)

    // The raw ratio is far below the floor — this is exactly what percent
    // rounds away, and exactly why the ring cannot read `percent`.
    expect(completion.walked / completion.total).toBeLessThan(
      DRAMA_RING_MIN_VISIBLE_RATIO,
    )
    expect(dramaCompletionRingRatio(completion)).toBe(
      DRAMA_RING_MIN_VISIBLE_RATIO,
    )
  })

  it('never lowers a ratio already above the floor', () => {
    // A drama walked past the floor reads its own true fraction, not a
    // clamped-down one.
    const walk = session('s1', Array(11).fill('dark'))
    const completion = dramaCompletion([walk], 3) // 12 of 13 nodes

    const raw = completion.walked / completion.total
    expect(raw).toBeGreaterThan(DRAMA_RING_MIN_VISIBLE_RATIO)
    expect(dramaCompletionRingRatio(completion)).toBeCloseTo(raw, 12)
  })

  it('caps a corrupt over-count at a full ring, never past it', () => {
    const walk = session('s1', ['dark', 'sunny', 'neutral', 'dark', 'sunny'])
    const completion = dramaCompletion([walk], 2)

    expect(dramaCompletionRingRatio(completion)).toBe(1)
  })

  it('reads 0 for an unreadable tree rather than dividing by zero', () => {
    const completion = dramaCompletion([session('s1', ['dark'])], Number.NaN)
    expect(dramaCompletionRingRatio(completion)).toBe(0)
  })
})
