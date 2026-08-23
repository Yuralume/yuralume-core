import {
  DRAMA_BRANCH_FACTOR,
  type DramaTreeNodePosition,
  type DramaWalkedTree,
} from '@/utils/dramaProgress'

/**
 * 分歧圖 (BD 計畫 D8.3) — the geometry behind the branching graph.
 *
 * Everything here is pure arithmetic over `DramaWalkedTree`, which already
 * carries nothing but coordinates (see `utils/dramaProgress`' header on the
 * anti-spoiler boundary). No node id, title, summary or image path passes
 * through this module, so the picture it plans is a *shape* and cannot leak
 * what sits on a branch nobody walked.
 *
 * ## Why a layered dot triangle and not concentric rings
 *
 * The obvious drawing for a tree of playthroughs is a sunburst: one ring per
 * act, lit wedges for what was walked. It is wrong here, and wrong in a way
 * that quietly contradicts the number printed next to it. Rings of equal
 * thickness weight every act the same, but a 3-ary tree does not: with six
 * acts the last layer alone is 243 of 364 nodes — 67% of the whole
 * denominator. A player who has walked two full paths lights up a large,
 * satisfying fraction of the rings while the completion line says 3.6%, and
 * the two never agree again.
 *
 * A layered triangle whose rows are as wide as they are populous has the
 * property the sunburst lacks, for free: **lit area is proportional to
 * walked nodes**, because every node — at any depth — occupies exactly one
 * slot of the same width. The picture and the percentage are then the same
 * fact drawn twice, which is the entire reason both are on screen.
 */

/** `3^depth` — how many nodes live on one layer. */
export function treeLayerNodeCount(depth: number): number {
  return DRAMA_BRANCH_FACTOR ** depth
}

/**
 * How many individual dots the whole picture may draw.
 *
 * A 12-act drama is 265,720 nodes. Drawing one SVG `<circle>` each is not a
 * slow render, it is a dead tab: a quarter-million DOM nodes plus a
 * quarter-million reactive list entries, on a panel a player opens casually.
 * The budget is the hard stop, and layers that do not fit inside it degrade
 * (see `planTreeLayerModes`) rather than being dropped — a deep layer still
 * says how much of itself was walked, just as a bar instead of as dots.
 *
 * 1000 was chosen so the default six-act tree (364 nodes) draws in full: the
 * common case must be the pretty one. Whether it *does* is then a second
 * question — its widest row is 243 dots, which needs about 870px of
 * container before those dots are big enough to see, so on a phone the same
 * tree still degrades its last row (see `maxDotRowNodes`). This budget is
 * only about how many circles the DOM can carry.
 */
export const DRAMA_TREE_DOT_BUDGET = 1000

/**
 * Widest layer that still gets its *un-walked* skeleton edges drawn.
 *
 * At 81 nodes across a 300-unit row the slots are 3.7 units apart and the
 * connecting lines still read as a tree; at 243 they merge into a grey wash
 * that hides the very dots they are supposed to connect. Above this width
 * the skeleton is dropped — but the lines the player actually *walked* are
 * drawn at every depth regardless, because those are at most one per layer
 * per playthrough and they are the signal, not the noise.
 *
 * This is the ceiling that holds on *any* screen; a narrow container drops
 * the skeleton earlier still, on the pixel test in `buildEdges`.
 */
export const DRAMA_TREE_EDGE_MAX_LAYER_NODES = 81

/** viewBox width. Arbitrary user units — only ratios inside it matter. */
export const DRAMA_TREE_VIEW_WIDTH = 300

/**
 * Smallest dot the picture is allowed to draw, **in rendered CSS pixels**.
 *
 * The viewBox is resolution-free by design, which is exactly the trap: the
 * SVG is `width: 100%`, so one view unit is worth whatever the container is
 * divided by 300, and a layer that looks generous in units can arrive on
 * screen as fog. A six-act tree's widest layer is 243 nodes; inside the
 * ending overlay (`width: min(440px, 100vw - 32px)`, so ~370px of drawing
 * room, ~291px on a 375px phone) that is a 1.0–1.3px dot filled at
 * `rgba(255,255,255,.16)` — after antialiasing, roughly 5% effective alpha
 * spread over one pixel, which is *nothing*. The dots are the only carrier
 * this feature has, so the floor is stated in pixels and every other number
 * here bends to keep it.
 *
 * 3px is not an aesthetic preference: it is the width at which the dim fill
 * survives antialiasing as a mark rather than a smudge.
 */
