import { describe, expect, it } from 'vitest'

import { DEV_DOCS, findDevDoc, resolveDevDoc } from '@/utils/devDocs'

describe('dev docs registry', () => {
  it('registers the Custom Media Gateway spec as the first doc', () => {
    expect(DEV_DOCS.length).toBeGreaterThan(0)
    expect(DEV_DOCS[0].slug).toBe('custom-media-gateway')
    expect(DEV_DOCS[0].titleKey).toBe('admin.devDocs.customMediaGateway.title')
  })

  it('bundles the real repo markdown byte-for-byte (single source of truth)', () => {
    const doc = findDevDoc('custom-media-gateway')
    expect(doc).toBeDefined()
    expect(doc!.source).toContain('Spec version: 1.0')
    expect(doc!.source).toContain('POST {base_url}/images/generations')
    expect(doc!.source).toContain('starter, not tuned')
  })

  it('registers the Custom TTS Server spec with its real repo markdown', () => {
    const doc = findDevDoc('custom-tts-server')
    expect(doc).toBeDefined()
    expect(doc!.titleKey).toBe('admin.devDocs.customTtsServer.title')
    expect(doc!.source).toContain('Custom TTS Server Specification')
    expect(doc!.source).toContain('POST {base_url}/tts/synthesize')
    expect(doc!.source).toContain('GET {base_url}/voices')
  })

  it('findDevDoc returns undefined for unknown or missing slugs', () => {
    expect(findDevDoc('does-not-exist')).toBeUndefined()
    expect(findDevDoc(null)).toBeUndefined()
    expect(findDevDoc(undefined)).toBeUndefined()
    expect(findDevDoc('')).toBeUndefined()
  })

  it('resolveDevDoc falls back to the first doc when slug is absent/unknown', () => {
    expect(resolveDevDoc(undefined)?.slug).toBe(DEV_DOCS[0].slug)
    expect(resolveDevDoc('nope')?.slug).toBe(DEV_DOCS[0].slug)
    expect(resolveDevDoc('custom-media-gateway')?.slug).toBe('custom-media-gateway')
  })
})
