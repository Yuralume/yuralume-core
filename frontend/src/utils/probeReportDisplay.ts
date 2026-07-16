import type { ProbeReport } from './api/providerSettings'

// Canonical capability ordering for rendering multi-capability probe sets
// (a saved connection row can hold several capabilities). Unknown
// capabilities sort last, preserving their received order.
const CAPABILITY_ORDER: readonly string[] = [
  'llm',
  'embedding',
  'image',
  'video',
  'tts',
  'search',
  'cloud',
]

/**
 * Format a probe latency (milliseconds) as a one-decimal seconds string,
 * e.g. 400 → "0.4s". Non-finite or negative values yield an empty string
 * so callers can omit the suffix entirely.
 */
export function formatLatency(latencyMs: number): string {
  if (!Number.isFinite(latencyMs) || latencyMs < 0) return ''
  return `${(latencyMs / 1000).toFixed(1)}s`
}

/** Universal ok/fail glyph, language-neutral for badge/line rendering. */
export function probeStatusMark(ok: boolean): string {
  return ok ? '✓' : '✗'
}

/**
 * Stable-sort probes by canonical capability order. Ties (same capability)
 * keep their input order; unknown capabilities fall to the end.
 */
export function orderProbes(probes: readonly ProbeReport[]): ProbeReport[] {
  const rank = (capability: string): number => {
    const idx = CAPABILITY_ORDER.indexOf(capability)
    return idx === -1 ? CAPABILITY_ORDER.length : idx
  }
  return probes
    .map((probe, index) => ({ probe, index }))
    .sort((a, b) => {
      const byCapability = rank(a.probe.capability) - rank(b.probe.capability)
      return byCapability !== 0 ? byCapability : a.index - b.index
    })
    .map(entry => entry.probe)
}

/**
 * Render one probe as a single-line summary, e.g.
 * "✓ llm · listed_models · 42 models (0.3s)".
 *
 * `actionLabel` is an optional pre-localized action label; when omitted the
 * raw backend action token is used (keeps this module i18n-agnostic and
 * unit-testable). Empty capability/detail segments are dropped so the line
 * never contains dangling separators.
 */
export function formatProbeLine(
  probe: ProbeReport,
  actionLabel?: string,
): string {
  const middle = [probe.capability, actionLabel ?? probe.action, probe.detail]
    .map(part => (part ?? '').trim())
    .filter(part => part.length > 0)
    .join(' · ')
  const latency = formatLatency(probe.latency_ms)
  const suffix = latency ? ` (${latency})` : ''
  return `${probeStatusMark(probe.ok)} ${middle}${suffix}`
}

/**
 * Render an ordered probe set as a newline-joined block, suitable for a
 * notification description with `white-space: pre-line`.
 *
 * `actionLabelFor` optionally maps a raw action token to a localized label.
 */
export function formatProbeLines(
  probes: readonly ProbeReport[],
  actionLabelFor?: (action: string) => string,
): string {
  return orderProbes(probes)
    .map(probe => formatProbeLine(probe, actionLabelFor?.(probe.action)))
    .join('\n')
}
