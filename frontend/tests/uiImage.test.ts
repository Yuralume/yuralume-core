/**
 * `UiImage` — the one component allowed to decide how big an image is asked
 * for (IMAGE_DELIVERY_AND_PAGINATION_PLAN, D5 / ticket IV4).
 *
 * What is pinned here is the attribute contract, because that contract is what
 * the three follow-up tickets (IV5-A/B/C) build on and what the measured
 * 673 MB regression came from: 22 hand-rolled `<img>` tags, zero `srcset`,
 * zero `decoding`, zero placeholder dimensions.
 *
 * Rendering is SSR (`createSSRApp` + `renderToString`) — this repo has no DOM
 * test infra on purpose (no jsdom, no @vue/test-utils), so what is observable
 * is the emitted markup. That is exactly the surface under test; the size
 * decision happens in `setup()` and lands in attributes.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'

import UiImage, {
  imageSrcset,
  imageVariantUrl,
  supportsSizeVariants,
} from '@/components/ui/UiImage.vue'

const URL_UNDER_TEST = '/v1/public/characters/abc/portrait.png'

async function render(props: Record<string, unknown> = {}): Promise<string> {
  const app = createSSRApp(UiImage, { src: URL_UNDER_TEST, ...props })
  return renderToString(app)
}

/** Renders through a parent, which is the only way to observe attribute fall-through. */
async function renderIn(template: string): Promise<string> {
  return renderToString(createSSRApp({ components: { UiImage }, template }))
}

// ----------------------------------------------------------------------
// The pure URL derivation (D1: variant keys must be string-derivable)
// ----------------------------------------------------------------------

describe('imageVariantUrl', () => {
  it('appends the variant label the delivery route reads', () => {
    expect(imageVariantUrl(URL_UNDER_TEST, 'w320'))
      .toBe(`${URL_UNDER_TEST}?v=w320`)
    expect(imageVariantUrl(URL_UNDER_TEST, 'full'))
      .toBe(`${URL_UNDER_TEST}?v=full`)
  })

  it('joins onto an existing query instead of starting a second one', () => {
    // Storage URLs are not guaranteed to be bare paths; a second `?` would
    // make the whole thing an un-routable key.
    expect(imageVariantUrl('/v1/public/a.png?t=1', 'w768'))
      .toBe('/v1/public/a.png?t=1&v=w768')
  })

  it('keeps the fragment behind the query', () => {
    expect(imageVariantUrl('/v1/public/a.png#frag', 'w320'))
      .toBe('/v1/public/a.png?v=w320#frag')
  })

  it('leaves already-local bytes alone', () => {
    // A pending upload preview is a blob the browser already holds. There is
    // no server in the loop to serve it at another size, and a query string
    // on a blob URL is simply a broken URL.
    const blob = 'blob:http://localhost:5174/9f2b'
    const data = 'data:image/png;base64,iVBOR'
    expect(imageVariantUrl(blob, 'w320')).toBe(blob)
    expect(imageVariantUrl(data, 'w320')).toBe(data)
    expect(supportsSizeVariants(blob)).toBe(false)
    expect(supportsSizeVariants(data)).toBe(false)
    expect(supportsSizeVariants(URL_UNDER_TEST)).toBe(true)
  })

  it('does not invent a URL out of nothing', () => {
    expect(imageVariantUrl('', 'w320')).toBe('')
    expect(imageVariantUrl(null, 'w320')).toBe('')
    expect(imageSrcset('')).toBeUndefined()
  })
})

describe('the ui/ barrel', () => {
  it('exports the component and its URL helpers', async () => {
    // IV5-A/B/C import from `@/components/ui`, not from the SFC path. The
    // helpers live in a plain `<script>` block precisely so they survive
    // re-export; if that ever stops working these come back undefined and the
    // conversions fail at runtime, not at build time.
    const barrel = await import('@/components/ui')
    expect(barrel.UiImage).toBe(UiImage)
    expect(barrel.imageVariantUrl('/v1/public/a.png', 'w768'))
      .toBe('/v1/public/a.png?v=w768')
    expect(barrel.imageSrcset('/v1/public/a.png')).toContain('320w')
    expect(barrel.supportsSizeVariants('/v1/public/a.png')).toBe(true)
    expect(barrel.UI_IMAGE_VARIANT_QUERY).toBe('v')
    expect(barrel.uiImageVariantPlan('full').loading).toBe('eager')
  })
})

