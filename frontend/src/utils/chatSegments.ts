export const TEXTING_REVEAL_DELAY = {
  baseMs: 350,
  perCharMs: 18,
  minMs: 300,
  maxMs: 1800,
  totalCapMs: 5000,
} as const

type SplitAssistantBubblesOptions = {
  stripActionNarration?: boolean
}

export function stripActionNarration(content: string): string {
  return (content ?? '')
    .replace(/\*[^*\n]+\*/g, ' ')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n[ \t]+/g, '\n')
    .replace(/[ \t]{2,}/g, ' ')
    .trim()
}

export function splitAssistantBubbles(
  content: string,
  options: SplitAssistantBubblesOptions = {},
): string[] {
  const raw = options.stripActionNarration
    ? stripActionNarration(content)
    : (content ?? '')
  const trimmed = raw.trim()
  if (!trimmed) return []
  if (!options.stripActionNarration && trimmed.includes('*')) return [raw]

  const segments = trimmed
    .split(/\n\s*\n+/)
    .map(part => part.trim())
    .filter(Boolean)

  return segments.length > 0 ? segments : [trimmed]
}

export function revealDelayMs(segment: string): number {
  const length = [...(segment ?? '')].length
  const value = TEXTING_REVEAL_DELAY.baseMs + length * TEXTING_REVEAL_DELAY.perCharMs
  return Math.max(
    TEXTING_REVEAL_DELAY.minMs,
    Math.min(TEXTING_REVEAL_DELAY.maxMs, value),
  )
}

export function revealDelaysFor(segments: string[]): number[] {
  if (segments.length <= 1) return []

  const delays = segments.slice(0, -1).map(revealDelayMs)
  const total = delays.reduce((sum, delay) => sum + delay, 0)
  if (total <= TEXTING_REVEAL_DELAY.totalCapMs) return delays

  const minTotal = delays.length * TEXTING_REVEAL_DELAY.minMs
  if (minTotal >= TEXTING_REVEAL_DELAY.totalCapMs) {
    return delays.map(() => TEXTING_REVEAL_DELAY.minMs)
  }

  const availableSlack = TEXTING_REVEAL_DELAY.totalCapMs - minTotal
  const originalSlack = total - minTotal
  return delays.map(delay => (
    TEXTING_REVEAL_DELAY.minMs
    + Math.floor((delay - TEXTING_REVEAL_DELAY.minMs) * availableSlack / originalSlack)
  ))
}
