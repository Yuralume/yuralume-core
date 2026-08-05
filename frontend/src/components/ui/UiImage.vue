<script lang="ts">
/**
 * The one place that knows how to ask the server for a right-sized image.
 *
 * Callers state a *purpose* (avatar / thumb / content / full); this component
 * turns that into `src`, `srcset`, `sizes`, `loading`, `decoding` and a
 * placeholder box. Before it existed, 22 hand-rolled `<img>` tags each asked
 * for the 1024x1536 original — a 40x40 sidebar avatar was pulling 983x the
 * pixels it displayed, and every one of those decoded bitmaps costs ~6 MB of
 * resident memory for as long as it stays in the DOM.
 *
 * Delivery contract (IMAGE_DELIVERY_AND_PAGINATION_PLAN D2): the object route
 * takes `?v=w320|w768|full` and **falls back to the original** when the variant
 * does not exist. That is why this component can ship before the encoder and
 * the backfill do — on un-backfilled data every URL here resolves to exactly
 * what it resolves to today.
 *
 * Why `srcset` carries only two entries (D5): a `w` descriptor must state a
 * real pixel width, and the frontend never learns the original's dimensions.
 * `w320` and `w768` are known by construction; `full` is not, so `full` is
 * delivered as a lone `src` with no descriptor rather than a guessed one.
 *
 * The rendered root is the `<img>` itself, with **no wrapper** — callers style
 * these through descendant selectors (`.image-tile img`, `.album-image-link
 * img`), and a wrapper would silently break every one of them. It also keeps
 * the component a single root, so `class`, `style`, `draggable`, `title` and
 * listeners all fall through. Note that a comment node in the template would
 * turn the root into a fragment and kill that fall-through, which is why this
 * commentary lives here.
 *
 * `draggable` is deliberately *not* a declared prop: Vue casts an absent
 * Boolean prop to `false`, which would have stamped `draggable="false"` on
 * every image in the product and quietly taken drag-to-save away.
 */

/** What the image is *for*. Size strategy is derived from this, not passed in. */
export type UiImageVariant = 'avatar' | 'thumb' | 'content' | 'full'

/** The three variant labels the delivery route accepts (D1). */
export type UiImageSizeKey = 'w320' | 'w768' | 'full'

/** Query parameter the object route reads the variant label from (D2). */
export const UI_IMAGE_VARIANT_QUERY = 'v'

/**
 * Whether a URL can be re-asked for at a different size.
 *
 * `data:` and `blob:` are bytes the browser already holds (local upload
 * previews, canvas output) — there is no server in the loop to serve a
 * variant, and appending a query string to them produces a broken URL.
 */
export function supportsSizeVariants(src: string | null | undefined): boolean {
  if (!src) return false
  return !/^(data|blob):/i.test(src)
}

/**
 * Derive the variant URL for `src`. Purely string work — the same property the
 * backend key derivation relies on (D1).
 *
 * Preserves an existing query string and fragment, because album and storage
 * URLs are not guaranteed to be bare paths.
 */
export function imageVariantUrl(
  src: string | null | undefined,
  key: UiImageSizeKey,
): string {
  if (!src) return ''
  if (!supportsSizeVariants(src)) return src
  const hashAt = src.indexOf('#')
  const base = hashAt === -1 ? src : src.slice(0, hashAt)
  const hash = hashAt === -1 ? '' : src.slice(hashAt)
  const separator = base.includes('?') ? '&' : '?'
  return `${base}${separator}${UI_IMAGE_VARIANT_QUERY}=${key}${hash}`
}

/**
 * The two-entry candidate list. Returns `undefined` when the URL cannot carry
 * variants at all, so the caller emits no `srcset` rather than an empty one.
 */
export function imageSrcset(src: string | null | undefined): string | undefined {
  if (!src || !supportsSizeVariants(src)) return undefined
  return `${imageVariantUrl(src, 'w320')} 320w, ${imageVariantUrl(src, 'w768')} 768w`
}

interface VariantPlan {
  /** Non-null => single `src` at this size, no `srcset`. */
  single: UiImageSizeKey | null
  loading: 'lazy' | 'eager'
  /** Fallback `sizes` when the caller gives none; only used alongside a srcset. */
  sizes: string | null
  /** Placeholder ratio when the caller states neither width+height nor a ratio. */
  aspectRatio: string
  width: number | null
  height: number | null
}

/**
 * D5's table, as data. `2 / 3` is the ratio this product actually generates
 * (1024x1536); avatars are square by construction.
 */
