<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDramaCompletionPercent, type DramaProgress } from '@/utils/dramaProgress'
import {
  buildDramaTreeLayout,
  type DramaTreeLayer,
  type DramaTreeLayout,
} from '@/utils/dramaProgressTree'

/**
 * 分歧圖 (BD 計畫 D8.3) — the drama's whole tree, as a shape.
 *
 * A layered dot triangle: layer `k` is `3^k` dots, as wide as it is
 * populous. Walked nodes are lit, un-walked ones are dim, and the run the
 * caller nominates burns gold on top of the accumulated history. The
 * arithmetic — including *why* a triangle and not a sunburst, and how a row
 * degrades into a coverage bar once it runs out of either dot budget or
 * room to be seen in — lives in `@/utils/dramaProgressTree`; this file only
 * measures the box and paints what that module decided.
 *
 * ## What is deliberately absent
 *
 * No titles, no thumbnails, no click targets, no node ids — not in the DOM,
 * not in a `title` attribute, not in a data attribute. The graph is drawn
 * from coordinates alone (see the anti-spoiler note in
 * `@/utils/dramaProgress`), so an un-walked dot is a position and nothing
 * else: there is nothing here that could name the scene sitting on it, and
 * nothing to add later without reopening a boundary the gallery payload
 * (BD9) closes on the wire.
 *
 * Presentational: `progress` and `currentSessionId` are both handed down.
 * Deciding *which* playthrough counts as "this run" is a question about the
 * screen the graph is on — an ending overlay means the session that just
 * finished, the drama's own page means the most recent one — and this
 * component is on both.
 */
const props = defineProps<{
  progress: DramaProgress | null
  /**
   * The playthrough to burn gold. `null` on surfaces where no run is
   * current, in which case the graph shows accumulated history only.
   */
  currentSessionId?: string | null
}>()

const { t } = useI18n()

/**
 * How wide this figure actually is, in CSS pixels.
 *
 * The viewBox is unitless and the SVG is `width: 100%`, so nothing inside
 * the drawing knows whether a "unit" arrives as 3px on a gallery panel or
 * 0.8px inside the 440px ending overlay — and a dot that lands under ~3px
 * is invisible at the alpha these dim fills use. Measuring is what lets the
 * layout choose *how many rows can be dots here* instead of guessing once
 * and fogging over on every phone.
 *
 * `null` until mounted; the layout falls back to a deliberately narrow
 * assumption for that one frame (see `DRAMA_TREE_FALLBACK_WIDTH_PX`).
 *
 * Measured on a padding-free wrapper rather than on the figure, so the
 * number is the SVG's own width and no copy of the figure's padding has to
 * live in TypeScript where it would drift from the stylesheet.
 */
const canvasEl = ref<HTMLElement | null>(null)
const containerWidthPx = ref<number | null>(null)
let observer: ResizeObserver | null = null

function measure(): void {
  const width = canvasEl.value?.clientWidth ?? 0
  if (width > 0) containerWidthPx.value = width
}

/**
 * Attach on every appearance, not once on mount: the figure is `v-if`'d on
 * having a shape to draw, so a drama selected *after* this component
 * mounted brings the canvas into existence long after `onMounted` ran, and
 * a mount-only observer would leave that drama on the un-measured fallback
 * forever.
 *
 * The immediate `measure()` matters for the same reason the observer does
 * not suffice: its first callback may land after the first paint, and that
 * frame is exactly the fog being fixed here.
 */
function attach(el: HTMLElement | null): void {
  observer?.disconnect()
  if (!el) return
  measure()
  if (typeof ResizeObserver === 'undefined') return
  observer ??= new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect.width ?? 0
    if (width > 0) containerWidthPx.value = width
  })
  observer.observe(el)
}

onMounted(() => attach(canvasEl.value))
watch(canvasEl, (el) => attach(el))

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

const layout = computed<DramaTreeLayout>(() =>
  buildDramaTreeLayout(props.progress?.tree ?? null, {
    currentSessionId: props.currentSessionId ?? null,
    containerWidthPx: containerWidthPx.value,
  }),
)

/**
 * Nothing to draw: no drama selected, or a segment count this client could
 * not read into a tree. An empty frame would read as a broken panel.
 */
const hasShape = computed(() => layout.value.layers.length > 0)

/**
 * A field of dots means nothing read aloud, so the whole figure carries one
 * label saying what it is *about* — the same completion fact the header line
 * states — and every dot, line and bar inside it is hidden. Reading out
 * 265,720 positions would be worse than reading out none.
 */
const ariaLabel = computed(() => {
  const c = props.progress?.completion
  if (!c || c.total <= 0) return t('branchingDrama.progressTree.ariaUnknown')
  return t('branchingDrama.progressTree.aria', {
    percent: formatDramaCompletionPercent(c),
    walked: c.walked,
    total: c.total,
  })
})

/** Left edge of the tick marking this run's position on a bar. */
function barMarkerX(layer: DramaTreeLayer): number {
  return Math.max(0, (layer.currentX ?? 0) - layout.value.barMarkerWidth / 2)
}
</script>

