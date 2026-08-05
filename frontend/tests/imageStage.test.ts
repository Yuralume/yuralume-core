/**
 * `ImageStage` — the full-screen portrait carousel (ticket IV5-B of
 * IMAGE_DELIVERY_AND_PAGINATION_PLAN).
 *
 * Two things are pinned here, and both come straight from the measured
 * regression (§1.2 / §1.7): the stage asked for the 2313 KB PNG original, and
 * it mounted **every** URL in `image_urls` as an `<img>`, hiding all but one
 * with `opacity: 0`. A hidden image is still a decoded bitmap — 6.0 MB each,
 * resident for as long as the character is selected.
 *
 * So: every mounted image must be the `full` WebP variant, and the number of
 * mounted images must not grow with `image_urls.length`.
 *
 * Rendering is SSR (`createSSRApp` + `renderToString`) — this repo has no DOM
 * test infra on purpose (no jsdom, no @vue/test-utils). SSR runs `setup()` and
 * the template once, at `activeIndex === 0`, which is enough to prove the
 * wiring; the windowing arithmetic for every *other* index is pinned directly
 * on the exported pure function.
 *
 * What SSR cannot reach, and what therefore has no guard here: the 1.2 s
 * cross-fade, the 8 s rotation timer firing, and click / pointer handlers.
 * Those are asserted only in so far as their markup renders.
 */

import { describe, expect, it } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import ImageStage, { stageMountedIndices } from '@/components/ImageStage.vue'
import { messages as zhTW } from '@/i18n/locales/zh-TW'

const L = (zhTW as { imageStage: Record<string, string> }).imageStage

function urls(count: number): string[] {
  return Array.from({ length: count }, (_, i) => `/v1/public/characters/c1/p${i}.png`)
}

/** Enough of a `Character` for the stage; the rest of the shape is unread. */
function character(imageCount: number) {
  return {
    id: 'char-1',
    name: '芊璃',
    image_urls: urls(imageCount),
    state: { emotion: '平靜' },
  }
}

async function render(props: Record<string, unknown>): Promise<string> {
  const app = createSSRApp(ImageStage, props)
  app.use(
    createI18n({
      legacy: false,
      locale: 'zh-TW',
      fallbackLocale: 'zh-TW',
      messages: { 'zh-TW': zhTW },
    }),
  )
  return renderToString(app)
}

/** The `<img>` elements only — `.stage-dot` buttons also carry `active`. */
function imgTags(html: string): string[] {
  return html.match(/<img\b[^>]*>/g) ?? []
}

function renderedSrcs(html: string): string[] {
  return imgTags(html).map((tag) => /\ssrc="([^"]*)"/.exec(tag)?.[1] ?? '')
}

// ----------------------------------------------------------------------
// The windowing rule, as a pure function
// ----------------------------------------------------------------------

describe('stageMountedIndices', () => {
  it('mounts nothing when there is nothing to show', () => {
    expect(stageMountedIndices(0, 0)).toEqual([])
  })

  it('mounts the whole set while it is smaller than the window', () => {
    // Nothing is saved below four images, and nothing should be broken either.
    expect(stageMountedIndices(1, 0)).toEqual([0])
    expect(stageMountedIndices(2, 0)).toEqual([0, 1])
    expect(stageMountedIndices(2, 1)).toEqual([0, 1])
    expect(stageMountedIndices(3, 1)).toEqual([0, 1, 2])
  })

  it('holds the active image and one neighbour on each side', () => {
    // Both neighbours, because every automatic move is +/-1 and a cross-fade
    // needs its destination already in the DOM to fade *into*.
    expect(stageMountedIndices(6, 3)).toEqual([2, 3, 4])
    expect(stageMountedIndices(9, 5)).toEqual([4, 5, 6])
  })

  it('wraps the window around the ends rather than truncating it', () => {
    // A naive `slice(active - 1, active + 2)` gives [0, 1] here and breaks the
    // fade on the rotation's wrap-around step.
    expect(stageMountedIndices(6, 0)).toEqual([0, 1, 5])
    expect(stageMountedIndices(6, 5)).toEqual([0, 4, 5])
  })

  it('does not grow with the character\'s image count', () => {
    // The whole point: 39 images cost the same as 3.
    for (const total of [6, 12, 40]) {
      expect(stageMountedIndices(total, 2)).toHaveLength(3)
    }
  })

  it('keeps a retained index that has fallen outside the window', () => {
    // After a dot jump the outgoing image is nowhere near the new window, and
    // unmounting it mid-transition would replace the 1.2 s fade with a cut.
    expect(stageMountedIndices(10, 6, [1])).toEqual([1, 5, 6, 7])
  })

  it('stays bounded however the retained indices land', () => {
    // Three window slots plus two single retention slots. Five only happens
    // across the two frames of a dot jump that follows another dot jump; the
    // steady state after any move is four, and neither depends on `total`.
    expect(stageMountedIndices(20, 10, [1, 18])).toHaveLength(5)
    expect(stageMountedIndices(20, 10, [1, 1])).toHaveLength(4)
    expect(stageMountedIndices(20, 10, [9, 11])).toHaveLength(3)
  })

  it('ignores retained indices that no longer address an image', () => {
    // Switching character shortens the array under a stale index; a `src` of
    // `undefined` renders an image that fetches the SPA's own HTML.
    expect(stageMountedIndices(6, 3, [null, -1, 99, 1.5])).toEqual([2, 3, 4])
  })

  it('returns ascending indices, because document order is stacking order', () => {
    // The images are absolutely stacked; which one paints over the other
    // during a fade is decided by document order alone.
    const indices = stageMountedIndices(10, 6, [1])
    expect([...indices].sort((a, b) => a - b)).toEqual(indices)
  })
})

