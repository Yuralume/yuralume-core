import { describe, expect, it } from 'vitest'
import {
  DRAMA_TREE_DOT_BUDGET,
  DRAMA_TREE_EDGE_MAX_LAYER_NODES,
  DRAMA_TREE_FALLBACK_WIDTH_PX,
  DRAMA_TREE_MIN_DOT_PX,
  DRAMA_TREE_MIN_LAYOUT_WIDTH_PX,
  DRAMA_TREE_MIN_STROKE_PX,
  DRAMA_TREE_VIEW_WIDTH,
  buildDramaTreeLayout,
  maxDotRowNodes,
  normalizeTreeWidthPx,
  planTreeLayerModes,
  treeLayerNodeCount,
  walkedPathFor,
} from '../src/utils/dramaProgressTree'
import {
  buildWalkedTree,
  fullTreeNodeCount,
  type DramaWalkedTree,
} from '../src/utils/dramaProgress'
import { MAX_DRAMA_TOTAL_SEGMENTS } from '../src/types/branchingDrama'
import treeSource from '../src/components/branching-drama/DramaProgressTree.vue?raw'
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

/** A playthrough shaped the way the service writes one (see BD13's tests). */
function session(id: string, tones: string[]): DramaSession {
  const turns = [turn({ node_id: `${id}-n0` })]
  tones.forEach((tone, i) => {
    turns.push(turn({ node_id: `${id}-n${i + 1}`, chosen_tone: tone }))
  })
  return {
    id,
    drama_id: 'd1',
    current_node_id: turns[turns.length - 1].node_id,
    status: 'ended',
    turns,
    created_at: '2026-08-19T00:00:00Z',
    updated_at: '2026-08-19T00:00:00Z',
  }
}

function treeOf(sessions: DramaSession[], segments: number): DramaWalkedTree {
  return buildWalkedTree(sessions, segments)
}

/**
 * Rendered widths the two surfaces actually hand the graph, in CSS pixels.
 *
 * Derived from the stylesheets, not measured at runtime — this suite has no
 * DOM — so the arithmetic is spelled out and has to be re-done if the boxes
 * around the figure change:
 *
 *   ending overlay  `.drama-exit`  = min(440, 100vw - 32) wide,
 *                                    padding 24 (18/14 under 768px)
 *   figure          `.drama-tree`  = 1px border + 10px padding per side
 *
 * so the SVG gets `dramaExit - 2·padX - 22`.
 */
const EXIT_HUB_WIDE = 370 // >= 472px viewport: 440 - 48 - 22
const EXIT_HUB_375 = 293 // 375 - 32 = 343, - 28 - 22
const EXIT_HUB_360 = 278 // 360 - 32 = 328, - 28 - 22
const EXIT_HUB_320 = 238 // 320 - 32 = 288, - 28 - 22
/** A roomy gallery panel — wide enough for the full six-act triangle. */
const GALLERY_WIDE = 900

describe('planTreeLayerModes — the render budget', () => {
  it('draws the default six-act tree entirely as dots', () => {
    const plans = planTreeLayerModes(6)
    expect(plans.map((p) => p.mode)).toEqual(Array(6).fill('dots'))
    expect(plans.map((p) => p.nodeCount)).toEqual([1, 3, 9, 27, 81, 243])
    const drawn = plans.reduce((n, p) => n + p.nodeCount, 0)
    expect(drawn).toBe(fullTreeNodeCount(6))
    expect(drawn).toBe(364)
  })

  it('degrades layer by layer, not tree by tree, at the create ceiling', () => {
    const plans = planTreeLayerModes(MAX_DRAMA_TOTAL_SEGMENTS)
    expect(plans).toHaveLength(12)
    // The shallow layers stay real dots even though the tree as a whole is
    // 265,720 nodes — the whole point of a per-layer decision.
    expect(plans.slice(0, 6).map((p) => p.mode)).toEqual(
      Array(6).fill('dots'),
    )
    expect(plans.slice(6).map((p) => p.mode)).toEqual(Array(6).fill('bar'))
  })

  it('never plans more dots than the budget, at any depth up to the arithmetic ceiling', () => {
    for (let depth = 1; depth <= 20; depth += 1) {
      const dots = planTreeLayerModes(depth)
        .filter((p) => p.mode === 'dots')
        .reduce((n, p) => n + p.nodeCount, 0)
      expect(dots).toBeLessThanOrEqual(DRAMA_TREE_DOT_BUDGET)
    }
  })

  it('honours a caller-supplied budget', () => {
    expect(planTreeLayerModes(6, { budget: 13 }).map((p) => p.mode)).toEqual([
      'dots',
      'dots',
      'dots',
      'bar',
      'bar',
      'bar',
    ])
  })

  it('has nothing to plan for an unreadable depth', () => {
    expect(planTreeLayerModes(0)).toEqual([])
    expect(planTreeLayerModes(-3)).toEqual([])
    expect(planTreeLayerModes(Number.NaN)).toEqual([])
  })

  it('agrees with the closed form for one layer', () => {
    expect(treeLayerNodeCount(0)).toBe(1)
    expect(treeLayerNodeCount(5)).toBe(243)
    expect(treeLayerNodeCount(11)).toBe(177147)
  })
})

