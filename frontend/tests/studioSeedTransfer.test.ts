import { describe, expect, it } from 'vitest'

import {
  stashStudioSeed,
  takeStudioSeed,
} from '@/utils/studioSeedTransfer'

describe('studioSeedTransfer', () => {
  it('hands a stashed seed to exactly one taker', () => {
    stashStudioSeed({ seedPrompt: 'seed-text', cast: ['c1'] })
    expect(takeStudioSeed()).toEqual({ seedPrompt: 'seed-text', cast: ['c1'] })
    // Take-once: a second take (refresh / revisit) gets nothing.
    expect(takeStudioSeed()).toBeNull()
  })

  it('returns null when nothing was stashed', () => {
    expect(takeStudioSeed()).toBeNull()
  })

  it('lets a newer stash replace an unconsumed one', () => {
    stashStudioSeed({ seedPrompt: 'first' })
    stashStudioSeed({ seedPrompt: 'second', cast: ['a', 'b'] })
    expect(takeStudioSeed()).toEqual({ seedPrompt: 'second', cast: ['a', 'b'] })
    expect(takeStudioSeed()).toBeNull()
  })

  it('carries seeds without a cast', () => {
    stashStudioSeed({ seedPrompt: 'template-scaffold' })
    expect(takeStudioSeed()).toEqual({ seedPrompt: 'template-scaffold' })
  })
})