describe('imageSrcset', () => {
  it('offers exactly the two widths the frontend can name', () => {
    // A `w` descriptor has to state a real pixel width. `full` has no known
    // width — the frontend never learns the original's dimensions — so it is
    // deliberately absent from the candidate list rather than guessed at.
    expect(imageSrcset(URL_UNDER_TEST)).toBe(
      `${URL_UNDER_TEST}?v=w320 320w, ${URL_UNDER_TEST}?v=w768 768w`,
    )
    expect(imageSrcset(URL_UNDER_TEST)).not.toContain('v=full')
  })
})

// ----------------------------------------------------------------------
// The rendered attribute contract, per variant (D5's table)
// ----------------------------------------------------------------------

describe('UiImage (SSR render) — every variant', () => {
  it.each(['avatar', 'thumb', 'content', 'full'] as const)(
    '%s decodes off the main thread',
    async (variant) => {
      expect(await render({ variant })).toContain('decoding="async"')
    },
  )

  it.each(['avatar', 'thumb', 'content', 'full'] as const)(
    '%s reserves a placeholder box',
    async (variant) => {
      // The precondition for IV5-C: without a reserved box, dropping `src` on
      // an off-screen image collapses its height and the 39059 px scroll
      // position jumps.
      const html = await render({ variant })
      const hasIntrinsicBox = /width="\d+"/.test(html) && /height="\d+"/.test(html)
      const hasRatio = html.includes('aspect-ratio')
      expect(hasIntrinsicBox || hasRatio).toBe(true)
    },
  )
})

describe('UiImage (SSR render) — avatar', () => {
  it('asks for the smallest variant and nothing else', async () => {
    const html = await render({ variant: 'avatar' })
    expect(html).toContain(`src="${URL_UNDER_TEST}?v=w320"`)
    // A 40x40 avatar has no use for a candidate list; one small file is the
    // whole answer.
    expect(html).not.toContain('srcset')
    expect(html).not.toContain('sizes')
  })

  it('defers to the viewport and carries fixed dimensions', async () => {
    const html = await render({ variant: 'avatar' })
    expect(html).toContain('loading="lazy"')
    expect(html).toContain('width="40"')
    expect(html).toContain('height="40"')
  })

  it('lets the caller state its own avatar size', async () => {
    const html = await render({ variant: 'avatar', width: 64, height: 64 })
    expect(html).toContain('width="64"')
    expect(html).toContain('height="64"')
  })
})

describe('UiImage (SSR render) — thumb and content', () => {
  it.each(['thumb', 'content'] as const)(
    '%s offers both known widths and falls back to the small one',
    async (variant) => {
      const html = await render({ variant })
      expect(html).toContain(`${URL_UNDER_TEST}?v=w320 320w`)
      expect(html).toContain(`${URL_UNDER_TEST}?v=w768 768w`)
      // `src` is what a browser without srcset support takes, and what the
      // candidate list is resolved against — the cheap one, not the original.
      expect(html).toContain(`src="${URL_UNDER_TEST}?v=w320"`)
      expect(html).toContain('loading="lazy"')
    },
  )

  it('uses the sizes the caller states', async () => {
    const html = await render({ variant: 'thumb', sizes: '(max-width: 600px) 45vw, 140px' })
    expect(html).toContain('sizes="(max-width: 600px) 45vw, 140px"')
  })

  it('never ships a candidate list without a sizes hint', async () => {
    // Omitting `sizes` makes the browser assume `100vw`, which picks w768 for
    // a 140px album cell — the exact waste this component exists to stop. So
    // an omission falls back to the variant default instead of nothing.
    const html = await render({ variant: 'content' })
    expect(html).toContain('srcset')
    expect(html).toMatch(/sizes="[^"]+"/)
  })

  it('drops the candidate list for bytes the browser already holds', async () => {
    const html = await render({ variant: 'content', src: 'blob:http://localhost:5174/9f2b' })
    expect(html).toContain('src="blob:http://localhost:5174/9f2b"')
    expect(html).not.toContain('srcset')
  })
})

