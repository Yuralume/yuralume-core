/**
 * Off-screen image release for the chat thread (plan
 * IMAGE_DELIVERY_AND_PAGINATION_PLAN, D6 / ticket IV5-C).
 *
 * **What is NOT guarded here, honestly.** The harness is SSR (`createSSRApp` +
 * `renderToString`, no jsdom by choice — plan D7). It cannot measure layout and
 * it cannot run an `IntersectionObserver`, so nothing below proves that an
 * image actually loses its bitmap when it scrolls away, that it comes back, or
 * that the box does not move when it does. Those three are **manual
 * acceptance** — see the checklist at the bottom of this file.
 *
 * What *is* guarded: the arithmetic and the wiring. The rule in numbers, the
 * exact string handed to the observer, the empty-string contract `UiImage`
 * turns into "no `src` attribute at all", and — read straight off the source —
 * that the panel and the bubble are still plugged into all of it. Those are the
 * parts that can be silently rewritten into something that still renders.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import {
  OFFSCREEN_IMAGE_RELEASE_MARGIN_PX,
  offscreenReleaseRootMargin,
  releasedImageSrc,
  shouldReleaseAtDistance,
  shouldReleaseImages,
  usableImageBox,
} from '@/utils/offscreenImageRelease'

// ----------------------------------------------------------------------
// The rule, in numbers
// ----------------------------------------------------------------------

describe('shouldReleaseAtDistance', () => {
  it('keeps whatever is on screen or just past the edge', () => {
    expect(shouldReleaseAtDistance({ distancePx: 0 })).toBe(false)
    expect(shouldReleaseAtDistance({
      distancePx: OFFSCREEN_IMAGE_RELEASE_MARGIN_PX,
    })).toBe(false)
  })

  it('releases only beyond the margin', () => {
    expect(shouldReleaseAtDistance({
      distancePx: OFFSCREEN_IMAGE_RELEASE_MARGIN_PX + 1,
    })).toBe(true)
  })

  it('takes a caller threshold — the margin is a default, not the rule', () => {
    expect(shouldReleaseAtDistance({ distancePx: 500, marginPx: 200 })).toBe(true)
    expect(shouldReleaseAtDistance({ distancePx: 500, marginPx: 900 })).toBe(false)
  })

  it('keeps the image when the measurement is meaningless', () => {
    // Releasing wrongly is the failure the reader can see (a blank box in
    // front of them); holding wrongly only costs memory.
    expect(shouldReleaseAtDistance({ distancePx: Number.NaN })).toBe(false)
    expect(shouldReleaseAtDistance({ distancePx: Number.POSITIVE_INFINITY }))
      .toBe(false)
  })

  it('falls back to the default for a nonsense threshold', () => {
    expect(shouldReleaseAtDistance({ distancePx: 1300, marginPx: Number.NaN }))
      .toBe(true)
    expect(shouldReleaseAtDistance({ distancePx: 900, marginPx: -1 }))
      .toBe(false)
  })
})

// ----------------------------------------------------------------------
// The same rule, as the browser takes it
// ----------------------------------------------------------------------

describe('offscreenReleaseRootMargin', () => {
  it('grows the root vertically by the margin and horizontally by nothing', () => {
    // Horizontal is 0 on purpose: the thread only scrolls vertically.
    expect(offscreenReleaseRootMargin())
      .toBe(`${OFFSCREEN_IMAGE_RELEASE_MARGIN_PX}px 0px`)
    expect(offscreenReleaseRootMargin(300)).toBe('300px 0px')
  })

  it('says the same thing the distance rule says', () => {
    // The whole reason the observer callback does no arithmetic: growing the
    // root by the margin *is* the distance test. If these two ever drift, the
    // tested rule stops being the shipped rule.
    const margin = Number.parseInt(offscreenReleaseRootMargin(750), 10)
    expect(shouldReleaseAtDistance({ distancePx: margin + 1, marginPx: 750 }))
      .toBe(true)
    expect(shouldReleaseAtDistance({ distancePx: margin, marginPx: 750 }))
      .toBe(false)
  })

  it('emits a length the browser can parse even when handed junk', () => {
    // `rootMargin` throws on a malformed string, which would take the whole
    // bubble down; a bad knob has to degrade to the default instead.
    expect(offscreenReleaseRootMargin(Number.NaN))
      .toBe(`${OFFSCREEN_IMAGE_RELEASE_MARGIN_PX}px 0px`)
    expect(offscreenReleaseRootMargin(-40))
      .toBe(`${OFFSCREEN_IMAGE_RELEASE_MARGIN_PX}px 0px`)
    expect(offscreenReleaseRootMargin(120.6)).toBe('121px 0px')
  })
})

describe('shouldReleaseImages', () => {
  it('reads one observer callback', () => {
    expect(shouldReleaseImages({ intersecting: false })).toBe(true)
    expect(shouldReleaseImages({ intersecting: true })).toBe(false)
  })

  it('never releases where there is no observer to put it back', () => {
    // No IntersectionObserver (SSR, an old browser) means no second callback
    // is ever coming. Releasing there would drop a picture permanently.
    expect(shouldReleaseImages({ intersecting: false, observerSupported: false }))
      .toBe(false)
  })
})

// ----------------------------------------------------------------------
// What gets handed to UiImage
// ----------------------------------------------------------------------

describe('releasedImageSrc', () => {
  it('hands back the URL while the picture is worth holding', () => {
    expect(releasedImageSrc('/v1/public/a.png', false)).toBe('/v1/public/a.png')
  })

  it('releases to an empty string, not to null', () => {
    // `UiImage` turns `''` into *no* `src` attribute. `src=""` would resolve to
    // the current document URL, so the browser would fetch the SPA's own HTML
    // and feed it to an image decoder — the opposite of releasing memory.
    expect(releasedImageSrc('/v1/public/a.png', true)).toBe('')
    expect(releasedImageSrc(null, false)).toBe('')
    expect(releasedImageSrc(undefined, true)).toBe('')
  })
})

describe('usableImageBox', () => {
  it('takes a real box', () => {
    expect(usableImageBox(240, 360)).toEqual({ width: 240, height: 360 })
    expect(usableImageBox(239.6, 359.4)).toEqual({ width: 240, height: 359 })
  })

  it('refuses a zero box rather than pinning the image to nothing', () => {
    // An element inside a skipped `content-visibility` subtree, or one measured
    // before layout, reports 0. Recording that as the placeholder would collapse
    // the picture the next time it is released.
    expect(usableImageBox(0, 360)).toBeNull()
    expect(usableImageBox(240, 0)).toBeNull()
    expect(usableImageBox(-5, 360)).toBeNull()
    expect(usableImageBox(Number.NaN, 360)).toBeNull()
    expect(usableImageBox(240, Number.POSITIVE_INFINITY)).toBeNull()
  })
})

// ----------------------------------------------------------------------
// Wiring, read from source
//
// The SSR harness cannot render `ChatPanel` (too large) and cannot observe or
// measure anything. These read the connections that, if dropped, leave every
// assertion above passing and the feature entirely absent.
// ----------------------------------------------------------------------

function source(file: string): string {
  return readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
}

describe('chat bubble picture wiring', () => {
  const bubble = source('components/ChatBubble.vue')

  it('asks for the mid-size variant, not the original', () => {
    expect(bubble).toContain('variant="content"')
  })

  it('states how wide the picture actually paints', () => {
    // An absent `sizes` makes the browser assume `100vw` and pick the largest
    // candidate for a 240px picture — the waste this ticket exists to stop.
    expect(bubble).toContain(':sizes="BUBBLE_IMAGE_SIZES"')
    expect(bubble).toMatch(/const BUBBLE_IMAGE_SIZES = '[^']+'/)
  })

  it('routes src through the release decision instead of binding it raw', () => {
    expect(bubble).toContain(':src="imageSrcFor(att.url)"')
    expect(bubble).toContain('releasedImageSrc(url, imagesReleased.value)')
  })

  it('observes the picture strip and stops when the bubble goes', () => {
    expect(bubble).toContain('observeOffscreenRelease(')
    expect(bubble).toMatch(/onBeforeUnmount\([\s\S]{0,400}stopObservingImages\?\.\(\)/)
  })

  it('feeds the measured box back as the placeholder', () => {
    // The red line: releasing must not move anything. The placeholder left
    // behind is the box the picture occupied, measured on load.
    expect(bubble).toContain(':width="imageBoxes[att.url]?.width"')
    expect(bubble).toContain(':height="imageBoxes[att.url]?.height"')
    expect(bubble).toContain('@load="rememberImageBox(att.url, $event)"')
  })

  it('keeps the height auto so a pinned width cannot squash the picture', () => {
    // A replaced element with both dimensions pinned ignores its ratio when
    // `max-width` shrinks it. Without this the measured placeholder would
    // distort the loaded picture on a narrow window.
    expect(bubble).toMatch(/\.bubble-image \{[^}]*height: auto;/)
  })

  it('still hands the original on, not the released display src', () => {
    // The picture no longer links anywhere: clicking opens the lightbox, which
    // carries "open the original" itself (LB3). What matters here is unchanged
    // — the *original* URL is what leaves this component, because the display
    // src is empty for exactly as long as the release is in effect. Feed that
    // to the overlay and a picture scrolled far enough away opens blank.
    expect(bubble).toContain('chatImageLightboxItems(imageAttachments.value)')
    expect(bubble).not.toContain(':items="imageSrcFor')
  })
})

describe('chat panel message-item containment', () => {
  const panel = source('components/ChatPanel.vue')

  it('keeps off-screen message items out of layout and paint', () => {
    expect(panel).toMatch(/content-visibility:\s*auto/)
  })

  it('gives them a remembered intrinsic size', () => {
    // Without `contain-intrinsic-size` a skipped item is 0px tall and the
    // scrollbar collapses; without `auto` every item the reader has already
    // scrolled past re-estimates itself and the bar jitters.
    expect(panel).toMatch(/contain-intrinsic-size:\s*auto\s+\d+px/)
  })

  it('applies it to message items only, not to every child', () => {
    // The load-older button, the first-turn guide, the typing indicator and
    // the credits / session-cap cards are single elements pinned to the ends of the
    // thread; paint containment on them is risk with no saving.
    expect(panel).toContain('.messages-container > .bubble')
    expect(panel).toContain('.messages-container > .scene-frame')
    expect(panel).not.toMatch(/\.messages-container > \*/)
  })
})

