import { describe, expect, it } from 'vitest'
import {
  clampRingRatio,
  ringCircumference,
  ringDashOffset,
  ringRadius,
} from '../src/utils/progressRing'

describe('clampRingRatio', () => {
  it('passes through values already inside [0, 1]', () => {
    expect(clampRingRatio(0)).toBe(0)
    expect(clampRingRatio(0.5)).toBe(0.5)
    expect(clampRingRatio(1)).toBe(1)
  })

  it('clamps values outside the range', () => {
    expect(clampRingRatio(-0.4)).toBe(0)
    expect(clampRingRatio(1.7)).toBe(1)
  })

  it('falls back to 0 for non-finite input', () => {
    expect(clampRingRatio(Number.NaN)).toBe(0)
    expect(clampRingRatio(Number.POSITIVE_INFINITY)).toBe(0)
    expect(clampRingRatio(Number.NEGATIVE_INFINITY)).toBe(0)
    // A prop typed `number` that arrives `undefined` at runtime (e.g. a
    // still-loading async value) must not blow up the SVG attribute.
    expect(clampRingRatio(undefined as unknown as number)).toBe(0)
  })
})

describe('ringCircumference', () => {
  it('is 2πr', () => {
    expect(ringCircumference(10)).toBeCloseTo(2 * Math.PI * 10, 10)
    expect(ringCircumference(0)).toBe(0)
  })

  it('clamps a negative radius to 0 rather than returning a negative length', () => {
    expect(ringCircumference(-5)).toBe(0)
  })
})

describe('ringRadius', () => {
  it('centers the stroke inside the box: (size - thickness) / 2', () => {
    expect(ringRadius(32, 4)).toBe(14)
    expect(ringRadius(48, 6)).toBe(21)
  })

  it('never returns a negative radius when thickness exceeds size', () => {
    expect(ringRadius(4, 20)).toBe(0)
  })
})

describe('ringDashOffset', () => {
  const radius = 10
  const circumference = ringCircumference(radius)

  it('is the full circumference at ratio 0 (nothing drawn)', () => {
    expect(ringDashOffset(0, radius)).toBeCloseTo(circumference, 10)
  })

  it('is 0 at ratio 1 (the whole ring drawn)', () => {
    expect(ringDashOffset(1, radius)).toBeCloseTo(0, 10)
  })

  it('is proportional in between', () => {
    expect(ringDashOffset(0.25, radius)).toBeCloseTo(circumference * 0.75, 10)
    expect(ringDashOffset(0.5, radius)).toBeCloseTo(circumference * 0.5, 10)
  })

  it('clamps an out-of-range or non-finite ratio instead of returning NaN', () => {
    expect(ringDashOffset(-1, radius)).toBeCloseTo(circumference, 10)
    expect(ringDashOffset(2, radius)).toBeCloseTo(0, 10)
    expect(Number.isNaN(ringDashOffset(Number.NaN, radius))).toBe(false)
    expect(ringDashOffset(Number.NaN, radius)).toBeCloseTo(circumference, 10)
  })
})