describe('buildDramaTreeLayout — a 12-act drama does not draw 265,720 dots', () => {
  const tree = treeOf(
    [session('s1', ['dark', 'sunny', 'neutral', 'dark', 'sunny', 'dark',
      'neutral', 'dark', 'sunny', 'neutral', 'dark'])],
    MAX_DRAMA_TOTAL_SEGMENTS,
  )

  it('emits 364 circles for a tree of 265,720 nodes', () => {
    const layout = buildDramaTreeLayout(tree, { containerWidthPx: GALLERY_WIDE })
    expect(fullTreeNodeCount(MAX_DRAMA_TOTAL_SEGMENTS)).toBe(265720)
    expect(layout.dotCount).toBe(364)
    expect(layout.dotCount).toBeLessThanOrEqual(DRAMA_TREE_DOT_BUDGET)
  })

  it('turns exactly the over-budget layers into bars, and empties their dots', () => {
    const layout = buildDramaTreeLayout(tree, { containerWidthPx: GALLERY_WIDE })
    expect(layout.barLayerCount).toBe(6)
    for (const layer of layout.layers) {
      if (layer.mode !== 'bar') continue
      expect(layer.dots).toHaveLength(0)
      expect(layer.barHeight).toBeGreaterThan(0)
    }
    expect(layout.layers[11].nodeCount).toBe(177147)
    expect(layout.layers[11].dots).toHaveLength(0)
  })

  it('keeps a walked deep layer visible even when its coverage rounds to nothing', () => {
    const layout = buildDramaTreeLayout(tree, { containerWidthPx: GALLERY_WIDE })
    const deepest = layout.layers[11]
    expect(deepest.walkedCount).toBe(1)
    expect(deepest.coverage).toBeCloseTo(1 / 177147, 12)
    // 1/177147 of 300 units is 0.0017 — it would paint nothing at all.
    expect(deepest.barFillWidth).toBeGreaterThan(0.5)
  })

  it('marks where this run crossed a degraded layer', () => {
    const layout = buildDramaTreeLayout(tree, {
      currentSessionId: 's1',
      containerWidthPx: GALLERY_WIDE,
    })
    const deepest = layout.layers[11]
    expect(deepest.currentX).not.toBeNull()
    expect(deepest.currentX!).toBeGreaterThanOrEqual(0)
    expect(deepest.currentX!).toBeLessThanOrEqual(DRAMA_TREE_VIEW_WIDTH)
  })

  it('leaves the bar unmarked when no run is nominated', () => {
    const layout = buildDramaTreeLayout(tree, { containerWidthPx: GALLERY_WIDE })
    expect(layout.layers[11].currentX).toBeNull()
  })
})