export const DRAMA_TREE_MIN_DOT_PX = 3

/** Thinnest line worth drawing, same reasoning, same units. */
export const DRAMA_TREE_MIN_STROKE_PX = 1

/**
 * Skeleton lines need clear air between them or they stop being a grid and
 * become a wash. The test is against the *stroke*, not a node count: lines
 * this thick need gaps this many times thicker.
 */
export const DRAMA_TREE_EDGE_SLOT_STROKE_RATIO = 4

/**
 * Width assumed before the container has been measured.
 *
 * A component learns its width one tick after it first renders, and the
 * honest choice for that tick is the *narrow* guess: guessing wide paints
 * one frame of the fog this whole module exists to prevent, while guessing
 * narrow paints one frame of a slightly shorter triangle that then grows.
 * 320px is the narrowest phone we lay out for.
 */
export const DRAMA_TREE_FALLBACK_WIDTH_PX = 320

/**
 * Below this the arithmetic stops meaning anything (a 40px sliver would ask
 * for dots wider than the layer they sit on). Clamping keeps a degenerate
 * container — a collapsed flex child, a measurement taken mid-transition —
 * from producing a degenerate picture.
 */
export const DRAMA_TREE_MIN_LAYOUT_WIDTH_PX = 160

const ROW_HEIGHT_DOTS = 13
const ROW_HEIGHT_BAR = 11
const PAD_Y = 5
const BAR_HEIGHT = 5

/** Dot radius as a fraction of one slot, so neighbours never collide. */
const DOT_RADIUS_RATIO = 0.42
/** …but a shallow layer's slots are enormous; a 60-unit dot is a blob. */
const DOT_RADIUS_MAX = 3
const WALKED_RADIUS_SCALE = 1.55
const CURRENT_RADIUS_SCALE = 2.2

/** Base line weights, in view units, before the pixel floor is applied. */
const EDGE_STROKE_SKELETON = 0.35
const EDGE_STROKE_WALKED = 0.7
const EDGE_STROKE_CURRENT = 1
/** Base width of the "this run" tick on a degraded layer, in view units. */
const BAR_MARKER_WIDTH = 2

/**
 * A walked-but-invisible layer is a lie. When coverage is 1/2187 the fill is
 * a seventh of a unit wide and rounds away to nothing, which reads as "you
 * never went there" — so a non-empty layer always keeps a visible sliver.
 */
const BAR_MIN_VISIBLE_FILL = 2

/**
 * The container width to lay out for, with the two ways a caller can fail to
 * supply one folded into a usable number.
 */
export function normalizeTreeWidthPx(
  containerWidthPx: number | null | undefined,
): number {
  const measured = containerWidthPx
  if (typeof measured !== 'number' || !Number.isFinite(measured)) {
    return DRAMA_TREE_FALLBACK_WIDTH_PX
  }
  if (measured <= 0) return DRAMA_TREE_FALLBACK_WIDTH_PX
  return Math.max(measured, DRAMA_TREE_MIN_LAYOUT_WIDTH_PX)
}

/**
 * The widest layer this container can still draw dot by dot.
 *
 * The widest *drawn* layer spans the full width, so its slot is
 * `width / n` pixels and its dots are `2 · DOT_RADIUS_RATIO · width / n`
 * across. Solving that for `DRAMA_TREE_MIN_DOT_PX` gives the cap, and
 * because every layer shares one slot (see the header), pinning the widest
 * layer pins every dot in the picture at once.
 *
 * This is the second half of the degrade rule. The first half — the dot
 * budget — asks "can the browser draw this many circles"; this half asks
 * "would a reader see them". A layer must pass both, and the answers differ:
 * on a 370px ending overlay the 243-node layer is well inside the budget and
 * still unreadable, so it degrades on width alone.
 */
export function maxDotRowNodes(containerWidthPx: number): number {
  const width = normalizeTreeWidthPx(containerWidthPx)
  const cap = (2 * DOT_RADIUS_RATIO * width) / DRAMA_TREE_MIN_DOT_PX
  return Math.max(1, Math.floor(cap))
}

export type DramaTreeLayerMode = 'dots' | 'bar'

/** Why a layer is a bar — `null` when it draws as dots. */
export type DramaTreeDegradeReason = 'budget' | 'width'

