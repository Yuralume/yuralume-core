import { describe, expect, it } from 'vitest'

import {
  MAX_CONTINUATION_SEED,
  MAX_MOMENT_SEED,
  MAX_SEED_PROMPT,
  clampSeedPrompt,
  composeContinuationSeed,
  composeMomentSeed,
  parseCastQuery,
  serializeCastQuery,
} from '@/utils/fusionSeed'

const strings = {
  recapLabel: 'RECAP',
  endingLabel: 'ENDING',
  instructionLabel: 'BRIEF',
  instruction: 'Continue from the ending.',
}

describe('composeContinuationSeed', () => {
  it('stitches recap, ending tail, and instruction into one seed', () => {
    const seed = composeContinuationSeed({
      title: 'The Old Bookstore',
      premise: 'Two strangers trapped by a storm.',
      endingText: 'And the rain finally stopped.',
      strings,
    })
    expect(seed).toContain('RECAP')
    expect(seed).toContain('The Old Bookstore')
    expect(seed).toContain('Two strangers trapped by a storm.')
    expect(seed).toContain('ENDING')
    expect(seed).toContain('And the rain finally stopped.')
    expect(seed).toContain('BRIEF')
    expect(seed).toContain('Continue from the ending.')
  })

  it('caps the seed at MAX_CONTINUATION_SEED and keeps the ending TAIL', () => {
    const endingText = `HEAD${'x'.repeat(4000)}TAILMARK`
    const seed = composeContinuationSeed({
      title: 'T',
      premise: 'P',
      endingText,
      strings,
    })
    expect(seed.length).toBeLessThanOrEqual(MAX_CONTINUATION_SEED)
    // The tail (what the reader just finished) survives; the head is dropped.
    expect(seed).toContain('TAILMARK')
    expect(seed).not.toContain('HEAD')
    // Instruction is part of the fixed scaffold and must remain.
    expect(seed).toContain('Continue from the ending.')
  })

  it('drops the ending block when there is no ending text', () => {
    const seed = composeContinuationSeed({
      title: 'T',
      premise: 'P',
      endingText: '',
      strings,
    })
    expect(seed).toContain('RECAP')
    expect(seed).toContain('BRIEF')
    expect(seed).not.toContain('ENDING')
  })

  it('never throws on all-empty input', () => {
    const seed = composeContinuationSeed({
      title: '',
      premise: '',
      endingText: '',
      strings: {
        recapLabel: '',
        endingLabel: '',
        instructionLabel: '',
        instruction: '',
      },
    })
    expect(typeof seed).toBe('string')
    expect(seed).toBe('')
  })

  it('coerces non-string prose fields without throwing', () => {
    const seed = composeContinuationSeed({
      // Simulate a malformed story object reaching the composer.
      title: undefined as unknown as string,
      premise: null as unknown as string,
      endingText: 42 as unknown as string,
      strings,
    })
    expect(seed).toContain('RECAP')
    expect(seed).toContain('BRIEF')
    expect(seed).not.toContain('42')
  })
})

const momentStrings = {
  momentLabel: 'MOMENT',
  instructionLabel: 'BRIEF',
  instruction: 'Write a side-story starting from this moment.',
}