describe('buildDramaTreeLayout — the triangle', () => {
  const tree = treeOf([session('s1', ['dark', 'sunny'])], 4)

  it('centres the root and spreads the widest drawn layer edge to edge', () => {
    const layout = buildDramaTreeLayout(tree)
    expect(layout.layers[0].dots).toHaveLength(1)
    expect(layout.layers[0].dots[0].x).toBeCloseTo(DRAMA_TREE_VIEW_WIDTH / 2, 6)

    const widest = layout.layers[3]
    expect(widest.dots).toHaveLength(27)
    const slot = DRAMA_TREE_VIEW_WIDTH / 27
    expect(widest.dots[0].x).toBeCloseTo(slot / 2, 6)
    expect(widest.dots[26].x).toBeCloseTo(
      DRAMA_TREE_VIEW_WIDTH - slot / 2,
      6,
    )
  })

  it('makes each layer as wide as it is populous', () => {
    const layout = buildDramaTreeLayout(tree)
    const span = (depth: number): number => {
      const dots = layout.layers[depth].dots
      return dots[dots.length - 1].x - dots[0].x
    }
    // Layer 2 holds 9 nodes and layer 3 holds 27; the gap between first and
    // last centre is (n-1) slots, so the ratio is (9-1)/(27-1).
    expect(span(2) / span(3)).toBeCloseTo(8 / 26, 6)
  })

  it('rows go down the page in depth order and the box encloses them', () => {
    const layout = buildDramaTreeLayout(tree)
    const ys = layout.layers.map((l) => l.y)
    expect([...ys].sort((a, b) => a - b)).toEqual(ys)
    expect(ys[0]).toBeGreaterThan(0)
    expect(ys[ys.length - 1]).toBeLessThan(layout.height)
  })

  it('draws nothing at all for an unreadable tree', () => {
    const empty = buildDramaTreeLayout(buildWalkedTree([], Number.NaN))
    expect(empty.layers).toEqual([])
    expect(empty.edges).toEqual([])
    expect(empty.dotCount).toBe(0)
    expect(empty.height).toBe(0)
    expect(buildDramaTreeLayout(null).layers).toEqual([])
  })
})

describe('buildDramaTreeLayout — walked, and this run', () => {
  // 'dark' = branch 0, 'sunny' = 1, 'neutral' = 2 (DRAMA_TONE_BRANCH_ORDER).
  const tree = treeOf(
    [session('old', ['sunny', 'sunny']), session('now', ['dark', 'neutral'])],
    3,
  )

  it('lights exactly the indices the sessions stood on', () => {
    const layout = buildDramaTreeLayout(tree, { currentSessionId: 'now' })
    const lit = (depth: number): number[] =>
      layout.layers[depth].dots.filter((d) => d.walked).map((d) => d.index)
    expect(lit(0)).toEqual([0])
    expect(lit(1)).toEqual([0, 1])
    // sunny→sunny = 1*3+1 = 4; dark→neutral = 0*3+2 = 2.
    expect(lit(2)).toEqual([2, 4])
  })

  it('flags only the nominated run as current, and makes it the biggest dot', () => {
    const layout = buildDramaTreeLayout(tree, { currentSessionId: 'now' })
    const current = layout.layers.flatMap((l) =>
      l.dots.filter((d) => d.current).map((d) => ({ depth: l.depth, i: d.index })),
    )
    expect(current).toEqual([
      { depth: 0, i: 0 },
      { depth: 1, i: 0 },
      { depth: 2, i: 2 },
    ])
    const layer2 = layout.layers[2]
    const currentDot = layer2.dots[2]
    const historyDot = layer2.dots[4]
    const dimDot = layer2.dots[0]
    expect(currentDot.r).toBeGreaterThan(historyDot.r)
    expect(historyDot.r).toBeGreaterThan(dimDot.r)
  })

  it('has no current path when the nominated session is not in the tree', () => {
    const layout = buildDramaTreeLayout(tree, { currentSessionId: 'ghost' })
    expect(layout.layers.flatMap((l) => l.dots).some((d) => d.current)).toBe(
      false,
    )
    expect(layout.edges.some((e) => e.current)).toBe(false)
  })
})

