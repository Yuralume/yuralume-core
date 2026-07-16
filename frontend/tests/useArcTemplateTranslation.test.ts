import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import { useArcTemplateTranslation } from '@/composables/useArcTemplateTranslation'
import type { ArcTemplate } from '@/types/arcTemplate'

// The composable imports the REST client (which imports axios) at module
// load; we inject `previewFn` so the real client is never called, but keep
// axios mocked so the import is inert in the node test environment.
vi.mock('axios', () => ({ default: { get: vi.fn(), post: vi.fn() } }))

/** Drain microtasks + the fire-and-forget translate pump. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

const noStore = { getItem: () => null, setItem: () => undefined }

function tpl(
  id: string,
  language: string,
  overrides: Partial<ArcTemplate> = {},
): ArcTemplate {
  return {
    id,
    title: `${id} title`,
    premise: `${id} premise`,
    theme: 'custom',
    tone: 'daily',
    language,
    duration_days: 14,
    beat_count: 1,
    applicability_scope: 'generic',
    target_character_ids: [],
    binding: { world_frames: [], required_traits: [] },
    beats: [
      {
        sequence: 0,
        day_offset: 0,
        title: `${id} beat`,
        summary: 'summary',
        tension: 'setup',
        scene_type: 'encounter',
        location: null,
        scene_characters: [],
        dramatic_question: null,
        required: true,
      },
    ],
    ...overrides,
  }
}

function translatedView(source: ArcTemplate, target: string): ArcTemplate {
  return { ...source, title: `[${target}] ${source.title}`, language: target }
}

function makePreview() {
  return vi.fn(async (id: string, target: string) => {
    return translatedView(tpl(id, 'zh-TW'), target)
  })
}

describe('useArcTemplateTranslation', () => {
  it('only translates cards whose authored language differs from the target', async () => {
    const templates = ref([tpl('a', 'zh-TW'), tpl('b', 'en-US')])
    const previewFn = makePreview()
    const c = useArcTemplateTranslation(templates, {
      targetLanguage: () => 'en-US',
      previewFn,
      storage: noStore,
      persistKey: 'k',
    })

    c.toggleTranslate()
    await flush()

    expect(previewFn).toHaveBeenCalledTimes(1)
    expect(previewFn).toHaveBeenCalledWith('a', 'en-US')
    // zh-TW card translated into en-US; already-en-US card left untouched.
    expect(c.displayTemplate(templates.value[0]).title).toBe('[en-US] a title')
    expect(c.displayTemplate(templates.value[1]).title).toBe('b title')
  })

  it('shows the authored prose when the toggle is off', () => {
    const templates = ref([tpl('a', 'zh-TW')])
    const c = useArcTemplateTranslation(templates, {
      targetLanguage: () => 'en-US',
      previewFn: makePreview(),
      storage: noStore,
      persistKey: 'k',
    })
    expect(c.translateEnabled.value).toBe(false)
    expect(c.displayTemplate(templates.value[0]).title).toBe('a title')
  })

  it('caches translations so re-enabling does not refetch', async () => {
    const templates = ref([tpl('a', 'zh-TW')])
    const previewFn = makePreview()
    const c = useArcTemplateTranslation(templates, {
      targetLanguage: () => 'en-US',
      previewFn,
      storage: noStore,
      persistKey: 'k',
    })

    c.toggleTranslate() // enable → fetch
    await flush()
    c.toggleTranslate() // disable
    await flush()
    c.toggleTranslate() // enable again → cache hit
    await flush()

    expect(previewFn).toHaveBeenCalledTimes(1)
  })

  it('keeps the authored prose on failure, marks it failed, and can retry', async () => {
    const templates = ref([tpl('a', 'zh-TW')])
    let fail = true
    const previewFn = vi.fn(async (id: string, target: string) => {
      if (fail) throw new Error('boom')
      return translatedView(tpl(id, 'zh-TW'), target)
    })
    const c = useArcTemplateTranslation(templates, {
      targetLanguage: () => 'en-US',
      previewFn,
      storage: noStore,
      persistKey: 'k',
    })

    c.toggleTranslate()
    await flush()
    // fail-soft: original prose, and the card is flagged failed (not cached).
    expect(c.displayTemplate(templates.value[0]).title).toBe('a title')
    expect(c.hasFailures.value).toBe(true)

    fail = false
    await c.retryFailed()
    await flush()

    expect(c.hasFailures.value).toBe(false)
    expect(c.displayTemplate(templates.value[0]).title).toBe('[en-US] a title')
  })

  it('refetches when the target language changes (cache key includes target)', async () => {
    const target = ref('en-US')
    const templates = ref([tpl('a', 'zh-TW')])
    const previewFn = makePreview()
    const c = useArcTemplateTranslation(templates, {
      targetLanguage: () => target.value,
      previewFn,
      storage: noStore,
      persistKey: 'k',
    })

    c.toggleTranslate()
    await flush()
    expect(previewFn).toHaveBeenCalledWith('a', 'en-US')

    target.value = 'ja-JP'
    await flush()

    expect(previewFn).toHaveBeenCalledWith('a', 'ja-JP')
    expect(previewFn).toHaveBeenCalledTimes(2)
    expect(c.displayTemplate(templates.value[0]).title).toBe('[ja-JP] a title')
  })

  it('invalidates a cached translation when the source prose changes (PATCH overwrite)', async () => {
    const templates = ref([tpl('a', 'zh-TW')])
    const previewFn = makePreview()
    const c = useArcTemplateTranslation(templates, {
      targetLanguage: () => 'en-US',
      previewFn,
      storage: noStore,
      persistKey: 'k',
    })

    c.toggleTranslate()
    await flush()
    expect(previewFn).toHaveBeenCalledTimes(1)

    // Same id, edited premise, list reloaded → stale cache must not be reused.
    templates.value = [tpl('a', 'zh-TW', { premise: 'edited premise' })]
    await flush()

    expect(previewFn).toHaveBeenCalledTimes(2)
  })

  it('restores the toggle from storage and translates on mount', async () => {
    const templates = ref([tpl('a', 'zh-TW')])
    const previewFn = makePreview()
    const store = {
      getItem: (key: string) => (key === 'persisted' ? '1' : null),
      setItem: vi.fn(),
    }
    const c = useArcTemplateTranslation(templates, {
      targetLanguage: () => 'en-US',
      previewFn,
      storage: store,
      persistKey: 'persisted',
    })

    expect(c.translateEnabled.value).toBe(true)
    await flush()
    expect(previewFn).toHaveBeenCalledTimes(1)
  })
})