export interface DramaTreeLayerPlan {
  depth: number
  nodeCount: number
  mode: DramaTreeLayerMode
  degradedBy: DramaTreeDegradeReason | null
}

export interface DramaTreeLayerModeOptions {
  budget?: number
  /**
   * Widest layer that still reads as dots at the size it will be rendered.
   * Omitted means "width unknown" and the plan falls back to the count
   * budget alone — callers that know the container pass
   * `maxDotRowNodes(width)`.
   */
  maxRowNodes?: number
}

/**
 * Which layers are drawn dot-by-dot and which degrade to a coverage bar.
 *
 * The decision is **per layer, cumulative** — not "the tree is deep, draw
 * bars". Layers are visited shallow-first and each one is admitted if it
 * still fits inside the remaining budget, so a 12-act drama keeps its first
 * six layers as real dots (364 of them) and only the six that cannot fit
 * become bars. Rejecting a layer never spends budget, so the check stays
 * honest for the layers after it.
 *
 * Layer size grows monotonically (`3^d`), so in practice the modes come out
 * as a run of dots followed by a run of bars — but the loop does not assume
 * it, which is what keeps the rule readable as "does this layer fit".
 *
 * "Fit" has two independent meanings and a layer must satisfy both: it must
 * fit the *drawing* budget (how many circles the DOM can carry) and the
 * *reading* budget (how narrow a dot may get before it stops being one, see
 * `maxDotRowNodes`). Rejecting a layer never spends drawing budget, on
 * either ground, so the check stays honest for the layers after it.
 */
export function planTreeLayerModes(
  depth: number,
  options: DramaTreeLayerModeOptions = {},
): DramaTreeLayerPlan[] {
  if (!Number.isFinite(depth) || depth <= 0) return []
  const budget = options.budget ?? DRAMA_TREE_DOT_BUDGET
  const maxRowNodes = options.maxRowNodes ?? Number.POSITIVE_INFINITY
  const plans: DramaTreeLayerPlan[] = []
  let spent = 0
  for (let d = 0; d < depth; d += 1) {
    const nodeCount = treeLayerNodeCount(d)
    const fitsWidth = nodeCount <= maxRowNodes
    const fitsBudget = spent + nodeCount <= budget
    const degradedBy: DramaTreeDegradeReason | null = !fitsWidth
      ? 'width'
      : !fitsBudget
        ? 'budget'
        : null
    if (degradedBy === null) spent += nodeCount
    plans.push({
      depth: d,
      nodeCount,
      mode: degradedBy === null ? 'dots' : 'bar',
      degradedBy,
    })
  }
  return plans
}

export interface DramaTreeDot {
  /** Position across the layer. Geometry only — never rendered as text. */
  index: number
  x: number
  y: number
  r: number
  walked: boolean
  /** Part of the playthrough the caller marked as "this run". */
  current: boolean
}

export interface DramaTreeEdge {
  key: string
  x1: number
  y1: number
  x2: number
  y2: number
  walked: boolean
  current: boolean
}

export interface DramaTreeLayer {
  depth: number
  nodeCount: number
  /** Distinct indices on this layer the player has stood on. */
  walkedCount: number
  /** `walkedCount / nodeCount`, `0..1`. */
  coverage: number
  mode: DramaTreeLayerMode
  /** Row centre line. */
  y: number
  /** Empty in `bar` mode. */
  dots: DramaTreeDot[]
  /** `bar` mode only — the filled span, in view units. */
  barFillWidth: number
  barHeight: number
  /**
   * `bar` mode only — where along the row this run's node sits, so the
   * highlighted path stays traceable into the layers that degraded.
   * `null` when this run did not reach the layer.
   */
  currentX: number | null
}

/**
 * Line thicknesses, in view units, already corrected for how small this
 * container makes a unit.
 *
 * They are layout output rather than CSS because CSS cannot see the
 * viewBox: `stroke-width: .35` is 1px of ink on a 900px gallery and 0.28px
 * — i.e. a 28%-alpha ghost of the colour that was chosen — inside a 370px
 * ending overlay. Same declaration, two different pictures.
 */
export interface DramaTreeStrokeWidths {
  skeleton: number
  walked: number
  current: number
}

