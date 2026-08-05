import { describe, expect, it } from 'vitest'
import { nextRovingRadioIndex } from '@/utils/radioGroupKeyboard'

describe('nextRovingRadioIndex', () => {
  it('moves to the next option on ArrowRight', () => {
    expect(nextRovingRadioIndex('ArrowRight', 0, 3)).toBe(1)
  })

  it('moves to the next option on ArrowDown (equivalent to ArrowRight)', () => {
    expect(nextRovingRadioIndex('ArrowDown', 0, 3)).toBe(1)
  })

  it('moves to the previous option on ArrowLeft', () => {
    expect(nextRovingRadioIndex('ArrowLeft', 1, 3)).toBe(0)
  })

  it('moves to the previous option on ArrowUp (equivalent to ArrowLeft)', () => {
    expect(nextRovingRadioIndex('ArrowUp', 1, 3)).toBe(0)
  })

  it('wraps from the last option to the first on ArrowRight', () => {
    expect(nextRovingRadioIndex('ArrowRight', 2, 3)).toBe(0)
  })

  it('wraps from the first option to the last on ArrowLeft', () => {
    expect(nextRovingRadioIndex('ArrowLeft', 0, 3)).toBe(2)
  })

  it('jumps to the first option on Home', () => {
    expect(nextRovingRadioIndex('Home', 2, 3)).toBe(0)
  })

  it('jumps to the last option on End', () => {
    expect(nextRovingRadioIndex('End', 0, 3)).toBe(2)
  })

  it('returns null for keys outside the radiogroup pattern (e.g. Tab)', () => {
    expect(nextRovingRadioIndex('Tab', 0, 3)).toBeNull()
    expect(nextRovingRadioIndex('Enter', 0, 3)).toBeNull()
    expect(nextRovingRadioIndex(' ', 0, 3)).toBeNull()
  })

  it('returns null when there are no options to move between', () => {
    expect(nextRovingRadioIndex('ArrowRight', 0, 0)).toBeNull()
  })

  it('wraps a not-found current index (-1) instead of returning a negative index', () => {
    // Defensive: shouldn't happen in practice (playerMode is always one of
    // the three known modes), but the function must stay total either way.
    expect(nextRovingRadioIndex('ArrowRight', -1, 3)).toBe(0)
    expect(nextRovingRadioIndex('ArrowLeft', -1, 3)).toBe(1)
  })
})
