/**
 * Admin → Developer Docs registry.
 *
 * Single source of truth per D9 (CUSTOM_MEDIA_GATEWAY_SPEC_AND_COMFYUI_PLAN.md):
 * doc content is `?raw`-imported straight from the repo's `docs/*.md` files at
 * build time, so the Admin page is always byte-identical to the shipped repo
 * document — no separate copy to keep in sync.
 *
 * Structure is a flat list (Q2 recommendation) with a reserved `category`
 * field so a future second/third doc can grow into grouped sections without
 * a registry rewrite. UI chrome (titles, hints) is i18n-keyed; the markdown
 * body itself stays English (D6) and is never translated.
 */
import customMediaGatewaySpec from '../../../docs/CUSTOM_MEDIA_GATEWAY_SPEC.md?raw'
import customTtsServerSpec from '../../../docs/CUSTOM_TTS_SERVER_SPEC.md?raw'

export interface DevDoc {
  slug: string
  /** i18n key resolving to the doc's display title in the docs list. */
  titleKey: string
  /** Raw English markdown source, byte-identical to the repo file. */
  source: string
  /** Reserved for future grouping once there are enough docs to need it. */
  category?: string
}

export const DEV_DOCS: readonly DevDoc[] = [
  {
    slug: 'custom-media-gateway',
    titleKey: 'admin.devDocs.customMediaGateway.title',
    source: customMediaGatewaySpec,
  },
  {
    slug: 'custom-tts-server',
    titleKey: 'admin.devDocs.customTtsServer.title',
    source: customTtsServerSpec,
  },
]

export function findDevDoc(slug: string | null | undefined): DevDoc | undefined {
  if (!slug) return undefined
  return DEV_DOCS.find(doc => doc.slug === slug)
}

/** Falls back to the first registered doc so `/admin/dev-docs` without a
 * `:slug` always has something to render. */
export function resolveDevDoc(slug: string | null | undefined): DevDoc | undefined {
  return findDevDoc(slug) ?? DEV_DOCS[0]
}