export interface DramaTreeLayout {
  width: number
  height: number
  layers: DramaTreeLayer[]
  edges: DramaTreeEdge[]
  /** Circles the component will actually emit. Bounded by the budget. */
  dotCount: number
  barLayerCount: number
  /** Container width this layout was planned for, after clamping. */
  containerWidthPx: number
  /** Rendered CSS pixels per view unit — the bridge between the two. */
  unitPx: number
  strokeWidths: DramaTreeStrokeWidths
  /** Width of the "this run" tick on a degraded layer, in view units. */
  barMarkerWidth: number
}

export interface DramaTreeLayoutOptions {
  /**
   * The playthrough to draw as "this run". Chosen by the caller — a page
   * knows which session just ended; this module must not guess.
   */
  currentSessionId?: string | null
  budget?: number
  /**
   * Rendered width of the SVG, in CSS pixels. The component measures it;
   * everything that has a minimum legible size is derived from it. Omitted
   * (first frame, or a caller that genuinely cannot measure) falls back to
   * `DRAMA_TREE_FALLBACK_WIDTH_PX`.
   */
  containerWidthPx?: number | null
}

/**
 * The coordinates one playthrough walked, or `[]` when that session is not
 * in the tree (nothing selected, a cleared drama, an id from another one).
 */
export function walkedPathFor(
  tree: DramaWalkedTree | null | undefined,
  sessionId: string | null | undefined,
): DramaTreeNodePosition[] {
  if (!tree || typeof sessionId !== 'string' || !sessionId) return []
  return tree.paths.find((p) => p.sessionId === sessionId)?.nodes ?? []
}

function emptyLayout(containerWidthPx: number): DramaTreeLayout {
  const unitPx = containerWidthPx / DRAMA_TREE_VIEW_WIDTH
  return {
    width: DRAMA_TREE_VIEW_WIDTH,
    height: 0,
    layers: [],
    edges: [],
    dotCount: 0,
    barLayerCount: 0,
    containerWidthPx,
    unitPx,
    strokeWidths: strokeWidthsFor(unitPx),
    barMarkerWidth: barMarkerWidthFor(unitPx),
  }
}

/**
 * Scale every line up together until the thinnest one clears
 * `DRAMA_TREE_MIN_STROKE_PX`, so the skeleton/walked/current hierarchy the
 * design encodes as 0.35/0.7/1 survives the correction instead of being
 * flattened by three separate clamps.
 */
function strokeWidthsFor(unitPx: number): DramaTreeStrokeWidths {
  const scale =
    unitPx > 0
      ? Math.max(1, DRAMA_TREE_MIN_STROKE_PX / (EDGE_STROKE_SKELETON * unitPx))
      : 1
  return {
    skeleton: EDGE_STROKE_SKELETON * scale,
    walked: EDGE_STROKE_WALKED * scale,
    current: EDGE_STROKE_CURRENT * scale,
  }
}

function barMarkerWidthFor(unitPx: number): number {
  if (unitPx <= 0) return BAR_MARKER_WIDTH
  return Math.max(BAR_MARKER_WIDTH, (2 * DRAMA_TREE_MIN_STROKE_PX) / unitPx)
}

/**
 * Plan the whole picture: rows, dots, edges and degraded bars.
 *
 * ## The horizontal mapping
 *
 * Every node in every dot layer occupies one slot of the same width —
 * `viewWidth / widestDotLayer` — and each layer is centred. So layer `d`
 * spans `3^d` slots and its width is literally proportional to how many
 * nodes it holds; that is the property that keeps lit area proportional to
 * walked nodes (see the file header). A node's centre is
 * `width/2 + (index + 0.5 - nodeCount/2) * slot`, which puts the root dead
 * centre and the widest drawn layer flush to both edges.
 *
 * Degraded layers span the full width instead — they are the widest layers
 * of the tree, so a full-width bar is the honest continuation of the
 * triangle rather than a change of subject.
 *
 * ## Why the picture degrades instead of scrolling
 *
 * The other way to keep 243 dots legible on a 291px phone is to draw them
 * 868px wide inside an `overflow-x: auto` box. It defeats the purpose: this
 * figure exists so a *shape* can be taken in at a glance — lit area against
 * total area — and a shape you can only see a third of at a time is not a
 * shape, it is a strip. Degrading keeps the whole triangle on screen and
 * says the truth about the rows that no longer fit, as a bar. (It also
 * keeps the ending overlay, which already scrolls vertically, from growing
 * a second scroll axis under the player's thumb.)
 */