describe('UiImage (SSR render) — full', () => {
  it('takes the original pixels as a single source', async () => {
    const html = await render({ variant: 'full' })
    expect(html).toContain(`src="${URL_UNDER_TEST}?v=full"`)
    expect(html).not.toContain('srcset')
    expect(html).not.toContain('sizes')
  })

  it('loads eagerly — the stage image is the thing being looked at', async () => {
    const html = await render({ variant: 'full' })
    expect(html).toContain('loading="eager"')
    expect(html).not.toContain('loading="lazy"')
  })
})

describe('UiImage (SSR render) — no URL', () => {
  it('emits no src at all rather than an empty one', async () => {
    // `src=""` resolves to the current document URL: the browser would fetch
    // the SPA's own HTML and feed it to an image decoder. IV5-C releases
    // decoded bitmaps by dropping the URL, so this is a normal state.
    const html = await render({ src: '' })
    expect(html).not.toContain('src=')
    expect(html).not.toContain('srcset')
  })

  it('still reserves the box while the URL is gone', async () => {
    // This is the whole point of the placeholder: an image released off-screen
    // must not collapse the column it was sitting in.
    expect(await render({ src: '' })).toContain('aspect-ratio')
    expect(await render({ src: '', variant: 'avatar' })).toContain('width="40"')
  })
})

describe('UiImage (SSR render) — caller overrides', () => {
  it('takes an explicit ratio over the variant default', async () => {
    const html = await render({ variant: 'content', aspectRatio: '16 / 9' })
    expect(html).toContain('aspect-ratio:16 / 9')
  })

  it('resolves the ratio through the kebab-cased attribute too', async () => {
    // The IV12 conversions write `aspect-ratio="3 / 4"` in their templates,
    // matching the window the card art actually sits in. If that ever stopped
    // resolving to the prop it would land as an inert HTML attribute and the
    // placeholder would silently revert to the variant's 2/3 default — a
    // wrong-shaped box that only shows up while the bytes are in flight.
    expect(await renderIn('<UiImage src="/v1/public/a.png" aspect-ratio="3 / 4" />'))
      .toContain('aspect-ratio:3 / 4')
  })

  it('lets the caller force eager on a lazy variant', async () => {
    expect(await render({ variant: 'thumb', loading: 'eager' }))
      .toContain('loading="eager"')
  })

  it('passes alt text through and defaults it to decorative', async () => {
    expect(await render({ alt: '芊璃的畫像' })).toContain('alt="芊璃的畫像"')
    // An empty value serialises as the bare attribute, which is what a
    // decorative image needs — a *missing* alt is the accessibility failure.
    expect(await render()).toMatch(/\salt(=""|\s|>)/)
  })

  it('leaves draggable alone unless the caller sets it', async () => {
    // Vue casts an absent Boolean *prop* to `false`, so declaring `draggable`
    // would have stamped `draggable="false"` on every image in the product and
    // taken drag-to-save away from the album and the chat. It stays an
    // ordinary fall-through attribute instead.
    expect(await render({ variant: 'full' })).not.toContain('draggable')
    expect(await renderIn('<UiImage src="/v1/public/a.png" variant="full" :draggable="false" />'))
      .toContain('draggable="false"')
  })

  it('keeps its own hook class alongside whatever the caller styles with', async () => {
    const html = await renderIn('<UiImage src="/v1/public/a.png" class="bubble-image" />')
    expect(html).toContain('ui-image')
    expect(html).toContain('bubble-image')
  })
})

// ----------------------------------------------------------------------
// The hot spots
// ----------------------------------------------------------------------

