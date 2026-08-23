<script setup lang="ts">
import { computed } from 'vue'
import { ringCircumference, ringDashOffset, ringRadius } from '@/utils/progressRing'

/**
 * 環形進度 (BD 計畫 D8.4) — a small ring for "how much of X is done", reused
 * anywhere a linear progress bar would be too wide (a button, a badge, a
 * list row).
 *
 * Purely presentational: `ratio` is the only input that matters, the SVG
 * math lives in `@/utils/progressRing` so it is testable without mounting
 * this component, and colors resolve through the same theme tokens the rest
 * of the UI kit uses (`--color-primary-light` etc.) rather than hardcoded
 * hex, so the ring stays correct in both themes without a dark-mode branch.
 *
 * Accessible by construction, not by convention: the SVG carries
 * `aria-hidden="true"` unconditionally — a ring of arcs reads as nothing
 * useful to a screen reader — so the *caller* is responsible for the
 * semantics (an `aria-label` on the button or region this ring sits inside,
 * or a visually-hidden text sibling). This component never guesses one.
 */
const props = withDefaults(
  defineProps<{
    /** 0–1. Values outside that range, or non-finite ones, clamp to it. */
    ratio: number
    /** Outer box size in px — the ring always fits exactly inside a square this large. */
    size?: number
    /** Stroke width in px. */
    thickness?: number
    /** Track color (the unfilled ring). Any valid CSS `stroke` value. */
    trackColor?: string
    /** Progress color (the filled arc). Any valid CSS `stroke` value. */
    progressColor?: string
  }>(),
  {
    size: 32,
    thickness: 3,
    trackColor: 'rgba(255, 255, 255, 0.14)',
    progressColor: 'var(--color-primary-light)',
  },
)

const radius = computed(() => ringRadius(props.size, props.thickness))
const center = computed(() => props.size / 2)
const circumference = computed(() => ringCircumference(radius.value))
const dashOffset = computed(() => ringDashOffset(props.ratio, radius.value))
</script>

<template>
  <span
    class="ui-ring"
    :style="{ width: `${size}px`, height: `${size}px` }"
  >
    <svg
      class="ui-ring__svg"
      :viewBox="`0 0 ${size} ${size}`"
      aria-hidden="true"
      focusable="false"
    >
      <circle
        class="ui-ring__track"
        :cx="center"
        :cy="center"
        :r="radius"
        :stroke-width="thickness"
        :stroke="trackColor"
        fill="none"
      />
      <circle
        class="ui-ring__progress"
        :cx="center"
        :cy="center"
        :r="radius"
        :stroke-width="thickness"
        :stroke="progressColor"
        fill="none"
        stroke-linecap="round"
        :stroke-dasharray="circumference"
        :stroke-dashoffset="dashOffset"
        :transform="`rotate(-90 ${center} ${center})`"
      />
    </svg>
    <span v-if="$slots.default" class="ui-ring__center">
      <slot />
    </span>
  </span>
</template>

<style scoped>
.ui-ring {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
}

.ui-ring__svg {
  display: block;
  width: 100%;
  height: 100%;
}

.ui-ring__progress {
  transition: stroke-dashoffset 0.3s ease;
}

.ui-ring__center {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--font-xs);
  line-height: 1;
  color: var(--color-text);
  pointer-events: none;
}
</style>