describe('IV10 is still wired the way IV10 left it', () => {
  // IV5-C shares this file with the pagination that landed just before it, and
  // the interaction is real: `restoredScrollTop` corrects by a *total height
  // delta* read one tick after the prepend, so anything that changes an item's
  // height afterwards invalidates the correction it already applied.
  const panel = source('components/ChatPanel.vue')

  it('still corrects the scroll anchor by the measured delta', () => {
    expect(panel).toContain('restoredScrollTop(anchor, el.scrollHeight)')
  })

  it('still refuses to reflow the thread mid-turn, before and after the await', () => {
    expect(panel.match(/if \(historyReflowBlocked\.value\) return/g)?.length)
      .toBeGreaterThanOrEqual(2)
  })

  it('still shifts the pinned index by the prepended count', () => {
    expect(panel).toContain('shiftPinnedIndex(')
  })
})

// ----------------------------------------------------------------------
// Manual acceptance — no guard exists for any of this, by construction
// ----------------------------------------------------------------------
//
// 1. Scroll a thread with several pictures well past one of them, then check
//    the tab's memory in Chrome's task manager: it should fall, and the
//    released `<img>` should have no `src` attribute in the inspector.
// 2. Scroll back: the picture returns (cache hit) and *nothing above or below
//    it moves*. Watch a caption or a following bubble, not the picture.
// 3. Load older messages (IV10) into a thread whose older pages contain
//    pictures: the message under the cursor must not move, at the moment of
//    the prepend and again as each picture decodes.
// 4. Upload a wide picture (not the 2:3 the product generates), let it load,
//    scroll it away and back: the box must be identical in both states.
// 5. The typewriter reveal still plays on the bubble that is speaking.