/**
 * Every file here renders an image straight off object storage, and every one
 * of them once asked for the 1024x1536 original: the 40x40 sidebar avatar
 * (983x the pixels it shows), the character grid, the album grid, the chat
 * bubble, and the stage — which mounts *every* URL in `image_urls` and hides
 * all but one with `opacity: 0`.
 *
 * Live since IV5-C: all three conversions (IV5-A, IV5-B, IV5-C) have landed, so
 * this is now the guard that stops the next hand-rolled `<img>` from
 * re-introducing the 673 MB.
 *
 * IV12 added the second half of the list — the surfaces the IV5 sweep missed
 * because they are not on the stage: the LumeGram card (post picture and its
 * account avatar), the two collectible-card faces, and the fusion roster,
 * which did not even carry `loading="lazy"`.
 *
 * OC6-IV added `FusionStoryPage.vue`'s shelf cover — the character's
 * `image_urls[0]` (the same 1024x1536 original) rendered at 44x58 in a list
 * of stories, one more spot IV5 never reached because it isn't on the stage
 * either.
 *
 * What is *not* here, and why, is as load-bearing as what is. Bundled brand
 * marks (`/logo-mark.png`, `/LumeGramLogo.png` in `SidebarBrand`,
 * `LoginPage`, `SetupPage`, `StagePage`) are build assets with no object key
 * and no variant to ask for. Upload previews (`ChatPanel`,
 * `CharacterCreateModal`) are `blob:`/`data:` bytes the browser already
 * holds — `UiImage` would pass them through untouched, so converting them
 * buys nothing. And `ChannelBindingsPanel`'s WhatsApp QR is an SVG from the
 * messaging API whose cache-buster is *literally* `?v=<n>`; routing it
 * through the component would have two different meanings fighting over one
 * query parameter.
 *
 * **And one exclusion that is a limitation rather than a decision: this scan
 * only sees `<img>`.** A CSS `background-image` paints the same original
 * bytes with no element for `/<img[\s/>]/` to match and no component for
 * `UiImage` to be, so a 1024x1536 portrait behind a 32px avatar is invisible
 * to every assertion in this file — which is exactly how `MemoirPage.vue` and
 * `MemoirOverlay.vue` sat outside the IV5 sweep until OC6-IV. Those two now
 * derive their URL through the same `imageVariantUrl` helper and are pinned
 * in `memoirAvatarImage.test.ts`; adding a hot spot to the list below does
 * **not** cover a background-image sibling, so check for one when converting.
 */
const IMAGE_HOT_SPOTS = [
  'components/PlayerSidebar.vue',
  'components/CharacterImagesPanel.vue',
  'components/AlbumPanel.vue',
  'components/ChatBubble.vue',
  'components/ImageStage.vue',
  'components/FeedCard.vue',
  'components/CharacterCardThumb.vue',
  'components/CharacterCardFace.vue',
  'components/fusion-story/CharacterMultiSelect.vue',
  'pages/FusionStoryPage.vue',
]

function source(file: string): string {
  return readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
}

/**
 * Only the markup counts. `ImageStage.vue` carries the string `<img>` inside a
 * CSS comment explaining its `user-select` rule — scanning the whole file
 * would keep this red after the conversion lands, for a reason that has
 * nothing to do with images.
 */
function templateMarkup(file: string): string {
  const src = source(file)
  const open = src.indexOf('<template>')
  const close = src.lastIndexOf('</template>')
  const markup = open === -1 || close === -1 ? src : src.slice(open, close)
  return markup.replace(/<!--[\s\S]*?-->/g, '')
}

describe('image hot spots go through UiImage', () => {
  it.each(IMAGE_HOT_SPOTS)('%s hand-rolls no <img>', (file) => {
    const markup = templateMarkup(file)
    expect(markup).not.toMatch(/<img[\s/>]/)
    expect(markup).toContain('<UiImage')
  })
})

// ----------------------------------------------------------------------
// OC6g — an official character card's image_url is an *absolute* Cloud URL
// (the local `/character-cards/{pack}/images/{n}` route only ever serves
// local packs), routed through the same `CharacterCardFace` art window as a
// local card. `supportsSizeVariants` only excludes `data:`/`blob:`, so this
// pins that an absolute `https://` origin gets the same `?v=` treatment as
// every other object-storage image — the cross-repo half of the contract
// `officialCardCatalogApi.test.ts` pins from the token-access side.
// ----------------------------------------------------------------------

describe('UiImage — absolute Cloud catalog image URLs (OC6g)', () => {
  const CLOUD_URL = 'https://app.yuralume.com/api/user/v1/public/official-cards/abc123/images/0'

  it('treats an absolute Cloud URL as variant-capable, same as a relative one', () => {
    expect(supportsSizeVariants(CLOUD_URL)).toBe(true)
    expect(imageVariantUrl(CLOUD_URL, 'w320')).toBe(`${CLOUD_URL}?v=w320`)
  })

  it('renders the same srcset/sizes contract as every other object-storage image', async () => {
    const html = await render({ variant: 'content', src: CLOUD_URL })
    expect(html).toContain(`${CLOUD_URL}?v=w320 320w`)
    expect(html).toContain(`${CLOUD_URL}?v=w768 768w`)
    expect(html).toContain(`src="${CLOUD_URL}?v=w320"`)
  })
})
