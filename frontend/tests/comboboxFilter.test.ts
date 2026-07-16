import { describe, expect, it } from 'vitest'

import { filterOptions } from '@/utils/comboboxFilter'

const MODELS = [
  'openrouter/anthropic/claude-opus-4.8',
  'openrouter/openai/gpt-5.5',
  'openrouter/openai/gpt-5.4-mini',
  'google/gemini-2.5-pro',
  'mistral-large-latest',
]

describe('filterOptions', () => {
  it('returns every option (copy) when the query is blank', () => {
    const result = filterOptions(MODELS, '   ')
    expect(result).toEqual(MODELS)
    expect(result).not.toBe(MODELS)
  })

  it('matches case-insensitive substrings anywhere in the option', () => {
    expect(filterOptions(MODELS, 'GPT-5.4')).toEqual([
      'openrouter/openai/gpt-5.4-mini',
    ])
  })

  it('requires every whitespace-separated token to be present (AND)', () => {
    expect(filterOptions(MODELS, 'openai gpt')).toEqual([
      'openrouter/openai/gpt-5.5',
      'openrouter/openai/gpt-5.4-mini',
    ])
    expect(filterOptions(MODELS, 'openai gemini')).toEqual([])
  })

  it('ranks options that start with the full query first, keeping input order otherwise', () => {
    expect(filterOptions(MODELS, 'mistral')).toEqual(['mistral-large-latest'])
    // "gemini" only appears mid-string, so it still matches via substring.
    expect(filterOptions(MODELS, 'gemini')).toEqual(['google/gemini-2.5-pro'])
  })
})
