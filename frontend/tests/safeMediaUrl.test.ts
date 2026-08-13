import { describe, it, expect } from 'vitest'

import { isSafeMediaUrl, safeMediaHref } from '@/utils/safeMediaUrl'

describe('safeMediaUrl', () => {
  it('keeps http/https and relative URLs', () => {
    for (const url of [
      'https://cdn.example/img/a.png',
      'http://cdn.example/img/a.png',
      '/relative/path.png',
      'images/a.png',
      '/a/b:c/d.png', // ':' inside a path segment, not a scheme
    ]) {
      expect(isSafeMediaUrl(url)).toBe(true)
      expect(safeMediaHref(url)).toBe(url)
    }
  })

  it('drops javascript: / data: / vbscript: and other schemes', () => {
    for (const url of [
      'javascript:alert(document.cookie)',
      'JavaScript:alert(1)',
      '  javascript:alert(1)',
      'java\tscript:alert(1)',
      'java\nscript:alert(1)',
      'data:text/html,<script>alert(1)</script>',
      'vbscript:msgbox(1)',
      'file:///etc/passwd',
    ]) {
      expect(isSafeMediaUrl(url)).toBe(false)
      expect(safeMediaHref(url)).toBe('')
    }
  })

  it('treats non-strings and empty as unsafe', () => {
    expect(safeMediaHref('')).toBe('')
    expect(safeMediaHref(null)).toBe('')
    expect(safeMediaHref(undefined)).toBe('')
    expect(safeMediaHref(42)).toBe('')
  })
})