describe('buildDramaTreeLayout — edges', () => {
  it('draws the whole skeleton while the layers are sparse', () => {
    const layout = buildDramaTreeLayout(treeOf([session('s', ['dark'])], 3))
    // Layers 1 and 2 hold 3 and 9 nodes; every one of the 12 gets a line.
    expect(layout.edges).toHaveLength(12)
    expect(layout.edges.filter((e) => e.walked)).toHaveLength(1)
  })

  it('drops the un-walked skeleton once a layer passes the density gate, but never the walked lines', () => {
    const tones = ['dark', 'sunny', 'neutral', 'dark', 'sunny']
    const layout = buildDramaTreeLayout(treeOf([session('s', tones)], 6), {
      currentSessionId: 's',
      containerWidthPx: GALLERY_WIDE,
    })
    expect(treeLayerNodeCount(5)).toBeGreaterThan(
      DRAMA_TREE_EDGE_MAX_LAYER_NODES,
    )
    const intoLayer5 = layout.edges.filter((e) => e.y2 === layout.layers[5].y)
    expect(intoLayer5).toHaveLength(1)
    expect(intoLayer5[0].current).toBe(true)

    const intoLayer4 = layout.edges.filter((e) => e.y2 === layout.layers[4].y)
    expect(treeLayerNodeCount(4)).toBe(DRAMA_TREE_EDGE_MAX_LAYER_NODES)
    expect(intoLayer4).toHaveLength(81)
  })

  it('emits a walked edge once — as current, never as both', () => {
    const layout = buildDramaTreeLayout(
      treeOf([session('s', ['dark', 'sunny'])], 3),
      { currentSessionId: 's' },
    )
    const drawn = layout.edges.filter((e) => e.walked)
    expect(drawn).toHaveLength(2)
    expect(drawn.every((e) => e.current)).toBe(true)
    expect(new Set(layout.edges.map((e) => e.key)).size).toBe(
      layout.edges.length,
    )
  })

  it('paints dim skeleton first and this run last', () => {
    const layout = buildDramaTreeLayout(
      treeOf([session('old', ['sunny']), session('now', ['dark'])], 2),
      { currentSessionId: 'now' },
    )
    const rank = layout.edges.map((e) => (e.current ? 2 : e.walked ? 1 : 0))
    expect([...rank].sort((a, b) => a - b)).toEqual(rank)
  })

  it('never draws an edge into or out of a degraded layer', () => {
    const layout = buildDramaTreeLayout(
      treeOf(
        [session('s', ['dark', 'sunny', 'neutral', 'dark', 'sunny', 'dark'])],
        7,
      ),
    )
    const barYs = new Set(
      layout.layers.filter((l) => l.mode === 'bar').map((l) => l.y),
    )
    expect(barYs.size).toBeGreaterThan(0)
    for (const edge of layout.edges) {
      expect(barYs.has(edge.y1)).toBe(false)
      expect(barYs.has(edge.y2)).toBe(false)
    }
  })
})

