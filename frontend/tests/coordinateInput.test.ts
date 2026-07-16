import { describe, expect, it } from 'vitest'
import { normalizeCoordinateInput } from '@/utils/coordinateInput'

describe('normalizeCoordinateInput', () => {
  it('passes through a typed number unchanged (Vue auto-cast on type="number")', () => {
    expect(normalizeCoordinateInput(35.24)).toBe(35.24)
  })

  it('parses a numeric string', () => {
    expect(normalizeCoordinateInput('35.24')).toBe(35.24)
  })

  it('treats an empty string as no value', () => {
    expect(normalizeCoordinateInput('')).toBeNull()
  })

  it('treats a whitespace-only string as no value', () => {
    expect(normalizeCoordinateInput('   ')).toBeNull()
  })

  it('treats null/undefined as no value', () => {
    expect(normalizeCoordinateInput(null)).toBeNull()
    expect(normalizeCoordinateInput(undefined)).toBeNull()
  })

  it('preserves the falsy-but-valid coordinate 0', () => {
    expect(normalizeCoordinateInput(0)).toBe(0)
    expect(normalizeCoordinateInput('0')).toBe(0)
  })

  it('passes out-of-range values through unchanged (range validation is the backend contract)', () => {
    expect(normalizeCoordinateInput(120)).toBe(120)
    expect(normalizeCoordinateInput('-200')).toBe(-200)
  })

  it('trims surrounding whitespace from a numeric string', () => {
    expect(normalizeCoordinateInput('  12.5  ')).toBe(12.5)
  })

  it('returns null for a non-numeric string instead of NaN', () => {
    expect(normalizeCoordinateInput('not-a-number')).toBeNull()
  })

  it('returns null for a non-finite typed number (defensive)', () => {
    expect(normalizeCoordinateInput(Number.NaN)).toBeNull()
  })

  it('reproduces the original bug path: calling .trim() on a Vue-cast number throws', () => {
    // This is the exact defect: Vue 3 auto-casts v-model on <input type="number">
    // to a JS number even without .number, so a ref declared ref('') can hold a
    // number after manual entry. The old code called `.value.trim()` directly.
    const valueAfterVueNumberCast: unknown = 35.24
    expect(() => (valueAfterVueNumberCast as string).trim()).toThrow(TypeError)
    // The helper must handle the same value without throwing.
    expect(() => normalizeCoordinateInput(valueAfterVueNumberCast as number)).not.toThrow()
    expect(normalizeCoordinateInput(valueAfterVueNumberCast as number)).toBe(35.24)
  })
})