<template>
  <figure
    v-if="hasShape"
    class="drama-tree"
    role="img"
    :aria-label="ariaLabel"
  >
    <!-- 量寬度用的無 padding 容器：SVG 是 width:100%，所以這個 div 的寬
         就是圖真正被畫成幾個 CSS 像素——每點多大、哪幾層還畫得成點，全從
         這個數字推。 -->
    <div ref="canvasEl" class="drama-tree__canvas">
      <svg
        class="drama-tree__svg"
        :viewBox="`0 0 ${layout.width} ${layout.height}`"
        preserveAspectRatio="xMidYMid meet"
        aria-hidden="true"
        focusable="false"
      >
        <!-- 邊先畫，點壓在上面：連線只是骨架，亮起來的節點才是主角。
             線寬由版面算出而非寫死在 CSS——同一個 stroke-width 在圖集面是
             1px，在 440px 結局頁只有 0.28px，等於把顏色稀釋掉。 -->
        <line
          v-for="edge in layout.edges"
          :key="edge.key"
          class="drama-tree__edge"
          :class="{
            'is-walked': edge.walked,
            'is-current': edge.current,
          }"
          :x1="edge.x1"
          :y1="edge.y1"
          :x2="edge.x2"
          :y2="edge.y2"
          :stroke-width="
            edge.current
              ? layout.strokeWidths.current
              : edge.walked
                ? layout.strokeWidths.walked
                : layout.strokeWidths.skeleton
          "
        />

        <template v-for="layer in layout.layers" :key="layer.depth">
          <!-- 逐點層 -->
          <circle
            v-for="dot in layer.dots"
            :key="`${layer.depth}:${dot.index}`"
            class="drama-tree__dot"
            :class="{ 'is-walked': dot.walked, 'is-current': dot.current }"
            :cx="dot.x"
            :cy="dot.y"
            :r="dot.r"
          />

          <!-- 畫不成點的深層：整層退化成一條覆蓋率填充條。理由有兩個，
               各自獨立——節點數超過預算（十幾萬個圓會凍住分頁），或者這個
               寬度下每點會小到看不見。條的長度說的仍是同一件事：這層走過
               多少。 -->
          <template v-if="layer.mode === 'bar'">
            <rect
              class="drama-tree__bar-track"
              x="0"
              :y="layer.y - layer.barHeight / 2"
              :width="layout.width"
              :height="layer.barHeight"
              :rx="layer.barHeight / 2"
            />
            <rect
              v-if="layer.barFillWidth > 0"
              class="drama-tree__bar-fill"
              x="0"
              :y="layer.y - layer.barHeight / 2"
              :width="layer.barFillWidth"
              :height="layer.barHeight"
              :rx="layer.barHeight / 2"
            />
            <!-- 這一輪走到這層的哪個位置。條沒有逐點座標，但路徑不該在
                 這裡斷掉。 -->
            <rect
              v-if="layer.currentX !== null"
              class="drama-tree__bar-current"
              :x="barMarkerX(layer)"
              :y="layer.y - layer.barHeight / 2 - 1.5"
              :width="layout.barMarkerWidth"
              :height="layer.barHeight + 3"
              rx="1"
            />
          </template>
        </template>
      </svg>
    </div>
  </figure>
</template>

<style scoped>
.drama-tree {
  margin: 0;
  /* min-width: 0 so a flex/grid parent can shrink this below its content
     instead of pushing the column wider — the SVG scales, the page never
     grows a horizontal scrollbar because of it. */
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.22);
}

.drama-tree__canvas {
  min-width: 0;
}

.drama-tree__svg {
  display: block;
  width: 100%;
  height: auto;
  overflow: visible;
}

/* Line *colours* only. The widths are computed per render and bound as
   attributes, because a fixed `stroke-width` here means a different amount
   of ink at every container size (and a scoped rule would beat the
   attribute). See `strokeWidthsFor` in `@/utils/dramaProgressTree`. */
.drama-tree__edge {
  stroke: rgba(255, 255, 255, 0.16);
}
.drama-tree__edge.is-walked {
  stroke: rgba(var(--color-primary-rgb), 0.55);
}
.drama-tree__edge.is-current {
  stroke: rgba(var(--color-spark-rgb), 0.9);
}

/* An un-walked dot is the faintest thing on screen and the most numerous;
   at 3px across, .16 alpha is a rumour. */
.drama-tree__dot {
  fill: rgba(255, 255, 255, 0.22);
}
.drama-tree__dot.is-walked {
  fill: var(--color-primary-light);
}
.drama-tree__dot.is-current {
  fill: var(--color-spark);
}

.drama-tree__bar-track {
  fill: rgba(255, 255, 255, 0.08);
}
.drama-tree__bar-fill {
  fill: rgba(var(--color-primary-rgb), 0.75);
}
.drama-tree__bar-current {
  fill: var(--color-spark);
}
</style>