describe('the width the picture is actually rendered at', () => {
  const sixAct = treeOf(
    [session('s1', ['dark', 'sunny', 'neutral', 'dark', 'sunny'])],
    6,
  )

  it('clamps a missing, zero or absurd measurement into something drawable', () => {
    expect(normalizeTreeWidthPx(undefined)).toBe(DRAMA_TREE_FALLBACK_WIDTH_PX)
    expect(normalizeTreeWidthPx(null)).toBe(DRAMA_TREE_FALLBACK_WIDTH_PX)
    expect(normalizeTreeWidthPx(0)).toBe(DRAMA_TREE_FALLBACK_WIDTH_PX)
    expect(normalizeTreeWidthPx(-40)).toBe(DRAMA_TREE_FALLBACK_WIDTH_PX)
    expect(normalizeTreeWidthPx(Number.NaN)).toBe(DRAMA_TREE_FALLBACK_WIDTH_PX)
    // A collapsed parent measures small but real; laying out for 12px would
    // ask for dots wider than the row they sit on.
    expect(normalizeTreeWidthPx(12)).toBe(DRAMA_TREE_MIN_LAYOUT_WIDTH_PX)
    expect(normalizeTreeWidthPx(500)).toBe(500)
  })

  it('reads a row cap straight off the minimum legible dot', () => {
    // n dots across w pixels are 2*0.42*w/n across; the cap is where that
    // equals DRAMA_TREE_MIN_DOT_PX.
    expect(maxDotRowNodes(EXIT_HUB_WIDE)).toBe(103)
    expect(maxDotRowNodes(EXIT_HUB_375)).toBe(82)
    expect(maxDotRowNodes(EXIT_HUB_360)).toBe(77)
    expect(maxDotRowNodes(EXIT_HUB_320)).toBe(66)
    expect(maxDotRowNodes(GALLERY_WIDE)).toBe(252)
  })

  it.each([
    ['ending overlay, 440px cap', EXIT_HUB_WIDE, 5],
    ['375px phone', EXIT_HUB_375, 5],
    ['360px phone', EXIT_HUB_360, 4],
    ['320px phone', EXIT_HUB_320, 4],
    ['roomy gallery panel', GALLERY_WIDE, 6],
  ])(
    'draws as many rows of dots as %s can show, and bars for the rest',
    (_label, containerWidthPx, dotLayers) => {
      const layout = buildDramaTreeLayout(sixAct, {
        containerWidthPx: containerWidthPx as number,
      })
      const modes = layout.layers.map((l) => l.mode)
      expect(modes).toEqual([
        ...Array(dotLayers as number).fill('dots'),
        ...Array(6 - (dotLayers as number)).fill('bar'),
      ])
      expect(layout.barLayerCount).toBe(6 - (dotLayers as number))
    },
  )

  it.each([
    EXIT_HUB_WIDE,
    EXIT_HUB_375,
    EXIT_HUB_360,
    EXIT_HUB_320,
    GALLERY_WIDE,
    DRAMA_TREE_MIN_LAYOUT_WIDTH_PX,
  ])(
    'never draws a dot below the visible floor at %ipx — the regression FX2 was',
    (containerWidthPx) => {
      for (const tree of [sixAct, treeOf([session('s', ['dark'])], 2)]) {
        const layout = buildDramaTreeLayout(tree, { containerWidthPx })
        const dots = layout.layers.flatMap((l) => l.dots)
        expect(dots.length).toBeGreaterThan(0)
        for (const dot of dots) {
          // Rendered pixels, not view units: the whole bug was that these
          // two are not the same number.
          expect(dot.r * 2 * layout.unitPx).toBeGreaterThanOrEqual(
            DRAMA_TREE_MIN_DOT_PX - 1e-9,
          )
        }
      }
    },
  )

  it('was drawing 1.3px dots before the width entered the decision', () => {
    // The old layout kept all six layers as dots at any width: 243 slots
    // across 370px is a 1.27px dot at 16% alpha — the grey fog. Pinned as a
    // negative so a budget-only regression fails here.
    const budgetOnly = planTreeLayerModes(6)
    expect(budgetOnly.every((p) => p.mode === 'dots')).toBe(true)
    expect((2 * 0.42 * EXIT_HUB_WIDE) / 243).toBeLessThan(1.4)

    const layout = buildDramaTreeLayout(sixAct, {
      containerWidthPx: EXIT_HUB_WIDE,
    })
    expect(layout.layers[5].mode).toBe('bar')
  })

  it('says which of the two ceilings degraded a layer', () => {
    const deep = treeOf([session('s', ['dark'])], 8)
    const narrow = buildDramaTreeLayout(deep, {
      containerWidthPx: EXIT_HUB_WIDE,
    })
    expect(narrow.layers.map((l) => l.mode)).toEqual([
      'dots',
      'dots',
      'dots',
      'dots',
      'dots',
      'bar',
      'bar',
      'bar',
    ])
    expect(
      planTreeLayerModes(8, { maxRowNodes: maxDotRowNodes(EXIT_HUB_WIDE) })[5]
        .degradedBy,
    ).toBe('width')

    // Wide enough that 729 dots would be legible — now the DOM budget is
    // the one that says no, and it says so for its own reason.
    const plans = planTreeLayerModes(8, { maxRowNodes: maxDotRowNodes(2700) })
    expect(plans[5].degradedBy).toBeNull()
    expect(plans[6].degradedBy).toBe('budget')
  })

  it('assumes a narrow container before it has been measured', () => {
    const unmeasured = buildDramaTreeLayout(sixAct)
    expect(unmeasured.containerWidthPx).toBe(DRAMA_TREE_FALLBACK_WIDTH_PX)
    // Narrow-first: the un-measured frame is a shorter triangle, never fog.
    expect(unmeasured.layers[5].mode).toBe('bar')
  })

  it('keeps the drawing inside its own box at every width', () => {
    for (const containerWidthPx of [
      EXIT_HUB_WIDE,
      EXIT_HUB_375,
      EXIT_HUB_360,
      EXIT_HUB_320,
      GALLERY_WIDE,
    ]) {
      const layout = buildDramaTreeLayout(sixAct, {
        containerWidthPx,
        currentSessionId: 's1',
      })
      // The viewBox never widens with the container — the SVG is width:100%
      // and scales, so nothing here can push the page sideways.
      expect(layout.width).toBe(DRAMA_TREE_VIEW_WIDTH)
      for (const layer of layout.layers) {
        for (const dot of layer.dots) {
          expect(dot.x).toBeGreaterThanOrEqual(0)
          expect(dot.x).toBeLessThanOrEqual(DRAMA_TREE_VIEW_WIDTH)
        }
        expect(layer.barFillWidth).toBeLessThanOrEqual(DRAMA_TREE_VIEW_WIDTH)
        if (layer.currentX !== null) {
          expect(layer.currentX).toBeLessThanOrEqual(DRAMA_TREE_VIEW_WIDTH)
        }
      }
    }
  })
})