// ----------------------------------------------------------------------
// The rendered stage
// ----------------------------------------------------------------------

describe('ImageStage (SSR render)', () => {
  it('mounts only the window, not the whole album', async () => {
    // This is the 673 MB regression in one assertion: six mounted images at
    // 1024x1536x4 bytes is 36 MB resident to display one of them.
    const html = await render({ character: character(6) })
    expect(imgTags(html)).toHaveLength(3)
    expect(renderedSrcs(html)).toEqual([
      '/v1/public/characters/c1/p0.png?v=full',
      '/v1/public/characters/c1/p1.png?v=full',
      '/v1/public/characters/c1/p5.png?v=full',
    ])
  })

  it('holds the mounted count flat as the character accumulates images', async () => {
    for (const count of [4, 12, 39]) {
      expect(imgTags(await render({ character: character(count) }))).toHaveLength(3)
    }
  })

  it('asks for the full variant — same pixels, a tenth of the bytes', async () => {
    // The stage is the one place that must not be downscaled: it is
    // full-screen and the source is already being scaled *up*. `full` is the
    // same 1024x1536 re-encoded as WebP, 2313 KB -> 218 KB.
    const html = await render({ character: character(6) })
    for (const src of renderedSrcs(html)) {
      expect(src).toContain('?v=full')
    }
    expect(html).not.toContain('v=w320')
    expect(html).not.toContain('v=w768')
    expect(html).not.toContain('srcset')
  })

  it('keeps the attributes the stage carried before, and adds async decode', async () => {
    const html = await render({ character: character(6) })
    for (const tag of imgTags(html)) {
      expect(tag).toContain('stage-image')
      expect(tag).toContain('draggable="false"')
      expect(tag).toContain('alt="芊璃"')
      expect(tag).toContain('decoding="async"')
      // The stage image is the thing being looked at — it was never lazy.
      expect(tag).toContain('loading="eager"')
      expect(tag).not.toContain('loading="lazy"')
    }
  })

  it('marks exactly one image active — the others are the fade partners', async () => {
    const html = await render({ character: character(6) })
    const active = imgTags(html).filter((tag) => /class="[^"]*\bactive\b/.test(tag))
    expect(active).toHaveLength(1)
    expect(active[0]).toContain('/v1/public/characters/c1/p0.png?v=full')
  })

  it('still indicates every image, not just the mounted ones', async () => {
    // The dots are the map of the whole set; windowing the DOM must not
    // silently shrink the carousel from the player's point of view.
    const html = await render({ character: character(6) })
    // `\b` keeps the `stage-dots` container out of the count.
    expect((html.match(/class="stage-dot\b/g) ?? []).length).toBe(6)
    expect(html).toContain(L.dot.replace('{n}', '6'))
  })

  it('renders both arrows whenever there is somewhere to go', async () => {
    const html = await render({ character: character(6) })
    expect(html).toContain(L.prev)
    expect(html).toContain(L.next)
  })

  it('offers no navigation for a single image', async () => {
    const html = await render({ character: character(1) })
    expect(imgTags(html)).toHaveLength(1)
    expect(html).not.toContain('stage-dot')
    expect(html).not.toContain(L.prev)
  })

  it('says so when the character has no images at all', async () => {
    const html = await render({ character: character(0) })
    expect(imgTags(html)).toHaveLength(0)
    expect(html).toContain(L.noImages.replace('{name}', '芊璃'))
    expect(html).toContain(L.placeholderHint)
  })

  it('says so when no character is selected', async () => {
    const html = await render({ character: null })
    expect(imgTags(html)).toHaveLength(0)
    expect(html).toContain(L.noCharacter)
  })
})