const VARIANT_PLANS: Record<UiImageVariant, VariantPlan> = {
  avatar: {
    single: 'w320',
    loading: 'lazy',
    sizes: null,
    aspectRatio: '1 / 1',
    width: 40,
    height: 40,
  },
  thumb: {
    single: null,
    loading: 'lazy',
    sizes: '160px',
    aspectRatio: '2 / 3',
    width: null,
    height: null,
  },
  content: {
    single: null,
    loading: 'lazy',
    sizes: '(max-width: 640px) 90vw, 320px',
    aspectRatio: '2 / 3',
    width: null,
    height: null,
  },
  full: {
    single: 'full',
    loading: 'eager',
    sizes: null,
    aspectRatio: '2 / 3',
    width: null,
    height: null,
  },
}

export function uiImageVariantPlan(variant: UiImageVariant): VariantPlan {
  return VARIANT_PLANS[variant]
}
</script>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    src?: string | null
    alt?: string
    variant?: UiImageVariant
    /** `sizes` for the srcset. Ignored by `avatar` / `full`, which carry no srcset. */
    sizes?: string
    /** Placeholder width; with `height` it becomes the intrinsic box. */
    width?: number | string
    /** Placeholder height; with `width` it becomes the intrinsic box. */
    height?: number | string
    /** Placeholder ratio, e.g. `'16 / 9'`. Wins over the variant default. */
    aspectRatio?: string | number
    /** Override the variant's loading strategy. Rarely needed. */
    loading?: 'lazy' | 'eager'
  }>(),
  {
    src: '',
    alt: '',
    variant: 'thumb',
  },
)

const emit = defineEmits<{
  error: [event: Event]
  load: [event: Event]
}>()

const plan = computed(() => VARIANT_PLANS[props.variant])

/**
 * Absent when there is no URL, rather than `src=""` — an empty `src` resolves
 * to the current document URL, so the browser would fetch the SPA's own HTML
 * and hand it to an image decoder. IV5-C releases decoded bitmaps by dropping
 * the URL, which makes this the normal path, not an edge case.
 */
const resolvedSrc = computed(() => {
  const url = imageVariantUrl(props.src, plan.value.single ?? 'w320')
  return url === '' ? undefined : url
})

const resolvedSrcset = computed(() =>
  plan.value.single === null ? imageSrcset(props.src) : undefined,
)

/**
 * `sizes` without a `srcset` is inert, and a `srcset` without `sizes` makes the
 * browser assume `100vw` — which would pick w768 for a 140px album cell. So it
 * is emitted exactly when there is a srcset, and never left empty.
 */
const resolvedSizes = computed(() =>
  resolvedSrcset.value ? (props.sizes ?? plan.value.sizes ?? undefined) : undefined,
)

const resolvedLoading = computed(() => props.loading ?? plan.value.loading)

const resolvedWidth = computed(() => props.width ?? plan.value.width ?? undefined)
const resolvedHeight = computed(() => props.height ?? plan.value.height ?? undefined)

/**
 * A placeholder box is emitted unconditionally — it is the precondition for
 * off-screen release (D6/IV5-C). Without it, dropping `src` collapses the
 * element to zero height and the scroll position jumps.
 *
 * `aspect-ratio` is inert once CSS pins both dimensions, so this cannot fight
 * a caller's existing layout; it only fills in the dimension CSS leaves `auto`.
 */
const placeholderStyle = computed(() => {
  if (props.aspectRatio !== undefined) {
    return { aspectRatio: String(props.aspectRatio) }
  }
  if (resolvedWidth.value !== undefined && resolvedHeight.value !== undefined) {
    return undefined
  }
  return { aspectRatio: plan.value.aspectRatio }
})

/**
 * A URL that 404s renders a broken-image glyph, which reads as a product
 * defect. The existing hand-rolled avatars answered this by setting
 * `style.display = 'none'` from an inline `@error`; this keeps that outcome
 * and re-emits the event so callers can swap in their own fallback.
 */
const failed = ref(false)

watch(
  () => props.src,
  () => {
    failed.value = false
  },
)

function onError(event: Event) {
  failed.value = true
  emit('error', event)
}
</script>

<template>
  <img
    :class="['ui-image', { 'ui-image--failed': failed }]"
    :src="resolvedSrc"
    :srcset="resolvedSrcset"
    :sizes="resolvedSizes"
    :alt="alt"
    :width="resolvedWidth"
    :height="resolvedHeight"
    :loading="resolvedLoading"
    :style="placeholderStyle"
    decoding="async"
    @error="onError"
    @load="emit('load', $event)"
  />
</template>

<style scoped>
/* Behaviour, not base visuals: the element keeps whatever the caller's layout
   gives it and only disappears when its bytes never arrived. */
.ui-image--failed {
  display: none;
}
</style>