export function buildDramaTreeLayout(
  tree: DramaWalkedTree | null | undefined,
  options: DramaTreeLayoutOptions = {},
): DramaTreeLayout {
  const containerWidthPx = normalizeTreeWidthPx(options.containerWidthPx)
  const unitPx = containerWidthPx / DRAMA_TREE_VIEW_WIDTH
  const strokeWidths = strokeWidthsFor(unitPx)
  const barMarkerWidth = barMarkerWidthFor(unitPx)

  const depth = tree?.depth ?? 0
  if (!tree || depth <= 0) return emptyLayout(containerWidthPx)

  const plans = planTreeLayerModes(depth, {
    budget: options.budget,
    maxRowNodes: maxDotRowNodes(containerWidthPx),
  })
  if (plans.length === 0) return emptyLayout(containerWidthPx)

  const dotPlans = plans.filter((p) => p.mode === 'dots')
  const widestDotLayer = dotPlans.length
    ? dotPlans[dotPlans.length - 1].nodeCount
    : 0
  const slot = widestDotLayer > 0 ? DRAMA_TREE_VIEW_WIDTH / widestDotLayer : 0
  /**
   * The floor is a second lock on the same guarantee, for the case the
   * degrade rule cannot reach: a shallow tree's slots are enormous, so
   * `DOT_RADIUS_MAX` caps the radius in *units* and a narrow enough
   * container can still shrink that cap below the legible size. It can
   * never make dots collide — a layer is only admitted when its own slot
   * already affords `DRAMA_TREE_MIN_DOT_PX`, and every narrower layer
   * shares that slot with fewer nodes.
   */
  const minRadius = DRAMA_TREE_MIN_DOT_PX / 2 / unitPx
  const baseRadius = Math.max(
    Math.min(slot * DOT_RADIUS_RATIO, DOT_RADIUS_MAX),
    minRadius,
  )

  const currentByDepth = new Map<number, number>()
  for (const node of walkedPathFor(tree, options.currentSessionId)) {
    currentByDepth.set(node.depth, node.index)
  }

  const xOf = (index: number, nodeCount: number): number =>
    DRAMA_TREE_VIEW_WIDTH / 2 + (index + 0.5 - nodeCount / 2) * slot

  const layers: DramaTreeLayer[] = []
  let cursor = PAD_Y
  let dotCount = 0
  let barLayerCount = 0

  for (const plan of plans) {
    const walkedIndices = tree.levels[plan.depth] ?? new Set<number>()
    const walkedCount = walkedIndices.size
    const coverage = plan.nodeCount > 0 ? walkedCount / plan.nodeCount : 0
    const rowHeight = plan.mode === 'dots' ? ROW_HEIGHT_DOTS : ROW_HEIGHT_BAR
    const y = cursor + rowHeight / 2
    cursor += rowHeight

    const currentIndex = currentByDepth.get(plan.depth)

    if (plan.mode === 'dots') {
      const dots: DramaTreeDot[] = []
      for (let index = 0; index < plan.nodeCount; index += 1) {
        const walked = walkedIndices.has(index)
        const current = currentIndex === index
        const scale = current
          ? CURRENT_RADIUS_SCALE
          : walked
            ? WALKED_RADIUS_SCALE
            : 1
        dots.push({
          index,
          x: xOf(index, plan.nodeCount),
          y,
          r: baseRadius * scale,
          walked,
          current,
        })
      }
      dotCount += dots.length
      layers.push({
        depth: plan.depth,
        nodeCount: plan.nodeCount,
        walkedCount,
        coverage,
        mode: 'dots',
        y,
        dots,
        barFillWidth: 0,
        barHeight: 0,
        currentX: null,
      })
      continue
    }

    barLayerCount += 1
    const rawFill = DRAMA_TREE_VIEW_WIDTH * coverage
    layers.push({
      depth: plan.depth,
      nodeCount: plan.nodeCount,
      walkedCount,
      coverage,
      mode: 'bar',
      y,
      dots: [],
      barFillWidth:
        walkedCount > 0 ? Math.max(rawFill, BAR_MIN_VISIBLE_FILL) : 0,
      barHeight: BAR_HEIGHT,
      currentX:
        currentIndex === undefined
          ? null
          : (DRAMA_TREE_VIEW_WIDTH * (currentIndex + 0.5)) / plan.nodeCount,
    })
  }

  return {
    width: DRAMA_TREE_VIEW_WIDTH,
    height: cursor + PAD_Y,
    layers,
    edges: buildEdges(tree, plans, layers, currentByDepth, xOf, {
      containerWidthPx,
      skeletonStrokePx: strokeWidths.skeleton * unitPx,
    }),
    dotCount,
    barLayerCount,
    containerWidthPx,
    unitPx,
    strokeWidths,
    barMarkerWidth,
  }
}

