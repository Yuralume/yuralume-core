import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { nextActiveTool, toolActivityDisplay } from '@/utils/toolActivity'

describe('toolActivityDisplay', () => {
  it('maps the three shipped tools to diegetic label keys', () => {
    expect(toolActivityDisplay('generate_image').labelKey)
      .toBe('chat.toolActivity.generateImage')
    expect(toolActivityDisplay('web_search').labelKey)
      .toBe('chat.toolActivity.webSearch')
    expect(toolActivityDisplay('web_fetch').labelKey)
      .toBe('chat.toolActivity.webFetch')
  })

  it('falls back to the generic line for unknown tools instead of leaking names', () => {
    expect(toolActivityDisplay('some_future_tool').labelKey)
      .toBe('chat.toolActivity.generic')
  })

  it('every label key it can produce exists in all three locales', () => {
    const keys = ['generate_image', 'web_search', 'web_fetch', 'anything_else']
      .map((tool) => toolActivityDisplay(tool).labelKey)
    for (const locale of ['zh-TW', 'en-US', 'ja-JP']) {
      const source = readFileSync(
        resolve(__dirname, `../src/i18n/locales/${locale}.ts`),
        'utf-8',
      )
      expect(source).toContain('toolActivity: {')
      for (const key of keys) {
        const leaf = key.split('.').pop()!
        expect(source, `${locale} missing ${key}`).toMatch(
          new RegExp(`${leaf}: '`),
        )
      }
    }
  })
})

describe('nextActiveTool', () => {
  it('started sets, matching finished clears', () => {
    let state: string | null = null
    state = nextActiveTool(state, { tool: 'generate_image', status: 'started' })
    expect(state).toBe('generate_image')
    state = nextActiveTool(state, { tool: 'generate_image', status: 'finished' })
    expect(state).toBeNull()
  })

  it('a multi-hop chain switches to the newest tool', () => {
    let state: string | null = null
    state = nextActiveTool(state, { tool: 'web_search', status: 'started' })
    state = nextActiveTool(state, { tool: 'web_fetch', status: 'started' })
    expect(state).toBe('web_fetch')
  })

  it('a stale finish from an earlier tool does not blank a newer one', () => {
    let state: string | null = null
    state = nextActiveTool(state, { tool: 'web_search', status: 'started' })
    state = nextActiveTool(state, { tool: 'generate_image', status: 'started' })
    state = nextActiveTool(state, { tool: 'web_search', status: 'finished' })
    expect(state).toBe('generate_image')
  })

  it('unknown statuses and empty tool names leave the state untouched', () => {
    expect(nextActiveTool('web_search', { tool: 'web_search', status: 'paused' }))
      .toBe('web_search')
    expect(nextActiveTool('web_search', { tool: '', status: 'finished' }))
      .toBe('web_search')
  })
})

describe('ChatPanel wiring (source scan — SSR harness cannot click)', () => {
  const source = readFileSync(
    resolve(__dirname, '../src/components/ChatPanel.vue'),
    'utf-8',
  )

  it('feeds stream activity events through the reducer', () => {
    expect(source).toContain('nextActiveTool(activeToolName.value, activity)')
  })

  it('renders the indicator from the pure display mapping', () => {
    expect(source).toContain('toolActivityDisplay(activeToolName.value)')
    expect(source).toContain('class="tool-activity"')
  })

  it('clears the indicator when the turn ends, success or failure', () => {
    const finallyBlock = source.slice(source.indexOf('} finally {'))
    expect(finallyBlock).toContain('activeToolName.value = null')
  })

  it('the old always-on tool wait guess is gone', () => {
    expect(source).not.toContain('streamingHint')
    expect(source).not.toContain('tool-wait-hint')
  })
})
