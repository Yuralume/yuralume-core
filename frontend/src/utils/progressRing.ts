/**
 * `UiProgressRing` 的環形進度幾何 (BD 計畫 D8.4)。
 *
 * Pure arithmetic — `ratio` in, `stroke-dasharray` / `stroke-dashoffset`
 * numbers out — kept out of the `.vue` file so it can be tested without a
 * mounted component (this repo has no DOM/component mount harness; see
 * `AGENTS.md`).
 */

/**
 * Coerce a completion ratio into the drawable range `[0, 1]`.
 *
 * A non-finite input (`NaN`, `Infinity`, a prop that typed as `number` but
 * arrived `undefined`) falls back to `0` — an unreadable ratio draws an
 * empty ring, never a full one or a `NaN` stroke offset that Chrome silently
 * refuses to paint.
 */
export function clampRingRatio(ratio: number): number {
  if (!Number.isFinite(ratio)) return 0
  return Math.min(1, Math.max(0, ratio))
}

/** Circumference of a circle with the given radius. Negative radii clamp to `0`. */
export function ringCircumference(radius: number): number {
  return 2 * Math.PI * Math.max(0, radius)
}

/**
 * `stroke-dashoffset` for a ring whose `stroke-dasharray` is set to its own
 * circumference: with that dasharray, the *visible* arc length left after
 * subtracting the offset is `ratio * circumference`, so a plain circle
 * element becomes a progress arc with no path-drawing math in the template.
 *
 * `ratio` is clamped first, so a bad input still returns a finite, drawable
 * offset instead of propagating `NaN` onto the SVG attribute.
 */
export function ringDashOffset(ratio: number, radius: number): number {
  const circumference = ringCircumference(radius)
  return circumference * (1 - clampRingRatio(ratio))
}

/** The stroke radius that centers a ring of `thickness` inside a `size`×`size` box. */
export function ringRadius(size: number, thickness: number): number {
  return Math.max(0, (size - thickness) / 2)
}
