/**
 * Markdown → HTML rendering for the Admin Developer Docs section.
 *
 * Source markdown is our own shipped repo content (see `devDocs.ts`), never
 * user input, so the XSS surface is minimal — but we still render with
 * `html:false` (the markdown-it default) so raw HTML in the source can't
 * leak through the `v-html` mount point in `DevDocsAdminPage.vue`.
 */
import MarkdownIt from 'markdown-it'

let renderer: MarkdownIt | null = null

function getRenderer(): MarkdownIt {
  if (!renderer) {
    renderer = new MarkdownIt({ html: false, linkify: true })
  }
  return renderer
}

export function renderDevDocMarkdown(source: string): string {
  return getRenderer().render(source)
}
