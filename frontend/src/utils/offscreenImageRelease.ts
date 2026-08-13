/**
 * When an image that has scrolled away should stop holding a decoded bitmap
 * (D6 / ticket IV5-C).
 *
 * The measurement this exists for: a single chat thread held 39 loaded images
 * worth 228 MB of decoded bitmaps, and **30 of them (174 MB) were nowhere near
 * the viewport**. Decoded size is `width x height x 4 bytes` and is paid for as
 * long as the element keeps its URL, whether or not anything ever paints it.
 * Dropping `src` is what gives that memory back; putting it back is a cache
 * hit (the delivery route ships `immutable` + ETag, ~26 ms).
 *
 * Everything here is a pure function of numbers, because the frontend test
 * harness is SSR (`createSSRApp` + `renderToString`, no jsdom): it can neither
 * measure layout nor run an `IntersectionObserver`. The browser glue lives in
 * `offscreenImageObserver.ts` and is deliberately thin, so that the part with a
 * decision in it is the part under test — the same split `chatHistoryScroll.ts`
 * uses for IV10.
 */

/**
 * How far outside the viewport an image has to be before its bitmap is
 * released, in CSS px, on each of the top and bottom edges.
 *
 * Derived, not picked: the measured thread was 39059 px of scroll over 316
 * messages carrying 39 images — about one image per 1000 px of thread — and a
 * chat image box is at most 360 px tall (`.bubble-image { max-height: 360px }`).
 * So 1200 px keeps roughly one image beyond the viewport on each side (~3-5
 * decoded images total, ~10-17 MB at the w768 variant, which is the plan's
 * "in-window only" target), while still giving more than a screenful of runway:
 * a fling has to cross 1200 px before a released image could be looked at, and
 * the re-fetch is a cache hit.
 *
 * Callers may override it; this is the default, not the rule.
 *
 * It is a ceiling rather than an exact distance in the chat thread, because
 * `ChatPanel` also gives message items `content-visibility: auto`: a skipped
 * subtree has no boxes, so an observer reports anything inside it as not
 * intersecting. Where the two disagree the tighter one wins, and the browser's
 * relevancy margin (half a viewport) is usually tighter. Both only ever
 * *release*, so the interaction costs memory nothing and buys a re-decode.
 */
export const OFFSCREEN_IMAGE_RELEASE_MARGIN_PX = 1200

function normalizedMargin(marginPx: number | undefined): number {
  if (marginPx === undefined) return OFFSCREEN_IMAGE_RELEASE_MARGIN_PX
  if (!Number.isFinite(marginPx) || marginPx < 0) {
    return OFFSCREEN_IMAGE_RELEASE_MARGIN_PX
  }
  return Math.round(marginPx)
}

export interface DistanceReleaseInput {
  /**
   * Gap in CSS px between the image's box and the nearest viewport edge.
   * Zero (or negative) while any part of it is on screen.
   */
  distancePx: number
  marginPx?: number
}

/**
 * The rule, stated in the units a human can check: strictly *beyond* the
 * margin gets released, exactly at the margin does not.
 *
 * A non-finite distance means whatever measured it has no idea where the
 * element is; keeping the image is the fail-safe, because the failure mode of
 * releasing wrongly (a blank box in front of the reader) is the visible one.
 */
export function shouldReleaseAtDistance(input: DistanceReleaseInput): boolean {
  if (!Number.isFinite(input.distancePx)) return false
  return input.distancePx > normalizedMargin(input.marginPx)
}

/**
 * The same rule, in the form an `IntersectionObserver` takes.
 *
 * Growing the observer's root by the margin makes "not intersecting" mean
 * exactly "further away than the margin", which is why the observer callback
 * needs no arithmetic of its own. Horizontal margin is 0 on purpose: the chat
 * thread only ever scrolls vertically, and a non-zero horizontal margin would
 * just widen the root for nothing.
 */
export function offscreenReleaseRootMargin(marginPx?: number): string {
  return `${normalizedMargin(marginPx)}px 0px`
}

export interface ObserverReleaseInput {
  /** `IntersectionObserverEntry.isIntersecting`, against the grown root. */
  intersecting: boolean
  /** False on browsers (and in SSR) with no `IntersectionObserver`. */
  observerSupported?: boolean
}

/**
 * Translate one observer callback into the decision.
 *
 * With no observer nothing is ever released: an unsupported browser keeps
 * today's behaviour (more memory, nothing broken) rather than losing images it
 * can never be told to restore.
 */
export function shouldReleaseImages(input: ObserverReleaseInput): boolean {
  if (input.observerSupported === false) return false
  return !input.intersecting
}

/**
 * The URL to hand `UiImage` — empty string, never `null`/`undefined`.
 *
 * `UiImage` turns an empty `src` into *no* `src` attribute at all, which is the
 * only correct way to release: `src=""` resolves to the current document URL,
 * so the browser would fetch the SPA's own HTML and hand it to an image
 * decoder — spending bandwidth and a decode to end up with a broken image.
 */
export function releasedImageSrc(
  url: string | null | undefined,
  released: boolean,
): string {
  if (released) return ''
  return url ?? ''
}

/** A laid-out image box, in CSS px. */
export interface ImageBox {
  width: number
  height: number
}

/**
 * Accept a measurement only if it describes a real box.
 *
 * This is the guard behind the ticket's red line — *releasing must not change
 * the layout*. The box measured while the image was loaded is fed back as the
 * element's `width`/`height`, so the placeholder left behind occupies exactly
 * what the picture occupied. Without it, a released image falls back to the
 * variant's 2 / 3 ratio against the CSS default object size (300x150), which is
 * right for the 1024x1536 the product generates and wrong for an upload of any
 * other shape — a wide photo would *grow* 150 px on release.
 *
 * Zero is the failure case, not a small box: an element inside a skipped
 * `content-visibility` subtree, or one measured before layout, reports 0.
 * Recording that would pin the image to nothing.
 */
export function usableImageBox(
  width: number,
  height: number,
): ImageBox | null {
  if (!Number.isFinite(width) || !Number.isFinite(height)) return null
  if (width <= 0 || height <= 0) return null
  return { width: Math.round(width), height: Math.round(height) }
}