describe('composeMomentSeed', () => {
  it('stitches the moment excerpt and the side-story instruction', () => {
    const seed = composeMomentSeed({
      momentText: 'They shared an umbrella under the first snow.',
      strings: momentStrings,
    })
    expect(seed).toContain('MOMENT')
    expect(seed).toContain('They shared an umbrella under the first snow.')
    expect(seed).toContain('BRIEF')
    expect(seed).toContain('Write a side-story starting from this moment.')
  })

  it('caps the seed at MAX_MOMENT_SEED and keeps the moment HEAD', () => {
    // A moment is the STARTING point, so the head survives and the tail
    // is dropped — the opposite of a continuation seed.
    const momentText = `HEADMARK${'x'.repeat(4000)}TAILMARK`
    const seed = composeMomentSeed({
      momentText,
      strings: momentStrings,
    })
    expect(seed.length).toBeLessThanOrEqual(MAX_MOMENT_SEED)
    expect(seed).toContain('HEADMARK')
    expect(seed).not.toContain('TAILMARK')
    // Instruction is part of the fixed scaffold and must remain.
    expect(seed).toContain('Write a side-story starting from this moment.')
  })

  it('drops the moment block when there is no moment text', () => {
    const seed = composeMomentSeed({
      momentText: '',
      strings: momentStrings,
    })
    expect(seed).not.toContain('MOMENT')
    expect(seed).toContain('BRIEF')
  })

  it('never throws on all-empty input', () => {
    const seed = composeMomentSeed({
      momentText: '',
      strings: { momentLabel: '', instructionLabel: '', instruction: '' },
    })
    expect(typeof seed).toBe('string')
    expect(seed).toBe('')
  })

  it('coerces a non-string moment without throwing', () => {
    const seed = composeMomentSeed({
      momentText: 42 as unknown as string,
      strings: momentStrings,
    })
    expect(seed).toContain('BRIEF')
    expect(seed).not.toContain('42')
  })

  it('stays within MAX_SEED_PROMPT after clampSeedPrompt', () => {
    const seed = composeMomentSeed({
      momentText: 'z'.repeat(5000),
      strings: momentStrings,
    })
    const clamped = clampSeedPrompt(seed)
    expect(clamped.length).toBeLessThanOrEqual(MAX_SEED_PROMPT)
    // The moment seed is already ≤ MAX_MOMENT_SEED (< MAX_SEED_PROMPT),
    // so clamping is a no-op passthrough here.
    expect(clamped).toBe(seed)
  })
})

describe('parseCastQuery', () => {
  const owned = ['a', 'b', 'c']

  it('filters to owned ids, preserving order', () => {
    expect(parseCastQuery('a,c', owned)).toEqual(['a', 'c'])
  })

  it('drops unknown ids the viewer does not own', () => {
    expect(parseCastQuery('a,z,c', owned)).toEqual(['a', 'c'])
  })

  it('de-duplicates and trims whitespace', () => {
    expect(parseCastQuery('a , a, b', owned)).toEqual(['a', 'b'])
  })

  it('returns [] for empty or non-string input', () => {
    expect(parseCastQuery('', owned)).toEqual([])
    expect(parseCastQuery(null, owned)).toEqual([])
    expect(parseCastQuery(undefined, owned)).toEqual([])
    expect(parseCastQuery(123, owned)).toEqual([])
    expect(parseCastQuery(['a'], owned)).toEqual([])
  })

  it('returns [] when nothing is owned', () => {
    expect(parseCastQuery('a,b', [])).toEqual([])
  })
})

describe('serializeCastQuery', () => {
  it('joins ids with commas', () => {
    expect(serializeCastQuery(['a', 'b'])).toBe('a,b')
  })

  it('drops falsy ids', () => {
    expect(serializeCastQuery(['a', '', 'b'])).toBe('a,b')
  })

  it('round-trips through parseCastQuery for owned ids', () => {
    const owned = ['a', 'b']
    expect(parseCastQuery(serializeCastQuery(['a', 'b']), owned)).toEqual(['a', 'b'])
  })
})

describe('clampSeedPrompt', () => {
  it('leaves short prompts untouched', () => {
    expect(clampSeedPrompt('hello')).toBe('hello')
  })

  it('truncates to MAX_SEED_PROMPT', () => {
    const clamped = clampSeedPrompt('y'.repeat(MAX_SEED_PROMPT + 500))
    expect(clamped.length).toBe(MAX_SEED_PROMPT)
  })

  it('returns "" for non-string input', () => {
    expect(clampSeedPrompt(undefined as unknown as string)).toBe('')
    expect(clampSeedPrompt(null as unknown as string)).toBe('')
  })
})
