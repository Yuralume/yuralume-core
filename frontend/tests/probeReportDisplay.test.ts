import { describe, expect, it } from 'vitest'

import type { ProbeReport } from '@/utils/api/providerSettings'
import {
  formatLatency,
  formatProbeLine,
  formatProbeLines,
  orderProbes,
  probeStatusMark,
} from '@/utils/probeReportDisplay'

function probe(overrides: Partial<ProbeReport> = {}): ProbeReport {
  return {
    capability: 'llm',
    action: 'listed_models',
    ok: true,
    detail: '42 models',
    latency_ms: 300,
    ...overrides,
  } as ProbeReport
}

describe('formatLatency', () => {
  it('formats milliseconds as one-decimal seconds', () => {
    expect(formatLatency(400)).toBe('0.4s')
    expect(formatLatency(300)).toBe('0.3s')
    expect(formatLatency(1234)).toBe('1.2s')
    expect(formatLatency(0)).toBe('0.0s')
  })

  it('returns empty string for non-finite or negative input', () => {
    expect(formatLatency(-1)).toBe('')
    expect(formatLatency(Number.NaN)).toBe('')
    expect(formatLatency(Number.POSITIVE_INFINITY)).toBe('')
  })
})

describe('probeStatusMark', () => {
  it('maps ok/fail to universal glyphs', () => {
    expect(probeStatusMark(true)).toBe('✓')
    expect(probeStatusMark(false)).toBe('✗')
  })
})

describe('formatProbeLine', () => {
  it('renders the contract example line with the raw action token', () => {
    expect(formatProbeLine(probe())).toBe('✓ llm · listed_models · 42 models (0.3s)')
  })

  it('uses a localized action label when provided', () => {
    expect(formatProbeLine(probe(), 'Model list')).toBe(
      '✓ llm · Model list · 42 models (0.3s)',
    )
  })

  it('marks failures with ✗', () => {
    expect(formatProbeLine(probe({ ok: false, detail: 'HTTP 401' }))).toBe(
      '✗ llm · listed_models · HTTP 401 (0.3s)',
    )
  })

  it('omits an empty detail segment without a dangling separator', () => {
    expect(formatProbeLine(probe({ detail: '' }))).toBe('✓ llm · listed_models (0.3s)')
  })

  it('omits the latency suffix when latency is unavailable', () => {
    expect(formatProbeLine(probe({ latency_ms: -1 }))).toBe(
      '✓ llm · listed_models · 42 models',
    )
  })
})

describe('orderProbes', () => {
  it('orders by canonical capability with unknown capabilities last', () => {
    const probes = [
      probe({ capability: 'search', action: 'searched' }),
      probe({ capability: 'mystery', action: 'reachability' }),
      probe({ capability: 'llm', action: 'chat_completion' }),
      probe({ capability: 'image', action: 'generated_image' }),
    ]
    expect(orderProbes(probes).map(p => p.capability)).toEqual([
      'llm',
      'image',
      'search',
      'mystery',
    ])
  })

  it('is stable within the same capability', () => {
    const probes = [
      probe({ capability: 'llm', action: 'config_check' }),
      probe({ capability: 'llm', action: 'chat_completion' }),
    ]
    expect(orderProbes(probes).map(p => p.action)).toEqual([
      'config_check',
      'chat_completion',
    ])
  })

  it('does not mutate the input array', () => {
    const probes = [
      probe({ capability: 'image' }),
      probe({ capability: 'llm' }),
    ]
    const snapshot = probes.map(p => p.capability)
    orderProbes(probes)
    expect(probes.map(p => p.capability)).toEqual(snapshot)
  })
})

describe('formatProbeLines', () => {
  it('joins ordered lines with newlines and applies the label resolver', () => {
    const probes = [
      probe({ capability: 'image', action: 'generated_image', detail: 'ok', latency_ms: 900 }),
      probe({ capability: 'llm', action: 'listed_models', detail: '42 models', latency_ms: 300 }),
    ]
    const label = (action: string) => (action === 'listed_models' ? 'Model list' : 'Image generation')
    expect(formatProbeLines(probes, label)).toBe(
      '✓ llm · Model list · 42 models (0.3s)\n✓ image · Image generation · ok (0.9s)',
    )
  })

  it('falls back to raw action tokens without a resolver', () => {
    expect(formatProbeLines([probe()])).toBe('✓ llm · listed_models · 42 models (0.3s)')
  })
})