describe('lines survive the container too', () => {
  const sixAct = treeOf(
    [session('s1', ['dark', 'sunny', 'neutral', 'dark', 'sunny'])],
    6,
  )

  it.each([
    EXIT_HUB_WIDE,
    EXIT_HUB_375,
    EXIT_HUB_360,
    EXIT_HUB_320,
    GALLERY_WIDE,
  ])('keeps the faintest line at least a pixel wide at %ipx', (width) => {
    const layout = buildDramaTreeLayout(sixAct, { containerWidthPx: width })
    const { skeleton, walked, current } = layout.strokeWidths
    expect(skeleton * layout.unitPx).toBeGreaterThanOrEqual(
      DRAMA_TREE_MIN_STROKE_PX - 1e-9,
    )
    // The 0.35 / 0.7 / 1 hierarchy is scaled, not clamped away.
    expect(walked / skeleton).toBeCloseTo(2, 9)
    expect(current / walked).toBeCloseTo(1 / 0.7, 9)
  })

  it('leaves a roomy panel exactly as the design drew it', () => {
    const layout = buildDramaTreeLayout(sixAct, { containerWidthPx: 1200 })
    expect(layout.strokeWidths.skeleton).toBeCloseTo(0.35, 9)
    expect(layout.strokeWidths.current).toBeCloseTo(1, 9)
  })

  it('drops the skeleton when the gaps stop clearing the lines', () => {
    // 81 nodes: 4.57px apart inside the ending overlay — still a grid — and
    // 3.62px apart on a 375px phone, where 1px lines close it into a wash.
    const roomy = buildDramaTreeLayout(sixAct, {
      containerWidthPx: EXIT_HUB_WIDE,
      currentSessionId: 's1',
    })
    const intoLayer4 = (l: typeof roomy): number =>
      l.edges.filter((e) => e.y2 === l.layers[4].y).length
    expect(roomy.layers[4].mode).toBe('dots')
    expect(intoLayer4(roomy)).toBe(81)

    const tight = buildDramaTreeLayout(sixAct, {
      containerWidthPx: EXIT_HUB_375,
      currentSessionId: 's1',
    })
    expect(tight.layers[4].mode).toBe('dots')
    // The dots are still there and still legible; only the backdrop went.
    expect(tight.layers[4].dots).toHaveLength(81)
    expect(intoLayer4(tight)).toBe(1)
    expect(
      tight.edges.filter((e) => e.y2 === tight.layers[4].y)[0].walked,
    ).toBe(true)
  })

  it('widens the run marker on a degraded row to a visible tick', () => {
    const layout = buildDramaTreeLayout(sixAct, {
      containerWidthPx: EXIT_HUB_320,
      currentSessionId: 's1',
    })
    expect(layout.barMarkerWidth * layout.unitPx).toBeGreaterThanOrEqual(
      2 * DRAMA_TREE_MIN_STROKE_PX - 1e-9,
    )
  })
})