/**
 * Parent→child lines, in paint order: dim skeleton first, walked history
 * over it, this run last.
 *
 * Only drawn between two adjacent *dot* layers — a bar has no per-node
 * position to hang a line off. Two separate density rules apply:
 *
 * - the **skeleton** (branches nobody took) has to clear two independent
 *   bars. `DRAMA_TREE_EDGE_MAX_LAYER_NODES` is resolution-free: past 243
 *   lines a fan is a wash on any monitor. The pixel test is not: the same
 *   81-node layer that reads as a grid on a gallery panel is solid ink at
 *   the width the ending overlay gives it, because the lines stopped
 *   getting thinner (they hit `DRAMA_TREE_MIN_STROKE_PX`) while the gaps
 *   between them kept shrinking. Failing either drops the skeleton — a
 *   drawn-but-illegible grid is worse than no grid, since it hides the dots
 *   it is supposed to be a backdrop for;
 * - **walked** lines are drawn at every dot layer. There is at most one per
 *   playthrough per layer, so they never reach that density, and dropping
 *   them would delete the only thing the player is looking for.
 *
 * A walked edge that is also part of this run is emitted once, in the
 * current pass, so the two never paint on top of each other.
 */
function buildEdges(
  tree: DramaWalkedTree,
  plans: DramaTreeLayerPlan[],
  layers: DramaTreeLayer[],
  currentByDepth: Map<number, number>,
  xOf: (index: number, nodeCount: number) => number,
  render: { containerWidthPx: number; skeletonStrokePx: number },
): DramaTreeEdge[] {
  const isDotLayer = (d: number): boolean =>
    d >= 0 && d < plans.length && plans[d].mode === 'dots'

  /** Is the fan into this layer still readable as separate lines? */
  const skeletonReadable = (nodeCount: number): boolean => {
    if (nodeCount > DRAMA_TREE_EDGE_MAX_LAYER_NODES) return false
    const slotPx = render.containerWidthPx / nodeCount
    return slotPx >= DRAMA_TREE_EDGE_SLOT_STROKE_RATIO * render.skeletonStrokePx
  }

  const edge = (
    d: number,
    index: number,
    walked: boolean,
    current: boolean,
  ): DramaTreeEdge => {
    const parentIndex = Math.floor(index / DRAMA_BRANCH_FACTOR)
    return {
      key: `${current ? 'c' : walked ? 'w' : 's'}:${d}:${index}`,
      x1: xOf(parentIndex, plans[d - 1].nodeCount),
      y1: layers[d - 1].y,
      x2: xOf(index, plans[d].nodeCount),
      y2: layers[d].y,
      walked,
      current,
    }
  }

  const currentKeys = new Set<string>()
  for (const [d, index] of currentByDepth) {
    if (d >= 1 && isDotLayer(d) && isDotLayer(d - 1)) {
      currentKeys.add(`${d}:${index}`)
    }
  }

  const skeleton: DramaTreeEdge[] = []
  const walked: DramaTreeEdge[] = []
  const current: DramaTreeEdge[] = []

  for (let d = 1; d < plans.length; d += 1) {
    if (!isDotLayer(d) || !isDotLayer(d - 1)) continue
    const walkedIndices = tree.levels[d] ?? new Set<number>()
    const parentIndices = tree.levels[d - 1] ?? new Set<number>()

    if (skeletonReadable(plans[d].nodeCount)) {
      for (let index = 0; index < plans[d].nodeCount; index += 1) {
        if (walkedIndices.has(index)) continue
        skeleton.push(edge(d, index, false, false))
      }
    }
    for (const index of walkedIndices) {
      // A walked node whose parent is missing from the layer above cannot
      // happen for a path built by `buildWalkedTree`, but a hand-made tree
      // could say so — and a line down to a node nobody stood on would be a
      // false claim about where the player went.
      if (!parentIndices.has(Math.floor(index / DRAMA_BRANCH_FACTOR))) continue
      if (currentKeys.has(`${d}:${index}`)) {
        current.push(edge(d, index, true, true))
      } else {
        walked.push(edge(d, index, true, false))
      }
    }
  }

  return [...skeleton, ...walked, ...current]
}
