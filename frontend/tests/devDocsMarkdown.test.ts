import { describe, expect, it } from 'vitest'

import { renderDevDocMarkdown } from '@/utils/devDocsMarkdown'

describe('dev docs markdown rendering', () => {
  it('renders headings and paragraphs to HTML', () => {
    const html = renderDevDocMarkdown('# Title\n\nSome **bold** text.')
    expect(html).toContain('<h1>Title</h1>')
    expect(html).toContain('<strong>bold</strong>')
  })

  it('renders fenced code blocks', () => {
    const html = renderDevDocMarkdown('```python\nprint("hi")\n```')
    expect(html).toContain('<pre>')
    expect(html).toContain('<code')
  })

  it('linkifies bare URLs (linkify: true)', () => {
    const html = renderDevDocMarkdown('See http://127.0.0.1:8188 for details.')
    expect(html).toContain('<a href="http://127.0.0.1:8188"')
  })

  it('does not pass through raw HTML (html: false)', () => {
    const html = renderDevDocMarkdown('<script>alert(1)</script>')
    expect(html).not.toContain('<script>')
  })
})