describe('walkedPathFor', () => {
  const tree = treeOf([session('s1', ['dark']), session('s2', ['sunny'])], 3)

  it('returns the coordinates of the named playthrough', () => {
    expect(walkedPathFor(tree, 's2')).toEqual([
      { depth: 0, index: 0 },
      { depth: 1, index: 1 },
    ])
  })

  it('answers nothing rather than guessing', () => {
    expect(walkedPathFor(tree, 'nope')).toEqual([])
    expect(walkedPathFor(tree, null)).toEqual([])
    expect(walkedPathFor(tree, '')).toEqual([])
    expect(walkedPathFor(null, 's1')).toEqual([])
  })
})

/**
 * 分歧圖的反劇透邊界 (BD 計畫 D8.3)。
 *
 * `DramaProgressTree.vue` 只被允許畫座標。一顆沒走過的點是「第 k 層第 i 個
 * 位置」，不是「某一幕」——它不能有標題、縮圖、tooltip、點擊目標，也不能把
 * node id 藏進 `data-*` 裡讓誰撿去打 `GET /nodes/{node_id}`。
 *
 * 這條界線在此之前只靠元件開頭那段註解與人審守著：把 `:data-node="dot.id"`
 * 加回模板，整套測試仍是綠的。這裡掃原始碼的 `<template>` 區塊——不掃 script
 * 與註解，因為那段註解本身就在講「不准有 title / data 屬性」，全檔掃描會被
 * 自己的說明文字誤判。此 repo 無元件掛載基建，`?raw` 是既有慣例
 * （見 `dramaGallery.test.ts`）。
 */
describe('DramaProgressTree anti-spoiler DOM', () => {
  /** 只取最外層 `<template>`…`</template>`（內層 `v-for` 的同名標籤要留著）。 */
  const markup = treeSource.slice(
    treeSource.indexOf('\n<template>'),
    treeSource.lastIndexOf('\n</template>'),
  )

  it('scans a template block that actually exists', () => {
    // 若元件改寫成別的結構讓切片落空，下面每條都會空手通過——先釘住切片本身。
    expect(markup).toContain('<svg')
    expect(markup.length).toBeGreaterThan(500)
  })

  it('renders no text and no picture', () => {
    // SVG `<text>` / `<title>` 會把場景名念出來；`<image>` / `<img>` 會把
    // 還沒走到的那一幕直接畫在玩家眼前。
    expect(markup).not.toMatch(/<text[\s>]/)
    expect(markup).not.toMatch(/<title[\s>]/)
    expect(markup).not.toMatch(/<im(?:age|g)[\s>]/i)
  })

  it('carries no title or tooltip attribute', () => {
    expect(markup).not.toMatch(/(?:^|\s):?title=/m)
    expect(markup).not.toMatch(/aria-describedby=/)
  })

  it('makes nothing inside the figure clickable', () => {
    // 可點＝可選取＝「這一格是什麼」變成一個能問的問題。整張圖是 role="img"。
    expect(markup).not.toMatch(/@click|v-on:|@mouse|@keydown|<button|<a[\s>]/)
  })

  it('emits no data-* attribute, so no node id can ride out on one', () => {
    // 靜態 `data-x=` 與 v-bind 的 `:data-x=` / `v-bind:data-x=` 都算。
    expect(markup).not.toMatch(/\s(?::|v-bind:)?data-[a-z]/i)
    expect(markup).not.toMatch(/node_id|nodeId/)
  })

  it('hides the whole field of dots from screen readers behind one label', () => {
    expect(markup).toContain('aria-hidden="true"')
    expect(markup).toContain('role="img"')
  })
})
